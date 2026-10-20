"""SQLite access: one file, WAL, foreign keys on, migrations applied in order."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"


class Database:
    """A thin wrapper. Each thread gets its own connection; writes go through transaction()."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._memory_conn: sqlite3.Connection | None = None
        # Every connection any thread opened, so close() can close them all (worker threads open their own).
        self._opened: list[sqlite3.Connection] = []
        self._opened_lock = threading.Lock()
        self._generation = 0  # bumped by close(): a thread's remembered connection from before is not used

    def connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = self._open()
            return self._memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None or getattr(self._local, "generation", -1) != self._generation:
            conn = self._open()
            with self._opened_lock:
                self._opened.append(conn)
                self._local.generation = self._generation
            self._local.conn = conn
        return conn

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def migrate(self) -> int:
        conn = self.connect()
        have = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        current = 0
        if have:
            current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        for f in sorted(MIGRATIONS.glob("*.sql")):
            n = int(f.name.split("_", 1)[0])
            if n > current:
                with self._write_lock:
                    conn.executescript(f.read_text())
                current = n
        return current

    def close(self) -> None:
        """Close the connection of every thread, so SQLite can remove the -wal and -shm files."""
        with self._opened_lock:
            opened, self._opened = self._opened, []
            self._generation += 1
        for conn in opened:
            conn.close()
        self._local.conn = None
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None
