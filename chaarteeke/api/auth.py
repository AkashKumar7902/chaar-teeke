"""Sessions, roles and the visitor entry. The server enforces every rule the UI shows."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import audit, clock
from ..context import AppContext

COOKIE = "ct_session"
CSRF_HEADER = "x-ct-csrf"
STAFF_ROLES = ("clinic", "abc", "vet", "oversight", "reviewer", "admin")
VISITOR_VIEWS = ("clinic", "abc", "oversight", "reviewer", "admin")
# An input rule for the admin's "set password" box, not an operating threshold. A contract_request asks
# for it to move into data/policy.json.
MIN_PASSWORD_LENGTH = 10
# What sign-in compares against when the username does not exist, so a wrong name takes as long as a
# wrong password and the answer does not reveal which usernames are real.
_NO_SUCH_USER = "scrypt$" + "00" * 16 + "$" + "00" * 32


@dataclass(frozen=True)
class Principal:
    role: str  # a staff role or "visitor"
    actor: str  # audit actor string
    display_name: str
    staff_id: str | None
    clinic_id: str | None
    acting_as: str | None  # a visitor's chosen view
    csrf: str

    @property
    def can_write(self) -> bool:
        return self.role != "visitor"

    @property
    def view(self) -> str:
        """The role whose pages this principal sees."""
        return self.acting_as if self.role == "visitor" and self.acting_as else self.role


class AuthError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        retry_after: int | None = None,
        *,
        key: str | None = None,
        params: dict[str, str | int] | None = None,
    ) -> None:
        """`code` is the error class; `key` (default: `code`) is the stable id of this exact message, and
        `params` the values the message names, so a page can translate it. Both are sent only when `key` or
        `params` is given."""
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after
        self.key, self.params = key, params


# ------------------------------------------------------------------ passwords
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk.hex().encode(), dk_hex.encode())


# ------------------------------------------------------------------- sessions
def session_now() -> datetime:
    """Sessions run on real time. The demo clock jumps hours or days ahead, and that must not sign the
    presenter and the visitors out."""
    c = clock.current()
    return c.now() - c.offset if isinstance(c, clock.OffsetClock) else c.now()


def _token_hash(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def _session_hours(ctx: AppContext) -> int:
    return int(ctx.policy["visitor"]["session_hours"])


def create_session(
    ctx: AppContext, *, staff_id: str | None, role: str, visitor_label: str | None
) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = session_now()
    with ctx.db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (clock.iso(now),))
        conn.execute(
            "INSERT INTO sessions(token_hash, staff_id, role, visitor_label, csrf, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                _token_hash(token, ctx.settings.secret_key),
                staff_id,
                role,
                visitor_label,
                csrf,
                clock.iso(now),
                clock.iso(now + timedelta(hours=_session_hours(ctx))),
            ),
        )
        who = conn.execute("SELECT username FROM staff WHERE id = ?", (staff_id,)).fetchone()
        actor = f"visitor:{visitor_label.split(':')[0]}" if visitor_label else f"staff:{who['username']}"
        audit.record(conn, actor, "session.start", "session", None, {"role": role})
    return token, csrf


# --------------------------------------------------------------- rate limits
class AttemptLimiter:
    """Recent attempts per key, kept in memory (a restart forgets them). Timestamps are real time, so the
    demo clock jumping ahead does not lift a lock-out."""

    def __init__(self) -> None:
        self._seen: dict[str, deque[datetime]] = {}
        self.lock = threading.Lock()  # requests run in worker threads: check-and-count must be one step

    def _recent(self, key: str, now: datetime, window: timedelta) -> deque[datetime]:
        seen = self._seen.get(key)
        if seen is None:
            return deque()
        while seen and now - seen[0] >= window:
            seen.popleft()
        if not seen:
            del self._seen[key]
            return deque()
        return seen

    def wait(self, key: str, now: datetime, window: timedelta, limit: int) -> timedelta | None:
        """How long until `key` may try again, or None when it may try now."""
        seen = self._recent(key, now, window)
        if len(seen) < limit:
            return None
        return seen[0] + window - now

    def add(self, key: str, now: datetime, window: timedelta) -> None:
        self._recent(key, now, window)  # drop what is old before adding
        self._seen.setdefault(key, deque()).append(now)

    def clear(self, key: str) -> None:
        self._seen.pop(key, None)

    def clear_matching(self, keep: Callable[[str], bool]) -> int:
        """Forget every key for which `keep` is true; returns how many there were."""
        gone = [k for k in self._seen if keep(k)]
        for k in gone:
            del self._seen[k]
        return len(gone)

    def keys(self, prefix: str) -> list[str]:
        return [k for k in self._seen if k.startswith(prefix)]

    def last(self, key: str) -> datetime | None:
        seen = self._seen.get(key)
        return seen[-1] if seen else None

    def count(self, key: str) -> int:
        return len(self._seen.get(key, ()))

    def forget_latest(self, key: str) -> None:
        """Take back the newest attempt of `key` (a try that turned out right)."""
        seen = self._seen.get(key)
        if seen:
            seen.pop()
            if not seen:
                del self._seen[key]

    def sweep(self, now: datetime, window: timedelta) -> None:
        for key in list(self._seen):
            self._recent(key, now, window)


def _limiter(ctx: AppContext) -> AttemptLimiter:
    return ctx.extras.setdefault("attempt_limiter", AttemptLimiter())


def _wait_phrase(wait: timedelta) -> str:
    seconds = max(1, -(-int(wait.total_seconds()) // 1))
    if seconds <= 120:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes = -(-seconds // 60)
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


PAIR = "pair:"  # failures of one username from one address
ADDRESS = "ip:"  # failures from one address, whoever the username


def _pair_key(username: str, client: str) -> str:
    return f"{PAIR}{username.lower()}|{client}"


def _pair_delay(pol: dict, failures: int) -> timedelta:
    """How long after its last try a username-and-address pair must wait, once it has failed
    max_failed_signins times: signin_delay_base_s, doubling with each further failure, never above
    signin_delay_max_s. Under the limit there is no wait."""
    over = failures - pol["max_failed_signins"]
    if over < 0:
        return timedelta(0)
    return timedelta(seconds=min(pol["signin_delay_base_s"] * 2**over, pol["signin_delay_max_s"]))


def sign_in(ctx: AppContext, username: str, password: str, client: str = "") -> tuple[str, str]:
    """`client` is the address the request came from. Wrong passwords are counted per username and address
    together, so a stranger cannot lock somebody else's account from another address. After
    auth.max_failed_signins wrong tries in auth.window_min minutes that pair must wait before the next try, a
    wait that doubles with each wrong one (auth.signin_delay_base_s up to signin_delay_max_s); the right
    password does not shorten it. A separate, larger count per address (max_failed_signins_per_address)
    refuses everything from an address that guesses across many usernames. An administrator can clear either
    (`clear_signin_locks`)."""
    pol = ctx.policy["auth"]
    window, now = timedelta(minutes=pol["window_min"]), session_now()
    pair, address = _pair_key(username, client), f"{ADDRESS}{client}" if client else None
    limiter = _limiter(ctx)
    with limiter.lock:
        limiter.sweep(now, window)
        waits = []
        last = limiter.last(pair)
        if last is not None and (until := last + _pair_delay(pol, limiter.count(pair)) - now) > timedelta(0):
            waits.append(until)
        if address and (w := limiter.wait(address, now, window, pol["max_failed_signins_per_address"])):
            waits.append(w)
        if not waits:
            # Count the try before the slow password check, so a burst of parallel guesses cannot all pass
            # the limit first; a right password takes its count back below.
            limiter.add(pair, now, window)
            if address:
                limiter.add(address, now, window)
    if waits:
        wait = max(waits)
        raise AuthError(
            "rate_limited",
            f"Too many wrong sign-ins. Wait {_wait_phrase(wait)} and try again.",
            429,
            retry_after=int(wait.total_seconds()) + 1,
        )
    row = ctx.db.one("SELECT id, role, password_hash, active FROM staff WHERE username = ?", (username,))
    good = verify_password(password, row["password_hash"] if row else _NO_SUCH_USER)
    if row is None or not row["active"] or not good:
        raise AuthError("bad_credentials", "Wrong username or password", 401)
    with limiter.lock:
        limiter.clear(pair)
        if address:
            limiter.forget_latest(address)
    return create_session(ctx, staff_id=row["id"], role=row["role"], visitor_label=None)


def signin_locks(ctx: AppContext) -> list[dict]:
    """The usernames-and-addresses (and addresses alone) that are being made to wait now, for the
    administrator's list. A username is the one somebody typed, so it may not be a real login."""
    pol = ctx.policy["auth"]
    window, now = timedelta(minutes=pol["window_min"]), session_now()
    limiter, out = _limiter(ctx), []
    with limiter.lock:
        limiter.sweep(now, window)
        for key in limiter.keys(PAIR):
            last, n = limiter.last(key), limiter.count(key)
            wait = last + _pair_delay(pol, n) - now
            if wait > timedelta(0):
                username, _, client = key.removeprefix(PAIR).rpartition("|")
                out.append(
                    {
                        "username": username,
                        "address": client,
                        "failures": n,
                        "wait_s": int(wait.total_seconds()) + 1,
                    }
                )
        for key in limiter.keys(ADDRESS):
            w = limiter.wait(key, now, window, pol["max_failed_signins_per_address"])
            if w is not None:
                out.append(
                    {
                        "username": None,
                        "address": key.removeprefix(ADDRESS),
                        "failures": limiter.count(key),
                        "wait_s": int(w.total_seconds()) + 1,
                    }
                )
    return out


def clear_signin_locks(ctx: AppContext, username: str | None, address: str | None) -> int:
    """Forget the failed sign-ins of a username (from every address), of an address, or of both together.
    Returns how many counts were cleared."""
    limiter = _limiter(ctx)
    user = username.lower() if username else None

    def hit(key: str) -> bool:
        if key.startswith(PAIR):
            name, _, client = key.removeprefix(PAIR).rpartition("|")
            return (user is None or name == user) and (address is None or client == address)
        return user is None and address is not None and key == f"{ADDRESS}{address}"

    with limiter.lock:
        return limiter.clear_matching(hit)


def enter_as_visitor(ctx: AppContext, view: str, client: str = "") -> tuple[str, str]:
    """`client` is the address of the request: policy auth.visitor_sessions_per_min entries a minute from
    one address, then 429."""
    if not ctx.settings.is_demo:
        raise AuthError("not_available", "Visitor entry is only in the demo", 404)
    if view not in VISITOR_VIEWS:
        raise AuthError("bad_view", "Choose one of the listed views", 400)
    if client:
        window, now = timedelta(minutes=1), session_now()
        limiter, key = _limiter(ctx), f"visitor:{client}"
        with limiter.lock:
            wait = limiter.wait(key, now, window, ctx.policy["auth"]["visitor_sessions_per_min"])
            if wait is None:
                limiter.add(key, now, window)
        if wait is not None:
            raise AuthError(
                "rate_limited",
                "Too many visitors entered from this address in the last minute. Wait a moment and try again.",
                429,
                retry_after=int(wait.total_seconds()) + 1,
            )
    label = secrets.token_hex(3)
    return create_session(ctx, staff_id=None, role="visitor", visitor_label=f"{label}:{view}")


def sign_out(ctx: AppContext, token: str) -> None:
    with ctx.db.transaction() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token, ctx.settings.secret_key),)
        )


def revoke_sessions(conn, staff_id: str) -> int:
    """End every login of one person (a password reset, a deactivation). Runs in the caller's transaction."""
    return conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_id,)).rowcount


def principal_for(ctx: AppContext, token: str | None) -> Principal | None:
    if not token:
        return None
    row = ctx.db.one(
        "SELECT s.role, s.staff_id, s.visitor_label, s.csrf, s.expires_at, st.username, st.display_name,"
        " st.clinic_id, st.active FROM sessions s LEFT JOIN staff st ON st.id = s.staff_id"
        " WHERE s.token_hash = ?",
        (_token_hash(token, ctx.settings.secret_key),),
    )
    if row is None or clock.parse_iso(row["expires_at"]) <= session_now():
        return None
    if row["role"] == "visitor":
        if not ctx.settings.is_demo:  # a demo database opened in pilot mode must not keep its visitors
            return None
        label = row["visitor_label"] or "x:abc"
        n, _, view = label.partition(":")
        return Principal(
            "visitor", f"visitor:{n}", "Visitor", None, _visitor_clinic(ctx, view), view, row["csrf"]
        )
    if not row["active"]:
        return None
    return Principal(
        row["role"],
        f"staff:{row['username']}",
        row["display_name"],
        row["staff_id"],
        row["clinic_id"],
        None,
        row["csrf"],
    )


def _visitor_clinic(ctx: AppContext, view: str) -> str | None:
    if view != "clinic":
        return None
    row = ctx.db.one(
        "SELECT id FROM clinics WHERE arv_status = 'confirmed' AND is_demo = 1 ORDER BY id LIMIT 1"
    )
    return row["id"] if row else None


def check_csrf(p: Principal, header_value: str | None) -> None:
    # Compared as bytes: a header with a non-ASCII character would make a str comparison raise.
    if not header_value or not hmac.compare_digest(header_value.encode(), p.csrf.encode()):
        raise AuthError("csrf", "Your session expired. Reload the page.", 403)


# --------------------------------------------------------------- role matrix
# Which views may read each area. Writes additionally need can_write (visitors never write).
READ = {
    # Patient level (today's list, a course, the call-backs): the clinic's own people only. The administrator
    # sets the system up and does not read a clinic's patients (docs/PRIVACY.md).
    "clinic": {"clinic"},
    "clinic_config": {"clinic", "admin"},  # the clinic's settings and the schedules it offers, no patient
    "team": {"abc", "vet", "oversight", "admin"},
    "alerts": {"abc", "vet", "admin"},
    # The summary and weekly digest are aggregates (small cells show "<5", shares need n >= 25, no row-level
    # date), so the reviewer, who is Health staff too, may read them: the digest page serves both roles.
    "oversight": {"oversight", "reviewer", "admin"},
    "records": {"abc", "vet", "admin"},  # the team's own field records: worked wards, sight counts
    "review": {"reviewer"},  # recordings are heard by the reviewer only, not by the administrator
    # register.csv is one row per patient course (clinic, register number, ward, doses given). It is the
    # Health register's own data, so only the reviewer, the data fiduciary's people, reads it. Oversight
    # sees aggregates only and the administrator sets the system up without seeing patients.
    "register": {"reviewer"},
    "admin": {"admin"},
    "system": {"admin", "abc", "clinic", "oversight", "reviewer", "vet"},
}
WRITE = {
    "clinic": {"clinic"},
    "alerts.claim": {"vet"},
    "alerts.callback": {"vet"},
    "team.records": {"abc", "vet"},
    "review": {"reviewer"},
    "admin": {"admin"},
}


def require_read(p: Principal | None, area: str) -> Principal:
    if p is None:
        raise AuthError("signed_out", "Sign in to continue", 401)
    if p.view not in READ[area]:
        raise AuthError("forbidden", "Your role can't see this", 403)
    return p


def require_demo_control(ctx: AppContext, p: Principal | None, csrf_header: str | None) -> Principal:
    """The demo clock is the visitor's control, so a visitor may move it. It is the one write a visitor makes
    besides opening a phone; it exists only in demo mode."""
    if p is None:
        raise AuthError("signed_out", "Sign in to continue", 401)
    if not ctx.settings.is_demo:
        raise AuthError("not_available", "The demo clock only exists in demo mode", 404)
    check_csrf(p, csrf_header)
    return p


def require_write(p: Principal | None, area: str, csrf_header: str | None) -> Principal:
    if p is None:
        raise AuthError("signed_out", "Sign in to continue", 401)
    if not p.can_write:
        raise AuthError("read_only", "Your role can't do this", 403)
    if p.role not in WRITE[area]:
        raise AuthError("forbidden", "Your role can't do this", 403)
    check_csrf(p, csrf_header)
    return p
