"""The audit log can be read by the administrator, and nothing can change or remove a row of it."""

from __future__ import annotations

import sqlite3

import pytest

from chaarteeke import audit, clock, enrol
from chaarteeke.db import MIGRATIONS, Database


def put(ctx, actor, action, object_type=None, object_id=None, detail=None):
    with ctx.db.transaction() as conn:
        audit.record(conn, actor, action, object_type, object_id, detail)


def log(client, who, **query) -> dict:
    res = client.get("/api/admin/audit", params=query, headers=who.read)
    assert res.status_code == 200, res.text
    return res.json()


# ------------------------------------------------------------------ the route
def test_the_administrator_reads_the_log_newest_first_a_page_at_a_time(client, ctx, people):
    for n in range(5):
        put(ctx, "staff:clinic", "dose.given", "course", f"crs_{n}", {"dose_no": n})
    page = log(client, people["admin"], limit=2)
    assert [r["object_id"] for r in page["rows"]] == ["crs_4", "crs_3"]
    assert page["rows"][0]["detail"] == {"dose_no": 4}
    assert page["rows"][0].keys() == {"seq", "at", "actor", "action", "object_type", "object_id", "detail"}
    assert page["total"] >= 5 and page["limit"] == 2 and page["offset"] == 0
    second = log(client, people["admin"], limit=2, offset=2)
    assert [r["object_id"] for r in second["rows"]] == ["crs_2", "crs_1"]
    seqs = [r["seq"] for r in page["rows"] + second["rows"]]
    assert seqs == sorted(seqs, reverse=True)


def test_the_log_can_be_filtered_by_action_object_and_actor(client, ctx, people):
    put(ctx, "staff:clinic", "dose.given", "course", "crs_a")
    put(ctx, "staff:clinic", "dose.unmarked", "course", "crs_a")
    put(ctx, "staff:vet", "alert.claim", "alert", "alr_1")
    admin = people["admin"]
    assert {r["action"] for r in log(client, admin, action="dose.given")["rows"]} == {"dose.given"}
    assert {r["action"] for r in log(client, admin, action="dose.")["rows"]} == {
        "dose.given",
        "dose.unmarked",
    }
    assert [r["action"] for r in log(client, admin, object_type="alert", object_id="alr_1")["rows"]] == [
        "alert.claim"
    ]
    mine = log(client, admin, actor="staff:vet")
    assert {r["actor"] for r in mine["rows"]} == {"staff:vet"} and mine["total"] == len(mine["rows"])
    assert log(client, admin, action="nothing.like.this") == {
        "rows": [], "total": 0, "limit": 50, "offset": 0, "actions": mine["actions"],
    }  # fmt: skip


def test_phone_actions_come_first_when_asked_and_head_the_list_of_actions(client, ctx, people):
    put(ctx, "staff:clinic", "phone.reveal", "course", "crs_1")
    put(ctx, "staff:clinic", "phone.reveal_refused", "course", "crs_2", {"why": "purged"})
    put(ctx, "staff:clinic", "dose.given", "course", "crs_3")
    put(ctx, "staff:clinic", "dose.given", "course", "crs_4")
    admin = people["admin"]
    plain = log(client, admin, limit=2)
    assert [r["action"] for r in plain["rows"]] == ["dose.given", "dose.given"]
    first = log(client, admin, limit=3, phone_first=True)
    assert [r["action"] for r in first["rows"]][:2] == ["phone.reveal_refused", "phone.reveal"]
    assert first["rows"][2]["action"] == "dose.given"
    names = [a["action"] for a in first["actions"]]
    assert names[:2] == ["phone.reveal", "phone.reveal_refused"]  # the menu starts with them
    assert {"action": "dose.given", "count": 2} in first["actions"]


def test_the_log_never_carries_a_phone_number(client, ctx, people):
    got = enrol.record_missed_call(ctx, "9876543210", "ari", clock.now())
    course = client.post(
        "/api/courses",
        json={"clinic_id": "cln_gmc", "register_no": "R-9", "regimen_id": "id_trc", "lang": "hi",
              "day0": clock.local_today(ctx.settings.local_tz).isoformat(), "eligibility": "adult"},
        headers=people["clinic"].write,
    ).json()["id"]  # fmt: skip
    client.post(
        f"/api/missed-calls/{got.missed_call_id}/link",
        json={"course_id": course},
        headers=people["clinic"].write,
    )
    assert (
        client.post(f"/api/courses/{course}/reveal-phone", headers=people["clinic"].write).status_code == 200
    )
    everything = client.get("/api/admin/audit", params={"limit": 200}, headers=people["admin"].read).text
    assert "9876543210" not in everything and "+919876543210" not in everything
    shown = log(client, people["admin"], action="phone.reveal")["rows"]
    assert [(r["actor"], r["object_id"]) for r in shown] == [("staff:clinic", course)]


def test_a_page_size_beyond_the_most_is_refused(client, people):
    assert client.get("/api/admin/audit?limit=201", headers=people["admin"].read).status_code == 400
    assert client.get("/api/admin/audit?limit=0", headers=people["admin"].read).status_code == 400
    assert client.get("/api/admin/audit?offset=-1", headers=people["admin"].read).status_code == 400
    assert client.get("/api/admin/audit?limit=200", headers=people["admin"].read).status_code == 200


def test_only_an_administrator_reads_the_log(client, people):
    assert client.get("/api/admin/audit", headers=people["out"].read).status_code == 401
    for role in ("clinic", "abc", "vet", "oversight", "reviewer"):
        assert client.get("/api/admin/audit", headers=people[role].read).status_code == 403, role


# -------------------------------------------------------- append-only in the database
def test_a_row_of_the_log_cannot_be_changed_or_removed(ctx):
    put(ctx, "staff:clinic", "dose.given", "course", "crs_x")
    seq = ctx.db.one("SELECT MAX(seq) AS s FROM audit")["s"]
    conn = ctx.db.connect()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit SET actor = 'staff:nobody' WHERE seq = ?", (seq,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit WHERE seq = ?", (seq,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with ctx.db.transaction() as c:  # nor inside a transaction of the app
            c.execute("UPDATE audit SET detail_json = NULL")
    row = ctx.db.one("SELECT actor FROM audit WHERE seq = ?", (seq,))
    assert row["actor"] == "staff:clinic"
    put(ctx, "staff:clinic", "dose.given", "course", "crs_y")  # adding is the one thing allowed
    assert ctx.db.one("SELECT MAX(seq) AS s FROM audit")["s"] == seq + 1


def test_a_database_made_before_the_triggers_gets_them_when_it_is_opened(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    for name in ("0001_init.sql", "0002_clinic_contact.sql"):  # the schema as it was at version 2
        raw.executescript((MIGRATIONS / name).read_text())
    raw.execute(
        "INSERT INTO audit(at, actor, action) VALUES ('2026-10-19T00:00:00.000Z', 'staff:a', 'old.row')"
    )
    raw.commit()
    assert raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
    raw.close()
    db = Database(path)
    assert db.migrate() >= 3
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"audit_no_update", "audit_no_delete"} <= names
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.connect().execute("DELETE FROM audit")
    assert db.one("SELECT action FROM audit")["action"] == "old.row"  # the old row is kept
    assert db.migrate() >= 3  # and opening it again changes nothing
    db.close()


def test_the_demo_reset_leaves_the_log_in_place_and_adds_to_it(client, ctx, people):
    put(ctx, "staff:clinic", "dose.given", "course", "crs_before_reset")
    before = ctx.db.one("SELECT COUNT(*) AS n FROM audit")["n"]
    res = client.post("/api/demo/reset", headers=people["admin"].write)
    assert res.status_code == 200, res.text
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE object_id = 'crs_before_reset'")["n"] == 1
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit")["n"] > before  # the reset itself was logged
