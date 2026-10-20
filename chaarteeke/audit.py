"""Append-only audit log. Every state change and every phone reveal writes one row."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import clock


def record(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    object_type: str | None = None,
    object_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write inside the caller's transaction (pass the transaction's connection)."""
    conn.execute(
        "INSERT INTO audit(at, actor, action, object_type, object_id, detail_json) VALUES (?,?,?,?,?,?)",
        (
            clock.iso(clock.now()),
            actor,
            action,
            object_type,
            object_id,
            json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
        ),
    )
