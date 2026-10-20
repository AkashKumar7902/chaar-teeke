"""Database.close() closes every connection the database opened, not only the calling thread's.

Found on Ubuntu 24.04 (deploy test): `chaar-teeke preflight` crashed in about two runs in three with
"OSError: Directory not empty" while deleting its throwaway folder. The first call runs part of its work in a
worker thread (asyncio.to_thread), that thread opened its own connection to the throwaway database, and
close() left it open, so SQLite's -wal and -shm files were still being used while the folder was deleted.
"""

from __future__ import annotations

import threading

from chaarteeke.db import Database


def _in_thread(fn):
    t = threading.Thread(target=fn)
    t.start()
    t.join()


def test_close_closes_the_connections_other_threads_opened(tmp_path):
    db = Database(tmp_path / "x.db")
    db.migrate()
    _in_thread(lambda: db.one("SELECT 1"))
    db.one("SELECT 1")
    assert (tmp_path / "x.db-wal").exists()  # two open connections: the WAL files are in use
    db.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.db"]  # the last close removes -wal and -shm


def test_the_database_opens_again_after_close_in_every_thread(tmp_path):
    db = Database(tmp_path / "x.db")
    db.migrate()
    seen: list[int] = []
    release = threading.Event()
    closed = threading.Event()

    def worker():
        db.one("SELECT 1")  # this thread now holds a connection
        closed.wait(5)
        seen.append(db.one("SELECT 42")[0])  # after close(), the same thread opens a fresh one
        release.set()

    t = threading.Thread(target=worker)
    t.start()
    while not (tmp_path / "x.db-wal").exists():  # wait until the worker has opened its connection
        threading.Event().wait(0.01)
    db.close()
    closed.set()
    t.join()
    assert seen == [42]
    db.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.db"]
