"""The place reviewer over HTTP: the queue, the audio, the decision and the learned names."""

from __future__ import annotations

from api_support import wav_bytes

from chaarteeke import clock


def put_clip(ctx, report_id: str) -> None:
    """Give a queued report a real file under var/clips, as a finished call would."""
    clips = ctx.settings.var_dir / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    path = clips / f"{report_id}.wav"
    path.write_bytes(wav_bytes(0.2))
    with ctx.db.transaction() as conn:
        conn.execute(
            "UPDATE place_captures SET audio_path = ? WHERE report_id = ?", (f"clips/{path.name}", report_id)
        )


def test_next_gives_the_oldest_waiting_recording(client, people):
    res = client.get("/api/review/next", headers=people["reviewer"].read)
    assert res.status_code == 200
    item = res.json()
    assert item["remaining"] >= 1 and item["report_id"]
    assert set(item) >= {"report_id", "heard_at", "transcript", "asr_status", "candidates", "audio_available", "expires_at"}  # fmt: skip


def test_next_is_null_when_nothing_waits(client, ctx, people):
    with ctx.db.transaction() as conn:
        conn.execute(
            "UPDATE bite_reports SET location_status = 'cant_tell' WHERE location_status = 'needs_review'"
        )
    res = client.get("/api/review/next", headers=people["reviewer"].read)
    assert res.status_code == 200 and res.json() is None


def test_the_audio_is_served_to_the_reviewer_and_each_listen_is_logged(client, ctx, people):
    report = client.get("/api/review/next", headers=people["reviewer"].read).json()["report_id"]
    put_clip(ctx, report)
    res = client.get(f"/api/review/{report}/audio", headers=people["reviewer"].read)
    assert res.status_code == 200 and res.headers["content-type"] == "audio/wav"
    assert res.content[:4] == b"RIFF" and res.headers["cache-control"] == "no-store"
    row = ctx.db.one("SELECT actor FROM audit WHERE action = 'clip.listen' AND object_id = ?", (report,))
    assert row["actor"] == "staff:reviewer"


def test_the_administrator_cannot_hear_a_recording(client, ctx, people):
    report = client.get("/api/review/next", headers=people["reviewer"].read).json()["report_id"]
    put_clip(ctx, report)
    assert client.get(f"/api/review/{report}/audio", headers=people["admin"].read).status_code == 403


def test_a_recording_that_is_gone_says_so(client, ctx, people):
    report = client.get("/api/review/next", headers=people["reviewer"].read).json()["report_id"]
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE place_captures SET clip_deleted_at = ?, audio_path = NULL WHERE report_id = ?", (clock.iso(clock.now()), report))  # fmt: skip
    res = client.get(f"/api/review/{report}/audio", headers=people["reviewer"].read)
    assert res.status_code == 410 and res.json()["error"] == "no_audio"


def test_deciding_a_ward_removes_the_clip_and_shrinks_the_queue(client, ctx, people, wards):
    reviewer = people["reviewer"]
    first = client.get("/api/review/next", headers=reviewer.read).json()
    put_clip(ctx, first["report_id"])
    res = client.post(
        f"/api/review/{first['report_id']}/decide", json={"ward_no": wards[0]}, headers=reviewer.write
    )
    assert res.status_code == 200 and res.json() == {"ok": True, "remaining": first["remaining"] - 1}
    row = ctx.db.one(
        "SELECT ward_no, location_status, cluster_id FROM bite_reports WHERE id = ?", (first["report_id"],)
    )
    assert (row["ward_no"], row["location_status"]) == (wards[0], "decided") and row["cluster_id"]
    assert not (ctx.settings.var_dir / "clips" / f"{first['report_id']}.wav").exists()
    again = client.post(
        f"/api/review/{first['report_id']}/decide", json={"cant_tell": True}, headers=reviewer.write
    )
    assert again.status_code == 409 and again.json()["error"] == "already_decided"


def test_a_decision_is_exactly_one_of_ward_outside_or_cannot_tell(client, people, wards):
    reviewer = people["reviewer"]
    report = client.get("/api/review/next", headers=reviewer.read).json()["report_id"]
    for body in (
        {},
        {"ward_no": wards[0], "cant_tell": True},
        {"ward_no": wards[0], "outside_tehsil": "Samba"},
    ):
        res = client.post(f"/api/review/{report}/decide", json=body, headers=reviewer.write)
        assert res.status_code == 400 and res.json()["error"] == "bad_decision", body
    unknown = client.post(f"/api/review/{report}/decide", json={"ward_no": 9999}, headers=reviewer.write)
    assert unknown.status_code == 404 and unknown.json()["error"] == "bad_ward"
    alone = client.post(f"/api/review/{report}/decide", json={"cant_tell": True, "remember_name": True}, headers=reviewer.write)  # fmt: skip
    assert alone.status_code == 400


def test_outside_the_city_and_cannot_tell(client, ctx, people):
    reviewer = people["reviewer"]
    first = client.get("/api/review/next", headers=reviewer.read).json()["report_id"]
    client.post(f"/api/review/{first}/decide", json={"outside_tehsil": "Samba"}, headers=reviewer.write)
    second = client.get("/api/review/next", headers=reviewer.read).json()["report_id"]
    client.post(f"/api/review/{second}/decide", json={"cant_tell": True}, headers=reviewer.write)
    rows = {
        r["id"]: r for r in ctx.db.query("SELECT * FROM bite_reports WHERE id IN (?, ?)", (first, second))
    }
    assert (rows[first]["location_status"], rows[first]["outside_tehsil"]) == ("outside_jmc", "Samba")
    assert rows[second]["location_status"] == "cant_tell"


def test_remembering_a_name_adds_it_to_the_learned_names(client, ctx, people, wards):
    reviewer = people["reviewer"]
    item = client.get("/api/review/next", headers=reviewer.read).json()
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE place_captures SET transcript = 'mera ghar rani bagh ke paas hai' WHERE report_id = ?", (item["report_id"],))  # fmt: skip
    res = client.post(f"/api/review/{item['report_id']}/decide", json={"ward_no": wards[2], "remember_name": True}, headers=reviewer.write)  # fmt: skip
    assert res.status_code == 200
    learned = client.get("/api/aliases", headers=reviewer.read).json()
    assert any("rani" in a["text"] and a["ward_no"] == wards[2] and a["ward_name"] for a in learned)


def test_a_name_can_be_taught_directly_once(client, ctx, people, wards):
    reviewer = people["reviewer"]
    body = {"text": "  Rani Bagh  ", "ward_no": wards[3]}
    first = client.post("/api/aliases", json=body, headers=reviewer.write)
    assert first.status_code == 200
    mine = [a for a in first.json() if a["ward_no"] == wards[3] and "rani" in a["text"]]
    assert len(mine) == 1 and mine[0]["added_by"] == "staff:reviewer"
    again = client.post("/api/aliases", json=body, headers=reviewer.write)
    assert len([a for a in again.json() if a["ward_no"] == wards[3] and "rani" in a["text"]]) == 1
    assert ctx.db.one("SELECT COUNT(*) AS n FROM audit WHERE action = 'alias.add'")["n"] == 1


def test_a_name_for_a_ward_that_is_not_there_or_with_no_place_word_is_refused(client, people, wards):
    reviewer = people["reviewer"].write
    assert (
        client.post("/api/aliases", json={"text": "rani bagh", "ward_no": 9999}, headers=reviewer).status_code
        == 404
    )
    empty = client.post("/api/aliases", json={"text": "ke paas", "ward_no": wards[0]}, headers=reviewer)
    assert empty.status_code == 400 and empty.json()["error"] == "bad_request"


def test_an_item_that_never_had_a_recording_does_not_claim_it_was_deleted(client, ctx, people):
    report = client.get("/api/review/next", headers=people["reviewer"].read).json()["report_id"]
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE place_captures SET audio_path = NULL, clip_deleted_at = NULL WHERE report_id = ?", (report,))  # fmt: skip
    res = client.get(f"/api/review/{report}/audio", headers=people["reviewer"].read)
    assert res.status_code == 410 and res.json()["error"] == "no_audio"
    assert "deleted" not in res.json()["message"]


def waiting(ctx) -> list[str]:
    return [
        r["id"]
        for r in ctx.db.query(
            "SELECT r.id FROM bite_reports r JOIN place_captures pc ON pc.report_id = r.id"
            " WHERE r.location_status = 'needs_review' AND pc.clip_deleted_at IS NULL"
            " ORDER BY r.created_at, r.id"
        )
    ]


def test_skip_gives_the_kth_oldest_waiting_recording_and_remaining_stays(client, ctx, people):
    ids = waiting(ctx)
    assert len(ids) >= 2
    read = people["reviewer"].read
    first = client.get("/api/review/next", headers=read).json()
    assert (
        first["report_id"]
        == ids[0]
        == client.get("/api/review/next?skip=0", headers=read).json()["report_id"]
    )
    for k in range(len(ids)):
        item = client.get(f"/api/review/next?skip={k}", headers=read).json()
        assert item["report_id"] == ids[k] and item["remaining"] == len(ids)


def test_skip_past_the_end_is_null_and_a_bad_skip_is_refused(client, ctx, people):
    read = people["reviewer"].read
    res = client.get(f"/api/review/next?skip={len(waiting(ctx))}", headers=read)
    assert res.status_code == 200 and res.json() is None
    for bad in ("-1", "x", "100001", "99999999999999999999"):
        res = client.get(f"/api/review/next?skip={bad}", headers=read)
        assert res.status_code == 400 and res.json()["error"] == "bad_request"
