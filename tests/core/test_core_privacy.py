"""Purges of phones, clips and unlinked missed calls, the purge log and the retention status."""

from __future__ import annotations

from datetime import timedelta

from chaarteeke import clock, enrol, privacy


def make_capture(
    ctx, world, report_id, *, location_status, expires_in_days=7, decided=False, name=None, path=None
):
    """A bite report with a place clip on disk, as the call runner and the review lane leave it."""
    world.clinic()
    clips = ctx.settings.var_dir / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    file = clips / (name or f"{report_id}.wav")
    file.write_bytes(b"RIFF....WAVE")
    now = clock.now()
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bite_reports(id, clinic_id, bite_week, location_status, source, created_at)"
            " VALUES (?, 'cln_a', '2026-10-19', ?, 'clinic', ?)",
            (report_id, location_status, clock.iso(now)),
        )
        conn.execute(
            "INSERT INTO place_captures(report_id, audio_path, transcript, candidates_json, asr_status,"
            " decided_at, expires_at) VALUES (?,?,?,?, 'done', ?, ?)",
            (
                report_id,
                path or str(file),
                "near the old bus stand",
                "[]",
                clock.iso(now) if decided else None,
                clock.iso(now + timedelta(days=expires_in_days)),
            ),
        )
    return file


def capture(ctx, report_id):
    return ctx.db.one("SELECT * FROM place_captures WHERE report_id=?", (report_id,))


# -------------------------------------------------------------------- phones
def test_phones_past_purge_after_are_deleted(ctx, world, fixed_clock, events):
    old = world.active(regimen="reexposure", number="+919876543210")  # purge on day 10
    keep = world.active(regimen="id_trc", number="+919876543211")  # purge on day 35
    fixed_clock.advance(timedelta(days=9))
    assert privacy.run_purges(ctx).phones == 0
    fixed_clock.advance(timedelta(days=1))
    events()
    report = privacy.run_purges(ctx)
    assert (report.phones, report.clips, report.missed_calls, report.errors) == (1, 0, 0, [])
    assert ctx.db.one("SELECT 1 FROM contacts WHERE course_id=?", (old,)) is None
    assert ctx.db.one("SELECT 1 FROM contacts WHERE course_id=?", (keep,)) is not None
    log = ctx.db.one("SELECT * FROM purge_log WHERE what='phone'")
    assert (log["course_id"], log["reason"], log["purged_at"]) == (old, "retention", clock.iso(clock.now()))
    assert "phone.purge" in world.audit_actions()
    assert enrol.get_course(ctx, old).phone_purged is True
    # the course whose window is over is closed so it stops showing up as missed
    assert enrol.get_course(ctx, old).status == "stopped"
    assert [c["status"] for c in world.calls(old)] == ["cancelled"]
    assert ("course", {"course_id": old, "status": "stopped"}) in events()
    assert all("98765" not in str(d) for _, d in events())


def test_a_completed_course_keeps_its_status_when_the_number_goes(ctx, world, fixed_clock):
    from chaarteeke import doses

    cid = world.active(regimen="reexposure", day0=world.today() - timedelta(days=3))
    doses.mark_given(ctx, cid, 2, "clinic", world.today(), "staff:a")
    fixed_clock.advance(timedelta(days=8))
    privacy.run_purges(ctx)
    assert enrol.get_course(ctx, cid).status == "completed"


def test_a_second_run_finds_nothing(ctx, world, fixed_clock):
    world.active(regimen="reexposure")
    fixed_clock.advance(timedelta(days=11))
    assert privacy.run_purges(ctx).phones == 1
    again = privacy.run_purges(ctx)
    assert (again.phones, again.clips, again.missed_calls) == (0, 0, 0)
    assert ctx.db.one("SELECT COUNT(*) FROM purge_log")[0] == 1


def test_last_purge_run_is_remembered(ctx, fixed_clock):
    assert privacy.retention_status(ctx)["last_run"] is None
    privacy.run_purges(ctx)
    assert ctx.db.one("SELECT value FROM kv WHERE key='last_purge_run'")["value"] == clock.iso(clock.now())
    fixed_clock.advance(timedelta(minutes=5))
    privacy.run_purges(ctx)
    assert privacy.retention_status(ctx)["last_run"] == clock.iso(clock.now())


# --------------------------------------------------------------------- clips
def test_expired_clip_is_deleted_and_leaves_the_review_queue(ctx, world, fixed_clock):
    file = make_capture(ctx, world, "rep_old", location_status="needs_review", expires_in_days=7)
    fixed_clock.advance(timedelta(days=7, seconds=1))
    report = privacy.run_purges(ctx)
    assert report.clips == 1 and not file.exists()
    row = capture(ctx, "rep_old")
    assert row["clip_deleted_at"] == clock.iso(clock.now())
    assert row["audio_path"] is None and row["transcript"] is None and row["candidates_json"] is None
    # nobody placed it in time: it is a place we could not tell, not a pending item
    assert ctx.db.one("SELECT location_status FROM bite_reports WHERE id='rep_old'")[0] == "cant_tell"
    log = ctx.db.one("SELECT * FROM purge_log WHERE what='clip'")
    assert log["reason"] == "expired"


def test_clip_not_yet_expired_and_undecided_is_kept(ctx, world, fixed_clock):
    file = make_capture(ctx, world, "rep_wait", location_status="needs_review", expires_in_days=7)
    fixed_clock.advance(timedelta(days=6))
    assert privacy.run_purges(ctx).clips == 0
    assert file.exists() and capture(ctx, "rep_wait")["clip_deleted_at"] is None
    pending = make_capture(ctx, world, "rep_pending", location_status="pending")
    privacy.run_purges(ctx)
    assert pending.exists()


def test_decided_and_matched_clips_go_at_once(ctx, world):
    decided = make_capture(ctx, world, "rep_dec", location_status="decided", decided=True)
    matched = make_capture(ctx, world, "rep_match", location_status="matched")
    outside = make_capture(ctx, world, "rep_out", location_status="outside_jmc")
    review = make_capture(ctx, world, "rep_rev", location_status="needs_review", decided=False)
    report = privacy.run_purges(ctx)
    assert report.clips == 3
    assert not decided.exists() and not matched.exists() and not outside.exists() and review.exists()
    assert {r["reason"] for r in ctx.db.query("SELECT reason FROM purge_log WHERE what='clip'")} == {
        "decided"
    }
    # a decided report keeps its place
    assert ctx.db.one("SELECT location_status FROM bite_reports WHERE id='rep_dec'")[0] == "decided"
    assert capture(ctx, "rep_dec")["transcript"] is None


def test_a_clip_the_review_lane_already_deleted_is_closed_out(ctx, world):
    file = make_capture(ctx, world, "rep_gone", location_status="decided", decided=True)
    file.unlink()
    assert privacy.run_purges(ctx).clips == 1
    assert capture(ctx, "rep_gone")["clip_deleted_at"] is not None


def test_relative_clip_paths_resolve_under_var(ctx, world):
    file = make_capture(
        ctx, world, "rep_rel", location_status="decided", decided=True, path="clips/rep_rel.wav"
    )
    assert privacy.run_purges(ctx).clips == 1 and not file.exists()


def test_a_clip_path_outside_var_clips_is_refused_and_reported(ctx, world, tmp_path):
    outside = tmp_path / "precious.wav"
    outside.write_bytes(b"x")
    make_capture(ctx, world, "rep_bad", location_status="decided", decided=True, path=str(outside))
    report = privacy.run_purges(ctx)
    assert report.clips == 0 and outside.exists()
    assert len(report.errors) == 1 and "outside" in report.errors[0]
    assert capture(ctx, "rep_bad")["clip_deleted_at"] is None


# --------------------------------------------------------------- missed calls
def test_unlinked_missed_call_expires_after_the_policy_hours(ctx, world, fixed_clock):
    old = enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    fixed_clock.advance(timedelta(hours=47))
    fresh = enrol.record_missed_call(ctx, "+919876543211", "ari", clock.now())
    assert privacy.run_purges(ctx).missed_calls == 0
    fixed_clock.advance(timedelta(hours=1, seconds=1))
    report = privacy.run_purges(ctx)
    assert report.missed_calls == 1
    row = ctx.db.one("SELECT * FROM missed_calls WHERE id=?", (old.missed_call_id,))
    assert (row["status"], row["phone_cipher"]) == ("expired", None)
    kept = ctx.db.one("SELECT * FROM missed_calls WHERE id=?", (fresh.missed_call_id,))
    assert (kept["status"], kept["phone_cipher"] is not None) == ("unlinked", True)
    assert ctx.db.one("SELECT reason FROM purge_log WHERE what='missed_call'")["reason"] == "expired"
    # an expired number is a new missed call if it rings again
    assert enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now()).status == "unlinked"


def test_a_linked_row_holds_no_number_and_a_repeat_call_holds_it_for_48_hours_only(ctx, world, fixed_clock):
    world.active(number="+919876543210")
    enrol.record_missed_call(ctx, "+919876543210", "ari", clock.now())
    rows = ctx.db.query("SELECT status, phone_cipher IS NOT NULL AS held FROM missed_calls")
    # the repeat call keeps the number the course already holds, so staff can link it to another entry
    assert {(r["status"], r["held"]) for r in rows} == {("linked", 0), ("duplicate", 1)}
    assert privacy.run_purges(ctx).missed_calls == 0
    fixed_clock.advance(timedelta(hours=49))
    assert privacy.run_purges(ctx).missed_calls == 1
    row = ctx.db.one("SELECT status, phone_cipher, phone_last4 FROM missed_calls WHERE status='duplicate'")
    assert (row["phone_cipher"], row["phone_last4"]) == (None, None)  # not even the last four digits stay


def test_an_expired_unlinked_call_loses_its_last_four_digits_too(ctx, fixed_clock):
    res = enrol.record_missed_call(ctx, "+919888888888", "ari", clock.now())
    assert ctx.db.one("SELECT phone_last4 FROM missed_calls WHERE id=?", (res.missed_call_id,))[0] == "8888"
    fixed_clock.advance(timedelta(hours=49))
    privacy.run_purges(ctx)
    row = ctx.db.one(
        "SELECT status, phone_cipher, phone_last4 FROM missed_calls WHERE id=?", (res.missed_call_id,)
    )
    assert (row["status"], row["phone_cipher"], row["phone_last4"]) == ("expired", None, None)


def test_the_last_four_digits_of_a_linked_call_go_with_the_number(ctx, world, fixed_clock):
    cid = world.active(number="+919876543210", regimen="reexposure")
    assert ctx.db.one("SELECT phone_last4 FROM missed_calls WHERE course_id=?", (cid,))[0] == "3210"
    fixed_clock.advance(timedelta(days=11))
    privacy.run_purges(ctx)
    assert ctx.db.one("SELECT phone_last4 FROM missed_calls WHERE course_id=?", (cid,))[0] is None


# ------------------------------------------------------------ retention status
def test_retention_status_counts(ctx, world, fixed_clock):
    world.active(regimen="reexposure", number="+919876543210")
    world.active(regimen="id_trc", number="+919876543211")
    enrol.record_missed_call(ctx, "+919876543212", "ari", clock.now())
    make_capture(ctx, world, "rep_a", location_status="decided", decided=True)
    make_capture(ctx, world, "rep_b", location_status="needs_review")
    s = privacy.retention_status(ctx)
    assert s["last_run"] is None
    assert s["pending"] == {
        "phones": 2,
        "phones_due": 0,
        "clips": 2,
        "clips_due": 1,
        "missed_calls": 1,
        "missed_calls_due": 0,
    }
    assert s["deleted"] == {"phone": 0, "clip": 0, "missed_call": 0}
    assert s["rules"] == {
        "phone_purge_days_after_last_dose": 7,
        "clip_max_age_days": 7,
        "unlinked_missed_call_hours": 48,
    }
    fixed_clock.advance(timedelta(days=11))
    privacy.run_purges(ctx)
    s = privacy.retention_status(ctx)
    assert s["pending"]["phones"] == 1 and s["pending"]["clips"] == 0 and s["pending"]["missed_calls"] == 0
    assert s["deleted"] == {"phone": 1, "clip": 2, "missed_call": 1}
    assert s["last_run"] == clock.iso(clock.now())


# ------------------------------------------------- the demo's shared example recordings
def _shipped_clip(ctx, tmp_path, world, report_id):
    """A recording that ships with the demo (shared by every demo), referenced by a relative path."""
    data = tmp_path / "data"
    (data / "demo" / "clips").mkdir(parents=True)
    shared = data / "demo" / "clips" / "1.wav"
    shared.write_bytes(b"RIFF....WAVE")
    ctx.settings = ctx.settings.model_copy(update={"data_dir": data})
    make_capture(ctx, world, report_id, location_status="decided", decided=True, path="data/demo/clips/1.wav")
    return shared


def test_a_shipped_example_clip_is_let_go_but_never_deleted(ctx, world, tmp_path):
    shared = _shipped_clip(ctx, tmp_path, world, "rep_shared")
    report = privacy.run_purges(ctx)
    assert report.errors == [] and report.clips == 1
    assert shared.exists()  # other demos still play it
    assert capture(ctx, "rep_shared")["clip_deleted_at"] is not None
    assert capture(ctx, "rep_shared")["audio_path"] is None


def test_a_withdrawal_lets_go_of_a_shipped_example_clip_without_deleting_it(ctx, world, tmp_path):
    cid = world.active()
    shared = _shipped_clip(ctx, tmp_path, world, "rep_w")
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE bite_reports SET course_id=? WHERE id='rep_w'", (cid,))
        conn.execute("UPDATE place_captures SET decided_at=NULL WHERE report_id='rep_w'")
    enrol.withdraw(ctx, cid, f"patient:{cid}", "key 9")
    assert shared.exists() and capture(ctx, "rep_w")["audio_path"] is None
    assert capture(ctx, "rep_w")["transcript"] is None
