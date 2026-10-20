"""The first administrator of a pilot server: made on the command line, because nobody can sign in to make one.

`chaar-teeke create-admin` asks for a username and a password (twice, not shown, never printed or logged),
refuses once any administrator exists (further people are added in the app by an administrator), and writes
an audit row that names the new login and that the command line made it.
"""

from __future__ import annotations

import re

from .. import audit
from ..context import AppContext
from ..ids import new_id
from . import auth

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
CLI_ACTOR = "system:cli"


class SetupError(Exception):
    """A refusal with plain words for the person at the terminal."""


def admin_exists(ctx: AppContext) -> bool:
    return ctx.db.one("SELECT 1 FROM staff WHERE role = 'admin' LIMIT 1") is not None


def create_first_admin(ctx: AppContext, username: str, password: str) -> str:
    """Add the first administrator and return their staff id. Refuses when any administrator exists."""
    username = username.strip()
    if not USERNAME.match(username):
        raise SetupError("A username is 3 to 32 lower-case letters, digits, dots, dashes or underscores.")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        raise SetupError(f"A password needs at least {auth.MIN_PASSWORD_LENGTH} characters.")
    staff_id = new_id("stf")
    pw_hash = auth.hash_password(password)
    with (
        ctx.db.transaction() as conn
    ):  # the check and the insert in one step, so two terminals cannot both pass
        if conn.execute("SELECT 1 FROM staff WHERE role = 'admin' LIMIT 1").fetchone():
            raise SetupError(
                "An administrator already exists. Sign in to the app as an administrator to add people."
            )
        if conn.execute("SELECT 1 FROM staff WHERE username = ?", (username,)).fetchone():
            raise SetupError("Someone already has that username.")
        conn.execute(
            "INSERT INTO staff(id, username, display_name, role, clinic_id, password_hash, active, is_demo)"
            " VALUES (?,?,?,'admin',NULL,?,1,?)",
            (staff_id, username, "Administrator", pw_hash, 1 if ctx.settings.is_demo else 0),
        )
        audit.record(
            conn,
            CLI_ACTOR,
            "staff.create",
            "staff",
            staff_id,
            {"username": username, "role": "admin", "via": "create-admin"},
        )
    return staff_id
