"""Retention jobs: phone numbers, voice clips and unmatched missed calls leave the system on time.

What goes, and when (numbers in policy.retention):
- a phone number: `purge_after` (7 days after the last scheduled dose); a course still open then is closed;
- a place clip and its transcript: once the place is decided or matched, or at `expires_at`;
- a missed call nobody linked, and a repeat call kept so staff can link it to another register entry: after
  `unlinked_missed_call_hours`, with its last four digits;
- a voice file in var/clips that no row points to (a save that broke half way): after `orphan_clip_minutes`.
Every deletion writes `purge_log` and an audit row; one `kv['last_purge_run']` says when the job last ran.
After a run that removed a number, a clip or a transcript, the database file is rewritten (VACUUM) so the
deleted bytes do not stay in free pages or the write-ahead log.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel

from . import audit, clock
from .context import AppContext
from .doses import emit

log = logging.getLogger(__name__)
ACTOR = "system:scheduler"
_DONE_STATUSES = ("matched", "decided", "outside_jmc", "cant_tell", "no_place_heard", "skipped")


class PurgeReport(BaseModel):
    ran_at: str
    phones: int = 0
    clips: int = 0
    missed_calls: int = 0
    orphan_clips: int = 0
    errors: list[str] = []


def _log(conn: sqlite3.Connection, course_id: str | None, what: str, reason: str, now: str) -> None:
    conn.execute(
        "INSERT INTO purge_log(course_id, what, reason, purged_at) VALUES (?,?,?,?)",
        (course_id, what, reason, now),
    )


def _purge_phones(ctx: AppContext, conn: sqlite3.Connection, now: str, events: list) -> int:
    rows = conn.execute(
        "SELECT course_id FROM contacts WHERE purge_after IS NOT NULL AND purge_after <= ?", (now,)
    ).fetchall()
    for r in rows:
        cid = r["course_id"]
        conn.execute("DELETE FROM contacts WHERE course_id=?", (cid,))
        conn.execute("UPDATE missed_calls SET phone_last4=NULL WHERE course_id=?", (cid,))
        _log(conn, cid, "phone", "retention", now)
        audit.record(conn, ACTOR, "phone.purge", "course", cid, {"reason": "retention"})
        for c in conn.execute(
            "SELECT id, kind, transport FROM calls WHERE course_id=? AND status='scheduled'",
            (cid,),
        ).fetchall():
            conn.execute(
                "UPDATE calls SET status='cancelled', error='the phone number was removed' WHERE id=?",
                (c["id"],),
            )
            events.append(
                (
                    "call",
                    {
                        "call_id": c["id"],
                        "course_id": cid,
                        "kind": c["kind"],
                        "status": "cancelled",
                        "simulated": c["transport"] == "sim",
                    },
                )
            )
        closed = conn.execute(
            "UPDATE courses SET status='stopped', stopped_reason=? WHERE id=? AND status IN ('active','awaiting_call')",
            ("Course window over; the phone number was removed", cid),
        ).rowcount
        if closed:
            events.append(("course", {"course_id": cid, "status": "stopped"}))
    return len(rows)


def clip_path(ctx: AppContext, audio_path: str) -> Path:
    """The file for a stored clip path; refuses anything outside var/clips."""
    clips_dir = (ctx.settings.var_dir / "clips").resolve()
    p = Path(audio_path)
    resolved = (p if p.is_absolute() else ctx.settings.var_dir / p).resolve()
    if not resolved.is_relative_to(clips_dir):
        raise ValueError(f"clip path {audio_path} is outside {clips_dir}")
    return resolved


def delete_clip_file(ctx: AppContext, audio_path: str) -> None:
    """Delete the file behind a stored clip path. The demo's example recordings ship in data/demo/clips and
    are shared by every demo, so they are never deleted: the row simply lets go of them. Any other path
    outside var/clips is refused (ValueError)."""
    try:
        clip_path(ctx, audio_path).unlink(missing_ok=True)
    except ValueError:
        shipped = (ctx.settings.data_dir / "demo" / "clips").resolve()
        p = Path(audio_path)
        if not (p if p.is_absolute() else ctx.settings.data_dir.parent / p).resolve().is_relative_to(shipped):
            raise


def _purge_clips(ctx: AppContext, conn: sqlite3.Connection, now: str, errors: list[str]) -> int:
    rows = conn.execute(
        "SELECT pc.report_id, pc.audio_path, pc.decided_at, pc.expires_at, r.location_status, r.course_id"
        " FROM place_captures pc JOIN bite_reports r ON r.id = pc.report_id"
        " WHERE pc.clip_deleted_at IS NULL AND (pc.expires_at <= ? OR pc.decided_at IS NOT NULL"
        f" OR r.location_status IN ({','.join('?' * len(_DONE_STATUSES))}))",
        (now, *_DONE_STATUSES),
    ).fetchall()
    n = 0
    for r in rows:
        try:
            if r["audio_path"]:
                delete_clip_file(ctx, r["audio_path"])
        except (OSError, ValueError) as e:
            errors.append(f"clip {r['report_id']}: {e}")
            log.error("could not delete clip %s: %s", r["report_id"], e)
            continue
        undecided = r["decided_at"] is None and r["location_status"] in ("pending", "needs_review")
        conn.execute(
            "UPDATE place_captures SET clip_deleted_at=?, audio_path=NULL, transcript=NULL, candidates_json=NULL"
            " WHERE report_id=?",
            (now, r["report_id"]),
        )
        if undecided:
            # Nobody placed it in time. It leaves the review queue and is counted as a place we could not tell.
            conn.execute("UPDATE bite_reports SET location_status='cant_tell' WHERE id=?", (r["report_id"],))
        reason = "expired" if undecided else "decided"
        _log(conn, r["course_id"], "clip", reason, now)
        audit.record(conn, ACTOR, "clip.purge", "bite_report", r["report_id"], {"reason": reason})
        n += 1
    return n


def _purge_missed_calls(ctx: AppContext, conn: sqlite3.Connection, now_dt, now: str) -> int:
    hours = ctx.policy["retention"]["unlinked_missed_call_hours"]
    cutoff = clock.iso(now_dt - timedelta(hours=hours))
    n = 0
    for r in conn.execute(
        "SELECT id, status FROM missed_calls WHERE status IN ('unlinked','duplicate')"
        " AND phone_cipher IS NOT NULL AND at <= ?",
        (cutoff,),
    ).fetchall():
        # the last four digits go with the number: nothing of it is kept once the row has expired
        conn.execute(
            "UPDATE missed_calls SET phone_cipher=NULL, phone_last4=NULL,"
            " status=CASE status WHEN 'unlinked' THEN 'expired' ELSE status END WHERE id=?",
            (r["id"],),
        )
        _log(conn, None, "missed_call", "expired", now)
        audit.record(conn, ACTOR, "missed_call.purge", "missed_call", r["id"], {"reason": "expired"})
        n += 1
    return n


def _sweep_orphan_clips(
    ctx: AppContext, conn: sqlite3.Connection, now_dt: datetime, now: str, errors: list[str]
) -> int:
    """Delete voice files in var/clips that no place_captures row points to: a save that broke half way leaves
    one, and nothing else would ever remove it. A file younger than `orphan_clip_minutes` may still be in
    the middle of being saved, so it is left alone."""
    clips = ctx.settings.var_dir / "clips"
    if not clips.is_dir():
        return 0
    older = now_dt - timedelta(minutes=ctx.policy["retention"]["orphan_clip_minutes"])
    n = 0
    for f in sorted(clips.iterdir()):
        try:
            if not f.is_file() or datetime.fromtimestamp(f.stat().st_mtime, UTC) > older:
                continue
            if conn.execute("SELECT 1 FROM place_captures WHERE audio_path=?", (str(f),)).fetchone():
                continue
            f.unlink()
        except OSError as e:
            errors.append(f"orphan clip {f.name}: {e}")
            log.error("could not delete orphan clip %s: %s", f.name, e)
            continue
        _log(conn, None, "clip", "orphan", now)
        audit.record(conn, ACTOR, "clip.purge", "clip_file", f.name, {"reason": "orphan"})
        n += 1
    return n


class StorageStatus(BaseModel):
    ok: bool
    free_mb: int | None = None
    detail: str


def storage_status(ctx: AppContext) -> StorageStatus:
    """Can this server still save a voice clip? var/ must be writable and have policy storage.min_free_mb free."""
    var = ctx.settings.var_dir
    probe = var / ".write-check"
    try:
        var.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"ok")
        probe.unlink()
        free_mb = shutil.disk_usage(var).free // (1024 * 1024)
    except OSError as e:
        return StorageStatus(ok=False, detail=f"The data folder is not writable: {type(e).__name__}.")
    need = ctx.policy["storage"]["min_free_mb"]
    if free_mb < need:
        return StorageStatus(
            ok=False, free_mb=free_mb, detail=f"Only {free_mb} MB is free; at least {need} MB is needed."
        )
    return StorageStatus(
        ok=True, free_mb=free_mb, detail=f"The data folder is writable with {free_mb} MB free."
    )


def _scrub_database(ctx: AppContext) -> None:
    """Rewrite the database file so deleted numbers, transcripts and answers do not linger in free pages or in
    the write-ahead log. Runs outside any transaction."""
    conn = ctx.db.connect()
    if ctx.db.path == ":memory:":
        return
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def rotate_ciphers(ctx: AppContext, actor: str = ACTOR) -> int:
    """Re-encrypt every stored phone number with the first of the configured keys, after a key is replaced
    (put the new key first in CT_PHONE_KEY, run this, then drop the old key). Returns how many were done."""
    n = 0
    with ctx.db.transaction() as conn:
        for table, key in (("contacts", "course_id"), ("missed_calls", "id")):
            for r in conn.execute(
                f"SELECT {key} AS k, phone_cipher FROM {table} WHERE phone_cipher IS NOT NULL"
            ).fetchall():
                conn.execute(
                    f"UPDATE {table} SET phone_cipher=? WHERE {key}=?",
                    (ctx.vault.rotate(r["phone_cipher"]), r["k"]),
                )
                n += 1
        audit.record(conn, actor, "phone.rotate_keys", None, None, {"rows": n})
    return n


class BackupError(Exception):
    """A backup that could not be made or opened, in plain words."""


def _backup_key(ctx: AppContext) -> bytes:
    if ctx.settings.backup_dir is None or not ctx.settings.backup_key:
        raise BackupError("Backups are not set up: set CT_BACKUP_DIR and CT_BACKUP_KEY.")
    return ctx.settings.backup_key.encode()


def backup_database(ctx: AppContext) -> Path:
    """Write an encrypted copy of the whole database (phone numbers are in it, themselves encrypted) to
    CT_BACKUP_DIR, and delete copies older than policy retention.backup_keep_days. The plain copy exists only
    for the moment it takes to encrypt it, in a private file. Returns the new file."""
    key = _backup_key(ctx)
    folder = ctx.settings.backup_dir
    now = clock.now()
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, plain_name = tempfile.mkstemp(dir=folder, prefix=".plain-", suffix=".db")  # mkstemp makes it 0600
        os.close(fd)
        plain = Path(plain_name)
        try:
            copy = sqlite3.connect(plain)
            try:
                ctx.db.connect().backup(copy)
            finally:
                copy.close()
            blob = Fernet(key).encrypt(plain.read_bytes())
        finally:
            plain.unlink(missing_ok=True)
        target = folder / f"chaarteeke-{now.strftime('%Y%m%dT%H%M%SZ')}.db.enc"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        oldest = now - timedelta(days=ctx.policy["retention"]["backup_keep_days"])
        for old in folder.glob("chaarteeke-*.db.enc"):
            if old != target and datetime.fromtimestamp(old.stat().st_mtime, UTC) < oldest:
                old.unlink()
    except (OSError, sqlite3.Error) as e:
        raise BackupError(f"The backup could not be made: {type(e).__name__}.") from e
    return target


def restore_backup(backup: Path, key: str, destination: Path) -> Path:
    """Decrypt a backup file into a new database file (never over an existing one). Open it with the same
    CT_PHONE_KEY the server used; the numbers inside are still encrypted with it."""
    if destination.exists():
        raise BackupError(f"{destination} already exists. Restore to a new file and move it into place.")
    try:
        plain = Fernet(key.encode()).decrypt(backup.read_bytes())
    except InvalidToken as e:
        raise BackupError("This backup does not open with that key.") from e
    except OSError as e:
        raise BackupError(f"The backup could not be read: {type(e).__name__}.") from e
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(plain)
    return destination


def run_purges(ctx: AppContext) -> PurgeReport:
    """Run every retention job once. Safe to repeat: a second run finds nothing left to do."""
    now_dt = clock.now()
    now = clock.iso(now_dt)
    events: list[tuple[str, dict[str, Any]]] = []
    errors: list[str] = []
    ctx.db.connect().execute("PRAGMA secure_delete = ON")  # freed pages are overwritten, not just unlinked
    with ctx.db.transaction() as conn:
        phones = _purge_phones(ctx, conn, now, events)
        clips = _purge_clips(ctx, conn, now, errors)
        missed = _purge_missed_calls(ctx, conn, now_dt, now)
        orphans = _sweep_orphan_clips(ctx, conn, now_dt, now, errors)
        conn.execute(
            "INSERT INTO kv(key, value) VALUES ('last_purge_run', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now,),
        )
    if phones or clips or missed or orphans:
        try:
            _scrub_database(ctx)
        except sqlite3.Error as e:  # the rows are gone either way; the free pages are tried again next time
            errors.append(f"database file not rewritten: {e}")
            log.error("could not rewrite the database file after a purge: %s", e)
    if phones or clips or missed or orphans or errors:
        events.append(("system", {"check": "purge", "ok": not errors}))
    storage = storage_status(ctx)
    if storage.ok != ctx.extras.get("storage_ok", True):
        events.append(("system", {"check": "storage", "ok": storage.ok}))
    ctx.extras["storage_ok"] = storage.ok
    emit(ctx, events)
    return PurgeReport(
        ran_at=now, phones=phones, clips=clips, missed_calls=missed, orphan_clips=orphans, errors=errors
    )


def retention_status(ctx: AppContext) -> dict[str, Any]:
    """For the admin page: when the job last ran, what waits, what has been deleted so far."""
    now_dt = clock.now()
    now = clock.iso(now_dt)
    cutoff = clock.iso(now_dt - timedelta(hours=ctx.policy["retention"]["unlinked_missed_call_hours"]))

    def count(sql: str, params: tuple = ()) -> int:
        return ctx.db.one(sql, params)[0]

    last = ctx.db.one("SELECT value FROM kv WHERE key='last_purge_run'")
    done = ",".join("?" * len(_DONE_STATUSES))
    deleted = {
        r["what"]: r["n"] for r in ctx.db.query("SELECT what, COUNT(*) AS n FROM purge_log GROUP BY what")
    }
    return {
        "last_run": last["value"] if last else None,
        "pending": {
            "phones": count("SELECT COUNT(*) FROM contacts"),
            "phones_due": count(
                "SELECT COUNT(*) FROM contacts WHERE purge_after IS NOT NULL AND purge_after <= ?", (now,)
            ),
            "clips": count(
                "SELECT COUNT(*) FROM place_captures WHERE clip_deleted_at IS NULL AND audio_path IS NOT NULL"
            ),
            "clips_due": count(
                "SELECT COUNT(*) FROM place_captures pc JOIN bite_reports r ON r.id = pc.report_id"
                " WHERE pc.clip_deleted_at IS NULL AND (pc.expires_at <= ? OR pc.decided_at IS NOT NULL"
                f" OR r.location_status IN ({done}))",
                (now, *_DONE_STATUSES),
            ),
            "missed_calls": count("SELECT COUNT(*) FROM missed_calls WHERE phone_cipher IS NOT NULL"),
            "missed_calls_due": count(
                "SELECT COUNT(*) FROM missed_calls WHERE status IN ('unlinked','duplicate') AND phone_cipher IS NOT NULL"
                " AND at <= ?",
                (cutoff,),
            ),
        },
        "deleted": {
            "phone": deleted.get("phone", 0),
            "clip": deleted.get("clip", 0),
            "missed_call": deleted.get("missed_call", 0),
        },
        "rules": {
            "phone_purge_days_after_last_dose": ctx.policy["retention"]["phone_purge_days_after_last_dose"],
            "clip_max_age_days": ctx.policy["retention"]["clip_max_age_days"],
            "unlinked_missed_call_hours": ctx.policy["retention"]["unlinked_missed_call_hours"],
        },
    }
