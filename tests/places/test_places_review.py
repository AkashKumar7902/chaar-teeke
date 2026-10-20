"""From a saved clip to a ward: process_capture, the reviewer's queue and the decision."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest
from places_support import FakeTranscriber, wav_bytes

from chaarteeke import alerts, clock
from chaarteeke.models import DecideIn
from chaarteeke.places import review
from chaarteeke.places.matcher import match

REV = "staff:rev"


@pytest.fixture
def events(pctx):
    q = pctx.bus.subscribe()

    def drain():
        out = []
        while not q.empty():
            ev = q.get_nowait()
            out.append((ev.type, ev.data))
        return out

    return drain


def audit_actions(ctx):
    return [r["action"] for r in ctx.db.query("SELECT action FROM audit ORDER BY seq")]


# ------------------------------------------------------------------ process_capture
def test_a_clear_name_is_placed_by_itself_and_the_clip_goes_at_once(pctx, world, says, events):
    pctx.transcriber = says("मुझे बख्शी नगर में कुत्ते ने काटा")
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert (row["location_status"], row["ward_no"]) == ("matched", 27)
    assert row["cluster_id"] == pctx.clusters.for_ward(27)
    assert row["asr_status"] == "done" and row["asr_model"] == "test double"
    assert row["confidence"] == "likely" and row["decided_by"] == "system:review"
    assert row["clip_deleted_at"] and row["audio_path"] is None and row["transcript"] is None
    assert not world.clip_path(rid).exists()
    assert "review.matched" in audit_actions(pctx)
    kinds = [t for t, _ in events()]
    assert "report" in kinds and "review" in kinds
    assert pctx.db.one("SELECT reason FROM purge_log WHERE what='clip'")["reason"] == "decided"


def test_the_transcriber_is_asked_in_the_courses_language(pctx, world, says):
    t = pctx.transcriber = says("Bakshi Nagar")
    for lang in ("hi", "doi", "ur"):
        review.process_capture(pctx, world.report(lang=lang, clip=wav_bytes()))
    assert [lang for _, lang in t.calls] == ["hi", "doi", "ur"]


def test_a_shared_name_waits_for_a_person_with_the_wards_offered(pctx, world, says):
    pctx.transcriber = says("गांधी नगर")
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["location_status"] == "needs_review" and row["ward_no"] is None
    assert row["confidence"] == "possible" and row["transcript"] == "गांधी नगर"
    assert world.clip_path(rid).exists()
    item = review.next_item(pctx)
    assert item.report_id == rid
    assert {c.ward_no for c in item.candidates} >= {20, 21}
    assert {c.label for c in item.candidates} == {"possible"}
    assert item.candidates[0].ward_name in ("Gandhi Nagar (North)", "Gandhi Nagar (South)")
    assert item.asr_status == "done" and item.audio_available and item.remaining == 1


def test_nothing_recognised_waits_with_the_transcript_and_no_candidates(pctx, world, says):
    pctx.transcriber = says("kutte ne kata")
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    assert world.row(rid)["confidence"] == "none"
    item = review.next_item(pctx)
    assert item.candidates == [] and item.transcript == "kutte ne kata"


def test_with_the_speech_model_off_the_item_says_so_and_still_has_the_clip(pctx, world):
    pctx.transcriber = None
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    item = review.next_item(pctx)
    assert item.asr_status == "off" and item.transcript is None and item.candidates == []
    assert item.audio_available
    assert world.row(rid)["location_status"] == "needs_review"


def test_a_failed_transcription_is_shown_with_a_scrubbed_reason_and_the_clip_stays(pctx, world, events):
    pctx.transcriber = FakeTranscriber(
        error=RuntimeError("model crashed for +91 98765 43210 at /srv/x/var/clips/a.wav")
    )
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["asr_status"] == "failed" and row["location_status"] == "needs_review"
    assert "RuntimeError" in row["asr_error"]
    assert "98765" not in row["asr_error"] and "/srv" not in row["asr_error"]
    assert review.next_item(pctx).asr_error == row["asr_error"]
    assert ("system", {"check": "speech", "ok": False}) in events()


def test_a_missing_clip_file_is_no_audio_and_does_not_enter_the_queue(pctx, world, says):
    pctx.transcriber = says("Bakshi Nagar")
    rid = world.report(clip=wav_bytes())
    world.clip_path(rid).unlink()
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["asr_status"] == "no_audio" and row["location_status"] == "no_place_heard"
    assert review.next_item(pctx) is None
    assert pctx.transcriber.calls == []


def test_a_missing_gazetteer_is_shown_as_failed_and_a_person_decides(pctx, world, says, events):
    pctx.transcriber = says("Bakshi Nagar")
    pctx.gazetteer = None
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["asr_status"] == "failed" and "place names" in row["asr_error"]
    assert row["location_status"] == "needs_review" and row["ward_no"] is None
    assert world.clip_path(rid).exists()
    assert ("system", {"check": "speech", "ok": False}) in events()


def test_missing_analysis_areas_stop_a_match_being_set_by_itself(pctx, world, says):
    pctx.transcriber = says("Bakshi Nagar")
    pctx.clusters = None
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["location_status"] == "needs_review" and row["ward_no"] is None
    assert "analysis areas" in row["asr_error"] and world.clip_path(rid).exists()


@pytest.mark.parametrize(
    ("state", "extra", "words"),
    [("loading", {}, "still loading"), ("failed", {"asr_error": "OSError: no weights"}, "did not load")],
)
def test_a_model_that_is_not_ready_is_not_called_off(pctx, world, state, extra, words):
    pctx.settings = pctx.settings.model_copy(update={"asr": "indicconformer"})
    pctx.transcriber = None
    pctx.extras.update(asr_state=state, **extra)
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    item = review.next_item(pctx)
    assert item.asr_status == "failed" and words in item.asr_error
    assert item.audio_available and item.transcript is None


def test_a_clip_path_outside_the_folders_is_no_audio_and_never_opened(pctx, world, says, tmp_path):
    pctx.transcriber = says("Bakshi Nagar")
    outside = tmp_path / "elsewhere.wav"
    outside.write_bytes(wav_bytes())
    rid = world.report(clip=wav_bytes())
    with pctx.db.transaction() as conn:
        conn.execute("UPDATE place_captures SET audio_path=? WHERE report_id=?", (str(outside), rid))
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["asr_status"] == "no_audio" and row["location_status"] == "no_place_heard"
    assert pctx.transcriber.calls == [] and outside.exists()


def test_process_pending_reads_what_a_stopped_server_left_behind(pctx, world, says):
    pctx.transcriber = says("kutte ne kata")
    first, second = world.report(clip=wav_bytes()), world.report(clip=wav_bytes())
    review.process_capture(pctx, first)
    assert review.process_pending(pctx) == 1
    assert world.row(second)["asr_status"] == "done"
    assert review.process_pending(pctx) == 0


def test_processing_twice_does_nothing_the_second_time(pctx, world, says):
    t = pctx.transcriber = says("गांधी नगर")
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    review.process_capture(pctx, rid)
    assert len(t.calls) == 1


def test_an_unknown_report_is_ignored(pctx, says):
    pctx.transcriber = says("x")
    review.process_capture(pctx, "rep_missing")


def test_a_withdrawal_during_transcription_wins(pctx, world, says):
    """The clip is deleted while the model is working: the late result is dropped, nothing is resurrected."""
    rid = world.report(clip=wav_bytes())

    def withdraw():
        world.clip_path(rid).unlink()
        with pctx.db.transaction() as conn:
            conn.execute(
                "UPDATE place_captures SET clip_deleted_at='2026-10-19T00:00:00.000Z', audio_path=NULL WHERE report_id=?",
                (rid,),
            )
            conn.execute("UPDATE bite_reports SET location_status='cant_tell' WHERE id=?", (rid,))

    pctx.transcriber = says("Bakshi Nagar", during=withdraw)
    review.process_capture(pctx, rid)
    row = world.row(rid)
    assert row["location_status"] == "cant_tell" and row["ward_no"] is None and row["transcript"] is None


def test_a_human_decision_is_not_overwritten_by_a_late_match(pctx, world, says):
    rid = world.report(clip=wav_bytes())

    def decide_meanwhile():
        with pctx.db.transaction() as conn:
            conn.execute("UPDATE bite_reports SET location_status='cant_tell' WHERE id=?", (rid,))

    pctx.transcriber = says("Bakshi Nagar", during=decide_meanwhile)
    review.process_capture(pctx, rid)
    assert world.row(rid)["location_status"] == "cant_tell"


def test_an_alert_follows_the_report_to_its_ward(pctx, world, says):
    """A sick-dog alert made before the place was known gets the ward when the match comes in."""
    pctx.transcriber = says("Bakshi Nagar")
    rid = world.report(clip=wav_bytes(), signs="yes")
    with pctx.db.transaction() as conn:
        alerts.from_report(pctx, conn, rid)
    assert pctx.db.one("SELECT ward_no FROM alerts WHERE report_id=?", (rid,))["ward_no"] is None
    review.process_capture(pctx, rid)
    a = pctx.db.one("SELECT ward_no, cluster_id FROM alerts WHERE report_id=?", (rid,))
    assert a["ward_no"] == 27 and a["cluster_id"] == pctx.clusters.for_ward(27)


# ---------------------------------------------------------------------- the queue
def queue_three(pctx, world, says):
    pctx.transcriber = says("गांधी नगर")
    ids = []
    for _ in range(3):
        rid = world.report(clip=wav_bytes())
        review.process_capture(pctx, rid)
        ids.append(rid)
        clock.current().advance(timedelta(seconds=1))
    return ids


def test_the_oldest_waiting_item_comes_first_and_remaining_counts_all(pctx, world, says):
    ids = queue_three(pctx, world, says)
    item = review.next_item(pctx)
    assert item.report_id == ids[0] and item.remaining == 3
    assert review.remaining(pctx) == 3


def test_an_empty_queue_is_none(pctx):
    assert review.next_item(pctx) is None
    assert review.remaining(pctx) == 0


def test_get_item_refuses_what_is_not_waiting(pctx, world):
    rid = world.report(clip=wav_bytes())
    with pytest.raises(review.ReviewError) as e:
        review.get_item(pctx, rid)
    assert e.value.code == "not_in_queue"


def test_the_clip_is_served_to_the_reviewer_as_bytes(pctx, world, says):
    pctx.transcriber = says("गांधी नगर")
    clip = wav_bytes(0.5)
    rid = world.report(clip=clip)
    review.process_capture(pctx, rid)
    assert review.audio(pctx, rid) == clip


def test_audio_of_nothing_or_of_a_deleted_clip_is_refused(pctx, world, says):
    with pytest.raises(review.ReviewError) as e:
        review.audio(pctx, "rep_missing")
    assert e.value.code == "not_found"
    pctx.transcriber = says("गांधी नगर")
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    review.decide(pctx, rid, DecideIn(cant_tell=True), REV)
    with pytest.raises(review.ReviewError) as e:
        review.audio(pctx, rid)
    assert e.value.code == "no_audio"


# -------------------------------------------------------------------- the decision
@pytest.fixture
def waiting(pctx, world, says):
    pctx.transcriber = says("मुझे गांधी नगर में कुत्ते ने काटा")
    rid = world.report(clip=wav_bytes(), signs="yes")
    with pctx.db.transaction() as conn:
        alerts.from_report(pctx, conn, rid)
    review.process_capture(pctx, rid)
    return rid


def test_choosing_a_ward_places_the_report_and_deletes_the_clip_and_transcript(pctx, world, waiting, events):
    events()
    left = review.decide(pctx, waiting, DecideIn(ward_no=21), REV)
    row = world.row(waiting)
    assert left == 0
    assert (row["location_status"], row["ward_no"]) == ("decided", 21)
    assert row["cluster_id"] == pctx.clusters.for_ward(21)
    assert row["decided_by"] == REV and row["decided_at"] and row["clip_deleted_at"]
    assert row["audio_path"] is None and row["transcript"] is None and row["candidates_json"] is None
    assert not world.clip_path(waiting).exists()
    assert "review.decide" in audit_actions(pctx)
    detail = json.loads(
        pctx.db.one("SELECT detail_json FROM audit WHERE action='review.decide'")["detail_json"]
    )
    assert detail == {"decision": "decided", "remembered": False, "ward_no": 21}
    assert ("review", {"remaining": 0}) in events()
    assert pctx.db.one("SELECT ward_no FROM alerts WHERE report_id=?", (waiting,))["ward_no"] == 21


def test_outside_the_city_and_cant_tell(pctx, world, says):
    pctx.transcriber = says("गांधी नगर")
    a, b = (world.report(clip=wav_bytes()) for _ in range(2))
    review.process_capture(pctx, a)
    review.process_capture(pctx, b)
    review.decide(pctx, a, DecideIn(outside_tehsil="  Samba "), REV)
    review.decide(pctx, b, DecideIn(cant_tell=True), REV)
    ra, rb = world.row(a), world.row(b)
    assert (ra["location_status"], ra["outside_tehsil"], ra["ward_no"]) == ("outside_jmc", "Samba", None)
    assert (rb["location_status"], rb["ward_no"]) == ("cant_tell", None)
    assert not world.clip_path(a).exists() and not world.clip_path(b).exists()


def test_remember_name_teaches_the_matcher_and_it_applies_next_time(pctx, world, says):
    pctx.transcriber = says("मुझे पीपल वाला चौक के पास कुत्ते ने काटा")
    first = world.report(clip=wav_bytes())
    review.process_capture(pctx, first)
    assert world.row(first)["location_status"] == "needs_review"
    review.decide(pctx, first, DecideIn(ward_no=18, remember_name=True), REV)
    learned = pctx.gazetteer.aliases()
    assert [(a.text_norm, a.ward_no, a.added_by) for a in learned] == [("पीपल वाला चौक", 18, REV)]
    # the same words from the next caller now place the report by themselves
    second = world.report(clip=wav_bytes())
    review.process_capture(pctx, second)
    assert (world.row(second)["location_status"], world.row(second)["ward_no"]) == ("matched", 18)


def test_a_name_is_not_remembered_unless_asked(pctx, world, waiting):
    review.decide(pctx, waiting, DecideIn(ward_no=20), REV)
    assert pctx.gazetteer.aliases() == []


def test_remember_name_with_only_filler_is_refused_and_nothing_changes(pctx, world, says):
    pctx.transcriber = says("main near the")
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, rid, DecideIn(ward_no=20, remember_name=True), REV)
    assert e.value.code == "no_name"
    assert world.row(rid)["location_status"] == "needs_review" and world.clip_path(rid).exists()


def test_remember_name_needs_a_transcript(pctx, world):
    pctx.transcriber = None
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, rid, DecideIn(ward_no=20, remember_name=True), REV)
    assert e.value.code == "no_name"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (DecideIn(), "bad_decision"),
        (DecideIn(ward_no=20, cant_tell=True), "bad_decision"),
        (DecideIn(ward_no=20, outside_tehsil="Samba"), "bad_decision"),
        (DecideIn(cant_tell=True, remember_name=True), "bad_decision"),
        (DecideIn(outside_tehsil="x" * 200), "bad_decision"),
        (DecideIn(ward_no=99), "bad_ward"),
        (DecideIn(ward_no=0), "bad_ward"),
    ],
)
def test_a_decision_must_be_exactly_one_valid_choice(pctx, world, waiting, data, code):
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, waiting, data, REV)
    assert e.value.code == code
    assert world.row(waiting)["location_status"] == "needs_review" and world.clip_path(waiting).exists()


def test_a_report_can_be_decided_once(pctx, waiting):
    review.decide(pctx, waiting, DecideIn(ward_no=20), REV)
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, waiting, DecideIn(ward_no=21), REV)
    assert e.value.code == "already_decided"


def test_a_report_that_was_matched_by_itself_cannot_be_redecided(pctx, world, says):
    pctx.transcriber = says("Bakshi Nagar")
    rid = world.report(clip=wav_bytes())
    review.process_capture(pctx, rid)
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, rid, DecideIn(ward_no=20), REV)
    assert e.value.code == "already_decided"


def test_a_report_that_is_not_in_the_queue_or_does_not_exist(pctx, world):
    rid = world.report(clip=wav_bytes())
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, rid, DecideIn(ward_no=20), REV)
    assert e.value.code == "not_in_queue"
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, "rep_missing", DecideIn(ward_no=20), REV)
    assert e.value.code == "not_found"


@pytest.mark.parametrize(
    ("actor", "code"),
    [
        ("visitor:3", "read_only"),
        ("staff:vet", "forbidden"),
        ("staff:ghost", "forbidden"),
        ("system:scheduler", "forbidden"),
        ("patient:crs_x", "forbidden"),
    ],
)
def test_only_a_reviewer_may_decide(pctx, world, waiting, actor, code):
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, waiting, DecideIn(ward_no=20), actor)
    assert e.value.code == code
    assert world.row(waiting)["location_status"] == "needs_review"


def test_an_inactive_reviewer_may_not_decide(pctx, waiting):
    with pctx.db.transaction() as conn:
        conn.execute("UPDATE staff SET active=0 WHERE username='rev'")
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, waiting, DecideIn(ward_no=20), REV)
    assert e.value.code == "forbidden"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can delete anything")
def test_if_the_clip_cannot_be_deleted_nothing_is_decided(pctx, world, waiting):
    path = world.clip_path(waiting)
    path.unlink()
    path.mkdir()  # unlink() on a directory fails: stands for a clip that cannot be removed
    try:
        with pytest.raises(OSError):
            review.decide(pctx, waiting, DecideIn(ward_no=20), REV)
        row = world.row(waiting)
        assert row["location_status"] == "needs_review" and row["decided_at"] is None
        assert row["transcript"]
    finally:
        path.rmdir()


def test_a_clip_path_outside_the_clips_folder_is_refused(pctx, world, waiting, tmp_path):
    outside = tmp_path / "elsewhere.wav"
    outside.write_bytes(b"x")
    with pctx.db.transaction() as conn:
        conn.execute("UPDATE place_captures SET audio_path=? WHERE report_id=?", (str(outside), waiting))
    with pytest.raises(ValueError, match="outside"):
        review.decide(pctx, waiting, DecideIn(ward_no=20), REV)
    assert outside.exists()


def test_decide_is_consistent_with_what_the_matcher_offered(pctx, waiting):
    """What the reviewer sees as 'possible' is exactly what the matcher returns for the transcript."""
    item = review.next_item(pctx)
    offered = match(pctx.gazetteer, "मुझे गांधी नगर में कुत्ते ने काटा", pctx.policy)
    assert [c.ward_no for c in item.candidates] == offered.ward_nos


# ---------------------------------------------------------- the demo's shipped recordings
def test_a_shipped_demo_recording_can_be_heard_but_is_never_deleted(pctx, world, waiting, tmp_path):
    data = tmp_path / "data"
    shipped = data / "demo" / "clips"
    shipped.mkdir(parents=True)
    clip = shipped / "unit-test-clip.wav"
    clip.write_bytes(wav_bytes(0.3))
    pctx.settings = pctx.settings.model_copy(update={"data_dir": data})
    with pctx.db.transaction() as conn:
        conn.execute(
            "UPDATE place_captures SET audio_path='data/demo/clips/unit-test-clip.wav' WHERE report_id=?",
            (waiting,),
        )
    world.clip_path(waiting).unlink()
    assert review.next_item(pctx).audio_available
    assert review.audio(pctx, waiting) == clip.read_bytes()
    review.decide(pctx, waiting, DecideIn(ward_no=20), REV)
    assert clip.exists()
    assert world.row(waiting)["audio_path"] is None and world.row(waiting)["clip_deleted_at"]


# ------------------------------------------------------ a bad row must not block the queue
def _point_outside(pctx, rid, tmp_path):
    outside = tmp_path / "elsewhere.wav"
    outside.write_bytes(b"x")
    with pctx.db.transaction() as conn:
        conn.execute("UPDATE place_captures SET audio_path=? WHERE report_id=?", (str(outside), rid))
    return outside


def test_a_row_with_a_bad_clip_path_does_not_break_the_queue_view(pctx, world, waiting, tmp_path):
    _point_outside(pctx, waiting, tmp_path)
    item = review.next_item(pctx)
    assert item.report_id == waiting and item.audio_available is False


def test_the_audio_of_a_bad_clip_path_is_refused_in_plain_words(pctx, world, waiting, tmp_path):
    outside = _point_outside(pctx, waiting, tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.audio(pctx, waiting)
    assert e.value.code == "bad_clip" and "outside the recordings folder" in e.value.message
    assert outside.exists()


def test_deciding_a_bad_clip_path_is_a_typed_error_and_changes_nothing(pctx, world, waiting, tmp_path):
    _point_outside(pctx, waiting, tmp_path)
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, waiting, DecideIn(ward_no=20), REV)
    assert e.value.code == "bad_clip"
    assert world.row(waiting)["location_status"] == "needs_review"


@pytest.mark.parametrize("name", ["Call 98765 43210", "see /srv/x/clip.wav", "sim:abc-123"])
def test_an_outside_place_name_may_not_carry_a_number_or_a_path(pctx, waiting, name):
    with pytest.raises(review.ReviewError) as e:
        review.decide(pctx, waiting, DecideIn(outside_tehsil=name), REV)
    assert e.value.code == "bad_decision"
    assert review.remaining(pctx) == 1


def test_an_outside_place_name_is_stored_on_one_line(pctx, waiting, world):
    review.decide(pctx, waiting, DecideIn(outside_tehsil="  Samba \n  town "), REV)
    assert world.row(waiting)["outside_tehsil"] == "Samba town"


def test_skip_gives_the_next_oldest_and_past_the_end_gives_none(pctx, world, says):
    ids = queue_three(pctx, world, says)
    for k in range(3):
        item = review.next_item(pctx, skip=k)
        assert item.report_id == ids[k] and item.remaining == 3
    assert review.next_item(pctx, skip=3) is None
    assert review.remaining(pctx) == 3
