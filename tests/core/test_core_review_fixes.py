"""Register numbers, corrections, shared phones, what a patient's key press leaves for the clinic, a patient
who says "I took it", the clinic's simulator missed calls, backups and the file-level deletion promises."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from core_support import FakeRunner
from cryptography.fernet import Fernet

from chaarteeke import clock, crypto, doses, enrol, privacy
from chaarteeke.context import AppContext
from chaarteeke.db import Database
from chaarteeke.enrol import EnrolError
from chaarteeke.scheduler import Scheduler

NUMBER = "+919876543210"


def codes(fn, *a, **kw) -> str:
    with pytest.raises(EnrolError) as e:
        fn(*a, **kw)
    return e.value.code


# ------------------------------------------------------- register numbers
def test_a_register_number_with_a_live_course_is_refused_and_names_the_existing_entry(ctx, world):
    first = world.course(register_no="ARC-7")
    with pytest.raises(EnrolError) as e:
        enrol.create_course(ctx, world.data(register_no="ARC-7"), "staff:a")
    assert e.value.code == "duplicate_register" and e.value.course_id == first
    assert ctx.db.one("SELECT COUNT(*) FROM courses")[0] == 1
    world.clinic("cln_b", "Other clinic")
    assert world.course(clinic_id="cln_b", register_no="ARC-7")  # another clinic's register is its own


def test_a_register_number_can_be_used_again_once_its_course_has_ended(ctx, world):
    first = world.course(register_no="ARC-7")
    enrol.stop(ctx, first, "left", "staff:a")
    assert world.course(register_no="ARC-7")


@pytest.mark.parametrize(
    "raw", ["", "   ", "12 34", "AB‮12", "AB\x0012", "AB\n12", "9876543210", "x" * 21, "९८७६", "a;b"]
)
def test_a_register_number_has_a_character_rule(ctx, world, raw):
    world.clinic()
    data = world.data().model_copy(update={"register_no": raw})
    assert codes(enrol.create_course, ctx, data, "staff:a") == "bad_register_no"
    assert ctx.db.one("SELECT COUNT(*) FROM courses")[0] == 0


def test_invisible_format_characters_are_dropped_not_kept(ctx, world):
    world.clinic()
    data = world.data().model_copy(update={"register_no": "AB​12﻿/2026"})
    assert enrol.create_course(ctx, data, "staff:a").register_no == "AB12/2026"


@pytest.mark.parametrize("raw", ["ARC-7", "12/2026", "a.b_c-1", "0123456789"[:9], "A" * 20])
def test_ordinary_register_numbers_pass(ctx, world, raw):
    world.clinic()
    data = world.data().model_copy(update={"register_no": raw})
    assert enrol.create_course(ctx, data, "staff:a").register_no == raw


def test_a_slip_at_the_desk_can_be_corrected_in_the_edit_window_and_is_audited(ctx, world, fixed_clock):
    cid = world.course(register_no="ARC-7")
    other = world.course(register_no="ARC-8")
    out = enrol.update_course(ctx, cid, "staff:asha", register_no="ARC-17", eligibility="guardian_consented")
    assert (out.register_no, out.eligibility) == ("ARC-17", "guardian_consented")
    row = ctx.db.one("SELECT actor, detail_json FROM audit WHERE action='course.update'")
    assert row["actor"] == "staff:asha" and "ARC-17" in row["detail_json"]
    assert codes(enrol.update_course, ctx, cid, "staff:a", register_no="ARC-8") == "duplicate_register"
    assert codes(enrol.update_course, ctx, cid, "staff:a", register_no="bad one") == "bad_register_no"
    assert codes(enrol.update_course, ctx, cid, "staff:a") == "nothing_to_change"
    assert codes(enrol.update_course, ctx, cid, "staff:a", register_no="ARC-17") == "nothing_to_change"
    assert other  # untouched
    minutes = ctx.policy["clinic"]["edit_window_minutes"]
    fixed_clock.advance(timedelta(minutes=minutes + 1))
    assert codes(enrol.update_course, ctx, cid, "staff:a", register_no="ARC-99") == "edit_window_closed"


def test_the_guardian_box_cannot_change_once_the_patient_has_been_called(ctx, world):
    cid = world.active(register_no="ARC-7", consent=False)
    ctx.db.connect().execute("UPDATE calls SET status='no_answer' WHERE course_id=?", (cid,))
    assert (
        codes(enrol.update_course, ctx, cid, "staff:a", eligibility="guardian_consented")
        == "edit_window_closed"
    )
    # the register number can still be corrected
    assert enrol.update_course(ctx, cid, "staff:a", register_no="ARC-8").register_no == "ARC-8"


def test_a_closed_course_cannot_be_corrected(ctx, world):
    cid = world.course(register_no="ARC-7")
    enrol.stop(ctx, cid, "left", "staff:a")
    assert codes(enrol.update_course, ctx, cid, "staff:a", register_no="ARC-8") == "course_closed"
    assert codes(enrol.update_course, ctx, "crs_nope", "staff:a", register_no="X") == "not_found"


# ------------------------------------------------ the clinic's own browser phone
def test_a_missed_call_from_a_clinics_own_phone_waits_to_be_linked_but_a_visitors_is_linked_at_once(
    ctx, world, fixed_clock
):
    cid = world.course(register_no="N101")
    # the visitor path: nobody behind the phone, so the demo links it at once to its own desk
    viewer = enrol.record_missed_call(ctx, "sim:visitor1", "sim", clock.now())
    assert viewer.status == "linked" and viewer.course_id != cid
    # the clinic's view: a normal missed call, seen on Today with the simulator pill, linked by staff
    res = enrol.record_missed_call(ctx, "sim:clinic1", "sim", clock.now(), clinic_hint="cln_a")
    assert res.status == "unlinked" and res.course_id is None
    today = enrol.clinic_today(ctx, "cln_a")
    assert [(u.id, u.transport) for u in today.unlinked] == [(res.missed_call_id, "sim")]
    assert [r.register_no for r in today.awaiting_call] == ["N101"]
    assert enrol.clinic_today(ctx, "cln_demo_visitor").unlinked == []
    linked = enrol.link(ctx, res.missed_call_id, cid, "staff:asha")
    assert linked.status == "active"
    assert enrol.clinic_today(ctx, "cln_a").unlinked == []
    first = world.calls(cid, "first")[0]
    assert first["transport"] == "sim" and first["status"] == "scheduled"


def test_another_clinics_simulator_call_is_not_offered_and_cannot_be_linked(ctx, world):
    world.clinic("cln_b", "Other clinic")
    cid = world.course(register_no="N101")
    res = enrol.record_missed_call(ctx, "sim:clinic2", "sim", clock.now(), clinic_hint="cln_b")
    assert enrol.clinic_today(ctx, "cln_a").unlinked == []
    assert codes(enrol.link, ctx, res.missed_call_id, cid, "staff:a") == "wrong_clinic"


# ----------------------------------------------------------- a shared phone
def test_a_household_can_hold_two_courses_on_one_phone(ctx, world, fixed_clock):
    mother = world.active(number=NUMBER, register_no="ARC-1")
    child = world.course(register_no="ARC-2")
    # the family rings again from the same phone for the second patient
    res = enrol.record_missed_call(ctx, NUMBER, "ari", clock.now())
    assert res.status == "duplicate" and res.course_id == mother
    today = enrol.clinic_today(ctx, "cln_a")
    assert [(u.id, u.phone_last4) for u in today.unlinked] == [(res.missed_call_id, "3210")]
    linked = enrol.link(ctx, res.missed_call_id, child, "staff:asha")
    assert linked.status == "active" and linked.phone_last4 == "3210"
    assert enrol.same_phone_courses(ctx, mother) == enrol.same_phone_courses(ctx, child) == 2
    # the same missed call cannot be linked to the course that already has the number
    other = enrol.record_missed_call(ctx, NUMBER, "ari", clock.now())
    assert codes(enrol.link, ctx, other.missed_call_id, other.course_id, "staff:a") == "already_linked"


def test_a_repeat_call_of_another_clinics_patient_cannot_be_linked_by_this_clinic(ctx, world):
    world.clinic("cln_b", "Other clinic")
    world.active(number=NUMBER, register_no="ARC-1")
    res = enrol.record_missed_call(ctx, NUMBER, "ari", clock.now())
    theirs = world.course(clinic_id="cln_b", register_no="B-1")
    assert codes(enrol.link, ctx, res.missed_call_id, theirs, "staff:b") == "wrong_clinic"


def test_a_repeat_call_is_offered_for_linking_only_when_an_entry_is_waiting_and_only_to_its_clinic(
    ctx, world
):
    world.clinic("cln_b", "Other clinic")
    world.active(number=NUMBER, register_no="ARC-1")
    enrol.record_missed_call(ctx, NUMBER, "ari", clock.now())
    assert enrol.clinic_today(ctx, "cln_a").unlinked == []  # nobody is waiting for a number
    world.course(register_no="ARC-2")
    world.course(clinic_id="cln_b", register_no="B-1")
    assert len(enrol.clinic_today(ctx, "cln_a").unlinked) == 1
    assert enrol.clinic_today(ctx, "cln_b").unlinked == []  # it is another clinic's patient's number


async def test_two_courses_on_one_phone_each_get_their_own_reminder(ctx, world, fixed_clock):
    ctx.runner = FakeRunner(ctx)
    mother = world.active(number=NUMBER, register_no="ARC-1")
    child = world.course(register_no="ARC-2")
    res = enrol.record_missed_call(ctx, NUMBER, "ari", clock.now())
    enrol.link(ctx, res.missed_call_id, child, "staff:a")
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE courses SET consent_reminders_at=? WHERE id=?", (clock.iso(clock.now()), child))
    evening = world.dose_date(mother, 2) - timedelta(days=1)
    world.goto(evening, "18:30", fixed_clock)
    sched = Scheduler(ctx, "a")
    await sched.tick()
    reminders = world.calls(kind="reminder")
    assert {r["course_id"] for r in reminders} == {mother, child}
    assert len({r["job_key"] for r in reminders}) == 2
    assert all(r["dose_no"] == 2 for r in reminders)


# ---------------------------------------------- what a patient's key leaves
def test_a_patient_who_pressed_9_is_listed_for_the_clinic_until_staff_clear_it(ctx, world, events):
    cid = world.active(register_no="ARC-5")
    enrol.withdraw(ctx, cid, f"patient:{cid}", "the patient pressed 9 to stop the calls")
    [note] = enrol.ended_by_patient(ctx, "cln_a")
    assert (note.course_id, note.register_no, note.kind, note.needs_new_number) == (
        cid,
        "ARC-5",
        "withdrawn",
        False,
    )
    assert "pressed 9" in note.reason and note.at
    assert enrol.get_course(ctx, cid).status == "stopped"
    # it stays across days and is not on another clinic's list
    world.clinic("cln_b", "Other clinic")
    assert enrol.ended_by_patient(ctx, "cln_b") == []
    enrol.ended_seen(ctx, cid, "staff:asha")
    assert enrol.ended_by_patient(ctx, "cln_a") == []
    row = ctx.db.one("SELECT actor FROM audit WHERE action='course.ended_seen'")
    assert row["actor"] == "staff:asha"
    assert codes(enrol.ended_seen, ctx, cid, "staff:asha") == "not_found"
    assert codes(enrol.ended_seen, ctx, "crs_nope", "staff:asha") == "not_found"


def test_a_patient_who_said_no_to_reminders_is_listed_too(ctx, world):
    cid = world.active(register_no="ARC-6", consent=False)
    enrol.withdraw(ctx, cid, f"patient:{cid}", "the patient said no to reminders", kind="declined")
    assert [(n.kind, n.register_no) for n in enrol.ended_by_patient(ctx, "cln_a")] == [("declined", "ARC-6")]


def test_a_staff_stop_leaves_no_note_because_the_clinic_did_it_itself(ctx, world):
    cid = world.active()
    enrol.stop(ctx, cid, "moved away", "staff:asha")
    assert enrol.ended_by_patient(ctx, "cln_a") == []


def test_a_wrong_number_keeps_the_course_open_and_asks_for_a_new_number(ctx, world, fixed_clock):
    cid = world.active(number=NUMBER, register_no="ARC-9")
    enrol.withdraw(ctx, cid, f"patient:{cid}", "the patient pressed 8: wrong number", kind="wrong_number")
    course = enrol.get_course(ctx, cid)
    assert course.status == "awaiting_call" and course.phone_last4 is None
    assert ctx.db.one("SELECT 1 FROM contacts WHERE course_id=?", (cid,)) is None  # the wrong number is gone
    assert [c["status"] for c in world.calls(cid, "first")] == ["cancelled"]
    [note] = enrol.ended_by_patient(ctx, "cln_a")
    assert (note.kind, note.needs_new_number) == ("wrong_number", True)
    today = enrol.clinic_today(ctx, "cln_a")
    assert [r.course_id for r in today.awaiting_call] == [cid]  # it is waiting for the right number
    # the clinic gets the right number and links it: the course carries on, the note goes
    res = enrol.record_missed_call(ctx, "+919999900233", "ari", clock.now())
    again = enrol.link(ctx, res.missed_call_id, cid, "staff:asha")
    assert again.status == "active" and again.phone_last4 == "0233"
    assert ctx.db.one("SELECT stopped_reason FROM courses WHERE id=?", (cid,))["stopped_reason"] is None
    assert enrol.ended_by_patient(ctx, "cln_a") == []
    firsts = world.calls(cid, "first")
    assert [c["status"] for c in firsts] == ["cancelled", "scheduled"]
    assert firsts[1]["job_key"] == f"first:{cid}:{res.missed_call_id}"


def test_a_wrong_number_keeps_the_patients_place_recording(ctx, world):
    cid = world.active(number=NUMBER)
    rep = "rep_x"
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bite_reports(id, course_id, clinic_id, bite_week, location_status, source, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (rep, cid, "cln_a", "2026-10-19", "needs_review", "clinic", clock.iso(clock.now())),
        )
        conn.execute(
            "INSERT INTO place_captures(report_id, audio_path, asr_status, expires_at) VALUES (?,?, 'done', ?)",
            (rep, None, clock.iso(clock.now() + timedelta(days=1))),
        )
    enrol.withdraw(ctx, cid, f"patient:{cid}", "wrong number", kind="wrong_number")
    assert (
        ctx.db.one("SELECT location_status FROM bite_reports WHERE id=?", (rep,))["location_status"]
        == "needs_review"
    )


# ------------------------------------------------------ "I took it" is a claim
def _reported(ctx, world, dose_no=2) -> str:
    cid = world.active(day0=world.today() - timedelta(days=4), register_no="ARC-3")
    doses.refresh_states(ctx)
    with ctx.db.transaction() as conn:
        call_id = enrol.new_call(
            conn,
            course_id=cid,
            kind="missed_dose",
            job_key="t:md",
            scheduled_for=clock.now(),
            dose_no=dose_no,
            transport="ari",
        )
        conn.execute("UPDATE calls SET status='completed' WHERE id=?", (call_id,))
        conn.execute(
            "INSERT INTO answers(call_id, question, value, at) VALUES (?, 'dose_taken', 'yes', ?)",
            (call_id, clock.iso(clock.now())),
        )
    return cid


def test_a_dose_the_patient_says_they_took_stays_open_and_is_listed_as_needing_confirmation(ctx, world):
    cid = _reported(ctx, world)
    assert enrol.patient_reported_doses(ctx.db, cid) == {2}
    assert world.dose(cid, 2)["status"] in ("late_minor", "late_review")
    today = enrol.clinic_today(ctx, "cln_a")
    [row] = today.missed
    assert (row.course_id, row.state, row.next_dose_no) == (cid, "patient_says_taken", 2)
    # only staff close it
    doses.mark_given(ctx, cid, 2, "clinic", world.today(), "staff:asha")
    assert world.dose(cid, 2)["marked_by"] == "clinic"
    assert enrol.clinic_today(ctx, "cln_a").missed == []


def test_a_patients_yes_is_listed_first_among_the_missed(ctx, world):
    older = world.active(day0=world.today() - timedelta(days=9), register_no="ARC-OLD")
    reported = _reported(ctx, world)
    doses.refresh_states(ctx)
    today = enrol.clinic_today(ctx, "cln_a")
    assert [r.course_id for r in today.missed][0] == reported
    assert older in [r.course_id for r in today.missed]


def test_the_missed_list_puts_the_most_recent_misses_first(ctx, world):
    d = world.today()
    oldest = world.active(day0=d - timedelta(days=30), register_no="A")
    recent = world.active(day0=d - timedelta(days=4), register_no="B")
    middle = world.active(day0=d - timedelta(days=9), register_no="C")
    doses.refresh_states(ctx)
    missed = enrol.clinic_today(ctx, "cln_a").missed
    assert [r.course_id for r in missed] == [recent, middle, oldest]


def test_waiting_entries_are_listed_newest_first(ctx, world, fixed_clock):
    first = world.course(register_no="W-1")
    fixed_clock.advance(timedelta(minutes=5))
    second = world.course(register_no="W-2")
    fixed_clock.advance(timedelta(minutes=5))
    third = world.course(register_no="A-0")  # a register number that sorts first
    assert [r.course_id for r in enrol.clinic_today(ctx, "cln_a").awaiting_call] == [third, second, first]


# ------------------------------------------------------------ clinic lines
def test_a_missed_call_on_a_clinics_own_line_is_shown_to_that_clinic_only(ctx, world):
    world.clinic("cln_b", "Other clinic")
    world.course(register_no="X-1")
    res = enrol.record_missed_call(ctx, NUMBER, "ari", clock.now(), clinic_hint="cln_b")
    assert [u.id for u in enrol.clinic_today(ctx, "cln_b").unlinked] == [res.missed_call_id]
    assert enrol.clinic_today(ctx, "cln_a").unlinked == []
    one_number = enrol.record_missed_call(ctx, "+919999900233", "ari", clock.now())
    assert one_number.missed_call_id in [u.id for u in enrol.clinic_today(ctx, "cln_a").unlinked]


# ------------------------------------------------------------------- keys
def test_several_keys_open_old_numbers_and_new_ones_use_the_first():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    before = crypto.PhoneVault(old).encrypt(NUMBER)
    vault = crypto.PhoneVault(f"{new},{old}")
    assert vault.decrypt(before) == NUMBER
    after = vault.rotate(before)
    assert crypto.PhoneVault(new).decrypt(after) == NUMBER
    with pytest.raises(ValueError):
        crypto.PhoneVault(new).decrypt(before)
    with pytest.raises(ValueError, match="empty"):
        crypto.PhoneVault(" , ")
    with pytest.raises(ValueError):
        crypto.PhoneVault(new).rotate(before)


def test_rotate_ciphers_moves_every_stored_number_to_the_new_key(ctx, world):
    from chaarteeke.crypto import PhoneVault

    world.active(number=NUMBER)
    enrol.record_missed_call(ctx, "+919999900233", "ari", clock.now())
    old_key = ctx.settings.phone_key
    new_key = Fernet.generate_key().decode()
    ctx.vault = PhoneVault(f"{new_key},{old_key}")
    assert privacy.rotate_ciphers(ctx) == 2
    ctx.vault = PhoneVault(new_key)  # the old key is no longer needed
    cid = ctx.db.one("SELECT course_id FROM contacts")["course_id"]
    assert enrol.reveal_phone(ctx, cid, "staff:a") == NUMBER


def test_the_demo_key_is_private_from_the_first_byte(tmp_path):
    key = crypto.demo_key(tmp_path)
    p = tmp_path / "demo_phone.key"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert crypto.demo_key(tmp_path) == key
    assert not list(tmp_path.glob(".demo_phone.*"))


# ---------------------------------------------------------------- backups
def _backup_ctx(ctx, tmp_path):
    ctx.settings = ctx.settings.model_copy(
        update={"backup_dir": tmp_path / "backups", "backup_key": Fernet.generate_key().decode()}
    )
    return ctx


def test_a_backup_is_encrypted_private_and_restores_to_the_same_data(ctx, world, tmp_path):
    cid = world.active(number=NUMBER, register_no="ARC-1")
    _backup_ctx(ctx, tmp_path)
    path = privacy.backup_database(ctx)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no plain copy left behind
    raw = path.read_bytes()
    assert b"SQLite format" not in raw and b"ARC-1" not in raw
    restored = privacy.restore_backup(path, ctx.settings.backup_key, tmp_path / "restored.db")
    con = sqlite3.connect(restored)
    assert con.execute("SELECT register_no FROM courses WHERE id=?", (cid,)).fetchone()[0] == "ARC-1"
    cipher = con.execute("SELECT phone_cipher FROM contacts WHERE course_id=?", (cid,)).fetchone()[0]
    assert ctx.vault.decrypt(cipher) == NUMBER  # still protected by the phone key
    con.close()
    # restoring never overwrites, and the wrong key opens nothing
    with pytest.raises(privacy.BackupError, match="already exists"):
        privacy.restore_backup(path, ctx.settings.backup_key, restored)
    with pytest.raises(privacy.BackupError, match="does not open"):
        privacy.restore_backup(path, Fernet.generate_key().decode(), tmp_path / "other.db")


def test_backups_older_than_the_policy_days_are_deleted(ctx, world, tmp_path, fixed_clock):
    _backup_ctx(ctx, tmp_path)
    first = privacy.backup_database(ctx)
    os.utime(first, (clock.now().timestamp(), clock.now().timestamp()))
    fixed_clock.advance(timedelta(days=ctx.policy["retention"]["backup_keep_days"] + 1))
    second = privacy.backup_database(ctx)
    assert not first.exists() and second.exists()


def test_without_backup_settings_there_is_no_backup_and_it_says_so(ctx):
    with pytest.raises(privacy.BackupError, match="CT_BACKUP_DIR"):
        privacy.backup_database(ctx)


async def test_the_scheduler_backs_up_once_a_day_after_the_purge_time(ctx, world, tmp_path, fixed_clock):
    _backup_ctx(ctx, tmp_path)
    sched = Scheduler(ctx, "a")
    await sched.tick()
    await sched.tick()
    assert len(list((tmp_path / "backups").glob("*.db.enc"))) == 1
    fixed_clock.advance(timedelta(days=1))
    await sched.tick()
    assert len(list((tmp_path / "backups").glob("*.db.enc"))) == 2


async def test_a_failed_backup_is_reported_not_hidden(ctx, world, tmp_path, events):
    _backup_ctx(ctx, tmp_path)
    ctx.settings = ctx.settings.model_copy(update={"backup_dir": tmp_path / "file-not-folder" / "x"})
    (tmp_path / "file-not-folder").write_text("in the way")
    events()
    report = await Scheduler(ctx, "a").tick()
    assert any("backup" in e for e in report.errors)
    assert ("system", {"check": "backup", "ok": False}) in events()


# ------------------------------------------------ files and deletion at rest
def _clips(ctx) -> Path:
    folder = ctx.settings.var_dir / "clips"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _age(path: Path, minutes: int) -> None:
    t = (clock.now() - timedelta(minutes=minutes)).timestamp()
    os.utime(path, (t, t))


def test_a_voice_file_no_row_points_to_is_removed_after_an_hour(ctx, world):
    folder = _clips(ctx)
    orphan, young, kept = folder / "rep_orphan.wav", folder / "rep_young.wav", folder / "rep_kept.wav"
    temp = folder / ".rep_half.ab12.tmp"
    for f in (orphan, young, kept, temp):
        f.write_bytes(b"RIFF")
    _age(orphan, 90)
    _age(temp, 90)
    _age(kept, 90)
    cid = world.active()
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO bite_reports(id, course_id, clinic_id, bite_week, location_status, source, created_at)"
            " VALUES ('rep_kept', ?, 'cln_a', '2026-10-19', 'pending', 'clinic', ?)",
            (cid, clock.iso(clock.now())),
        )
        conn.execute(
            "INSERT INTO place_captures(report_id, audio_path, asr_status, expires_at) VALUES ('rep_kept', ?, 'pending', ?)",
            (str(kept), clock.iso(clock.now() + timedelta(days=1))),
        )
    report = privacy.run_purges(ctx)
    assert report.orphan_clips == 2 and report.errors == []
    assert not orphan.exists() and not temp.exists()
    assert young.exists() and kept.exists()
    assert ctx.db.one("SELECT COUNT(*) FROM purge_log WHERE what='clip' AND reason='orphan'")[0] == 2
    assert privacy.run_purges(ctx).orphan_clips == 0


def test_storage_status_says_when_the_data_folder_cannot_take_a_clip(ctx):
    assert privacy.storage_status(ctx).ok
    ctx.policy.raw["storage"]["min_free_mb"] = 10**12
    low = privacy.storage_status(ctx)
    assert not low.ok and "MB" in low.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_storage_status_notices_a_read_only_data_folder(ctx):
    var = ctx.settings.var_dir
    var.mkdir(parents=True, exist_ok=True)
    var.chmod(0o500)
    try:
        status = privacy.storage_status(ctx)
    finally:
        var.chmod(0o700)
    assert not status.ok and "not writable" in status.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_the_system_check_flips_when_storage_goes_and_comes_back(ctx, events):
    var = ctx.settings.var_dir
    var.mkdir(parents=True, exist_ok=True)
    events()
    privacy.run_purges(ctx)
    assert ("system", {"check": "storage", "ok": False}) not in events()
    var.chmod(0o500)
    try:
        privacy.run_purges(ctx)
        privacy.run_purges(ctx)
        seen = events()
    finally:
        var.chmod(0o700)
    assert seen.count(("system", {"check": "storage", "ok": False})) == 1  # said once, not every minute
    privacy.run_purges(ctx)
    assert ("system", {"check": "storage", "ok": True}) in events()


def test_a_deleted_transcript_does_not_stay_in_the_database_file(tmp_path, settings, fixed_clock):
    from chaarteeke.crypto import PhoneVault
    from chaarteeke.events import EventBus
    from chaarteeke.policy import load_policy

    db = Database(tmp_path / "real.db")
    db.migrate()
    policy = load_policy()
    ctx = AppContext(
        settings=settings, db=db, policy=policy, vault=PhoneVault(settings.phone_key), bus=EventBus()
    )
    marker = "xylophone-marker-" + "z" * 40
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO clinics(id,name,short_name,default_regimen,default_lang,arv_status) VALUES ('c','C','C','id_trc','hi','confirmed')"
        )
        conn.execute(
            "INSERT INTO bite_reports(id, clinic_id, bite_week, location_status, source, created_at)"
            " VALUES ('r','c','2026-10-19','matched','clinic', ?)",
            (clock.iso(clock.now()),),
        )
        conn.execute(
            "INSERT INTO place_captures(report_id, transcript, asr_status, expires_at) VALUES ('r', ?, 'done', ?)",
            (marker, clock.iso(clock.now() + timedelta(days=1))),
        )
    assert marker.encode() in b"".join(p.read_bytes() for p in tmp_path.glob("real.db*"))
    assert privacy.run_purges(ctx).clips == 1
    left = b"".join(p.read_bytes() for p in tmp_path.glob("real.db*"))
    assert marker.encode() not in left
    db.close()
