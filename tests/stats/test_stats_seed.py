"""The demo seed: deterministic, refuses a non-empty database, and consistent with the dose calendar and today."""

from __future__ import annotations

import json
import re
import stat
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest
from stats_helpers import T0, build_ctx

from chaarteeke import clock
from chaarteeke.api.auth import verify_password
from chaarteeke.config import DATA_DIR
from chaarteeke.demo import seed as seeder
from chaarteeke.regimens import get as get_regimen
from chaarteeke.regimens import schedule
from chaarteeke.stats.coverage import coverage

FAKE_NUMBER = re.compile(r"^\+9199999 0\d{4}$".replace(" ", ""))
TODAY = date(2026, 10, 19)
PHONE_SHAPE = re.compile(
    r"\+91\d{10}|(?<![0-9a-z])\d{10}(?![0-9a-z])"
)  # ids are letters and digits, so mind the edges


def rows(ctx, sql, params=()):
    return [dict(r) for r in ctx.db.query(sql, params)]


# ------------------------------------------------------------------ determinism and refusal
def test_same_seed_same_rows_and_digest_and_reset_rebuilds_them(seeded, tmp_path):
    with clock.use_clock(clock.FixedClock(T0)):
        other = build_ctx(tmp_path)
        again = seeder.seed(other)
        assert again.counts == seeded.report.counts
        assert again.digest == seeded.report.digest == seeder.digest(seeded)
        rebuilt = seeder.seed(other, reset=True)
        assert rebuilt.digest == again.digest and rebuilt.counts == again.counts
        other.db.close()


def test_another_seed_makes_other_rows(seeded, tmp_path):
    with clock.use_clock(clock.FixedClock(T0)):
        gen = seeder._Gen(build_ctx(tmp_path), 7)
        gen.build()
    mine = {r["id"] for r in rows(seeded, "SELECT id FROM courses")}
    assert not mine & {r["id"] for r in gen.rows["courses"]}


def test_it_refuses_a_database_that_already_has_data(seeded):
    with pytest.raises(seeder.SeedError) as e:
        seeder.seed(seeded)
    assert e.value.code == "not_empty"


def test_it_refuses_pilot_mode(tmp_path, fixed_clock):
    ctx = build_ctx(tmp_path, mode="pilot")
    with pytest.raises(seeder.SeedError) as e:
        seeder.seed(ctx)
    assert e.value.code == "not_demo"
    assert ctx.db.one("SELECT COUNT(*) AS n FROM clinics")["n"] == 0


def test_it_needs_the_clusters(tmp_path, fixed_clock):
    ctx = build_ctx(tmp_path)
    ctx.clusters = None
    with pytest.raises(seeder.SeedError) as e:
        seeder.seed(ctx)
    assert e.value.code == "clusters_not_loaded"


def test_report_and_kv(seeded):
    r = seeded.report
    assert r.seed == seeder.DEFAULT_SEED and r.now == clock.iso(T0)
    assert r.counts["clinics"] == 3 and r.counts["staff"] == 6
    assert r.counts["place_captures"] == 12 and r.counts["alerts"] == 9
    assert 1500 <= r.counts["courses"] <= 2500  # about 25 a day for 12 weeks
    assert (
        json.loads(seeded.db.one("SELECT value FROM kv WHERE key = 'demo_seed'")["value"])["digest"]
        == r.digest
    )
    assert seeded.db.one("SELECT 1 FROM audit WHERE action = 'demo.seed'")


# ----------------------------------------------------------------- flags, clinics, people
@pytest.mark.parametrize(
    "table",
    [
        "clinics",
        "staff",
        "courses",
        "missed_calls",
        "calls",
        "bite_reports",
        "alerts",
        "worked_wards",
        "sight_counts",
    ],
)
def test_every_row_is_flagged_demo(seeded, table):
    assert seeded.db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE is_demo != 1")["n"] == 0
    assert seeded.db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"] > 0


def test_clinics(seeded):
    got = {r["id"]: r for r in rows(seeded, "SELECT * FROM clinics")}
    assert set(got) == {"cln_gmc", "cln_demo2", "cln_demo_visitor"}
    assert (
        got["cln_gmc"]["name"] == "GMC Jammu anti-rabies clinic"
        and got["cln_gmc"]["arv_status"] == "confirmed"
    )
    assert (
        got["cln_demo2"]["name"] == "Demo clinic (unverified)"
        and got["cln_demo2"]["arv_status"] == "unverified"
    )
    assert got["cln_demo_visitor"]["name"] == "Demo visitor desk"
    assert {c["visit_time"] for c in got.values()} == {"09:00"}
    assert got["cln_gmc"]["phone"] is None  # the real clinic's number is not ours to publish
    for cid in ("cln_demo2", "cln_demo_visitor"):
        assert re.fullmatch(r"\+9199999099\d\d", got[cid]["phone"])  # reserved, never dialled
    assert seeded.db.one("SELECT COUNT(*) AS n FROM courses WHERE clinic_id = 'cln_demo_visitor'")["n"] == 0


def test_staff_logins_for_every_role_each_with_its_own_random_password(seeded):
    staff = rows(seeded, "SELECT * FROM staff ORDER BY username")
    assert [s["username"] for s in staff] == ["abc", "admin", "clinic", "oversight", "reviewer", "vet"]
    assert {s["role"] for s in staff} == {"clinic", "abc", "vet", "oversight", "reviewer", "admin"}
    assert next(s for s in staff if s["role"] == "clinic")["clinic_id"] == "cln_gmc"
    logins = seeded.report.logins
    assert set(logins) == {s["username"] for s in staff}
    assert len(set(logins.values())) == len(logins)  # no two accounts share a password
    for s in staff:
        password = logins[s["username"]]
        assert re.fullmatch(r"[0-9a-z]{4}(-[0-9a-z]{4}){2}", password)
        assert s["password_hash"].startswith("scrypt$") and password not in s["password_hash"]
        assert verify_password(password, s["password_hash"])
        assert not verify_password("demo", s["password_hash"])  # the old fixed password is gone
        assert not verify_password("wrong", s["password_hash"])


def test_every_seed_makes_new_passwords_and_writes_them_to_a_private_file(tmp_path):
    from chaarteeke.demo.seed import logins_path

    with clock.use_clock(clock.FixedClock(T0)):
        ctx = build_ctx(tmp_path)
        first = seeder.seed(ctx)
        written = logins_path(ctx)
        assert written == ctx.settings.var_dir / "demo_logins.txt"
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
        text = written.read_text()
        for name, password in first.logins.items():
            assert f"{name} " in text and password in text
        second = seeder.seed(ctx, reset=True)
        assert second.digest == first.digest  # the data is the same; the passwords are not
        assert second.logins != first.logins and not set(first.logins.values()) & set(second.logins.values())
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
        text = written.read_text()
        assert all(p in text for p in second.logins.values()) and not any(p in text for p in first.logins.values())  # fmt: skip
        for name, password in second.logins.items():
            row = ctx.db.one("SELECT password_hash FROM staff WHERE username = ?", (name,))
            assert verify_password(password, row["password_hash"])
            assert not verify_password(first.logins[name], row["password_hash"])


def test_a_file_that_was_world_readable_is_private_again_after_a_seed(tmp_path):
    from chaarteeke.demo.seed import logins_path

    with clock.use_clock(clock.FixedClock(T0)):
        ctx = build_ctx(tmp_path)
        written = logins_path(ctx)
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text("old\n")
        written.chmod(0o644)
        seeder.seed(ctx)
        assert stat.S_IMODE(written.stat().st_mode) == 0o600 and "old" not in written.read_text()


def test_a_leftover_temporary_file_is_private_before_any_password_goes_in(tmp_path):
    from chaarteeke.demo.seed import logins_path

    with clock.use_clock(clock.FixedClock(T0)):
        ctx = build_ctx(tmp_path)
        written = logins_path(ctx)
        written.parent.mkdir(parents=True, exist_ok=True)
        leftover = written.with_name(written.name + ".tmp")
        leftover.write_text("stale\n")
        leftover.chmod(0o666)
        seeder.seed(ctx)
        assert stat.S_IMODE(written.stat().st_mode) == 0o600 and not leftover.exists()


# ------------------------------------------------------------------------------ phone numbers
def test_every_number_is_in_the_reserved_range_and_unique(seeded):
    held = [
        seeded.vault.decrypt(r["phone_cipher"]) for r in rows(seeded, "SELECT phone_cipher FROM contacts")
    ]
    held += [
        seeded.vault.decrypt(r["phone_cipher"])
        for r in rows(seeded, "SELECT phone_cipher FROM missed_calls WHERE phone_cipher IS NOT NULL")
    ]
    assert held and all(FAKE_NUMBER.match(n) for n in held)
    assert len(set(held)) == len(held)
    clinic_phones = {r["phone"] for r in rows(seeded, "SELECT phone FROM clinics WHERE phone IS NOT NULL")}
    assert clinic_phones and not clinic_phones & set(held)  # a clinic's number is never a patient's
    last4 = rows(seeded, "SELECT phone_last4 AS p FROM contacts")
    assert all(re.fullmatch(r"\d{4}", r["p"]) for r in last4)


def test_no_clear_phone_number_anywhere(seeded):
    for table in ("contacts", "missed_calls", "calls", "answers", "audit", "courses", "purge_log"):
        for r in seeded.db.query(f"SELECT * FROM {table}"):
            assert not any(PHONE_SHAPE.search(str(v)) for v in dict(r).values() if v is not None), table


def test_a_linked_missed_call_no_longer_holds_the_number_and_contacts_follow_the_purge_rule(seeded):
    assert (
        seeded.db.one(
            "SELECT COUNT(*) AS n FROM missed_calls WHERE status = 'linked' AND phone_cipher IS NOT NULL"
        )["n"]
        == 0
    )
    unlinked = rows(seeded, "SELECT * FROM missed_calls WHERE status = 'unlinked'")
    assert len(unlinked) == 3 and all(u["phone_cipher"] for u in unlinked)
    now = clock.iso(T0)
    assert seeded.db.one("SELECT COUNT(*) AS n FROM contacts WHERE purge_after <= ?", (now,))["n"] == 0
    # every course that left the reminder system has a purge_log row; every other one still holds its number
    gone = {r["course_id"] for r in rows(seeded, "SELECT course_id FROM purge_log WHERE what = 'phone'")}
    held = {r["course_id"] for r in rows(seeded, "SELECT course_id FROM contacts")}
    awaiting = {r["id"] for r in rows(seeded, "SELECT id FROM courses WHERE status = 'awaiting_call'")}
    everyone = {r["id"] for r in rows(seeded, "SELECT id FROM courses")}
    assert gone | held | awaiting == everyone and not gone & held
    reasons = Counter(r["reason"] for r in rows(seeded, "SELECT reason FROM purge_log"))
    assert reasons["retention"] > 0 and reasons["withdrawn"] > 0


# -------------------------------------------------------------------- doses and dates
def test_doses_follow_the_regimen_calendar(seeded):
    for c in rows(seeded, "SELECT id, regimen_id, day0 FROM courses"):
        want = schedule(get_regimen(c["regimen_id"]), date.fromisoformat(c["day0"]))
        got = rows(seeded, "SELECT * FROM doses WHERE course_id = ? ORDER BY dose_no", (c["id"],))
        assert [(d["dose_no"], d["offset_days"], d["due_date"]) for d in got] == [
            (w.dose_no, w.offset_days, w.due_date.isoformat()) for w in want
        ]


def test_dose_statuses_agree_with_today(seeded):
    pol = seeded.policy["doses"]
    for d in rows(
        seeded, "SELECT d.*, c.status AS course_status FROM doses d JOIN courses c ON c.id = d.course_id"
    ):
        due = date.fromisoformat(d["due_date"])
        if d["status"] == "given":
            given = date.fromisoformat(d["given_on"])
            assert given <= TODAY and d["marked_by"] in ("clinic", "patient")
            assert (
                given >= due
                if d["marked_by"] == "clinic" and d["dose_no"] > 1
                else given >= due - timedelta(0)
            )
        else:
            assert d["given_on"] is None and d["marked_by"] is None
        if due > TODAY:
            assert d["status"] in ("due", "not_needed")
        if d["status"] == "late_minor":
            assert pol["late_minor_days"][0] <= (TODAY - due).days
        if d["status"] == "late_review":
            assert (TODAY - due).days >= 0


def test_course_status_agrees_with_its_doses(seeded):
    for c in rows(seeded, "SELECT * FROM courses"):
        ds = rows(seeded, "SELECT status FROM doses WHERE course_id = ?", (c["id"],))
        all_given = all(d["status"] == "given" for d in ds)
        assert (c["status"] == "completed") == all_given, c["id"]
        assert (c["completed_at"] is not None) == all_given
        assert (c["stopped_reason"] is not None) == (c["status"] == "stopped")
        assert date.fromisoformat(c["day0"]) <= TODAY
    assert Counter(r["status"] for r in rows(seeded, "SELECT status FROM courses")) >= Counter(
        {"completed": 1, "active": 1, "stopped": 1, "awaiting_call": 2}
    )


def test_first_dose_is_given_on_day_zero_by_the_clinic(seeded):
    bad = seeded.db.one(
        "SELECT COUNT(*) AS n FROM doses d JOIN courses c ON c.id = d.course_id "
        "WHERE d.dose_no = 1 AND c.status != 'awaiting_call' AND (d.status != 'given' OR d.given_on != c.day0 OR d.marked_by != 'clinic')"
    )["n"]
    assert bad == 0


def test_nothing_is_dated_after_now(seeded):
    now = clock.iso(T0)
    for table, col in (
        ("courses", "created_at"),
        ("calls", "scheduled_for"),
        ("calls", "ended_at"),
        ("bite_reports", "created_at"),
        ("alerts", "created_at"),
        ("alerts", "closed_at"),
        ("alerts", "first_seen_by_vet_at"),
        ("answers", "at"),
        ("missed_calls", "at"),
        ("courses", "completed_at"),
        ("callback_requests", "requested_at"),
    ):
        assert seeded.db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {col} > ?", (now,))["n"] == 0, (
            table,
            col,
        )


# ------------------------------------------------------------------------------ calls
def test_calls_use_the_schedulers_job_keys_and_times(seeded):
    pol = seeded.policy["calls"]
    tz = seeded.settings.local_tz
    for c in rows(seeded, "SELECT * FROM calls WHERE attempt = 1 AND kind IN ('reminder','missed_dose')"):
        due = date.fromisoformat(
            seeded.db.one(
                "SELECT due_date FROM doses WHERE course_id = ? AND dose_no = ?",
                (c["course_id"], c["dose_no"]),
            )["due_date"]
        )
        day, hhmm = (
            (due - timedelta(days=1), pol["reminder_local_time"])
            if c["kind"] == "reminder"
            else (due + timedelta(days=1), pol["morning_after_local_time"])
        )
        assert c["scheduled_for"] == clock.iso(clock.at_local(day, hhmm, tz))
        assert c["job_key"] == f"{c['kind']}:{c['course_id']}:{c['dose_no']}:{day.isoformat()}"
    for c in rows(seeded, "SELECT * FROM calls WHERE kind = 'first' AND attempt = 1"):
        assert c["job_key"] == f"first:{c['course_id']}"


def test_calls_stay_inside_the_window_and_the_attempt_limit(seeded):
    pol = seeded.policy["calls"]
    tz = clock.local_tz(seeded.settings.local_tz)
    for c in rows(seeded, "SELECT * FROM calls WHERE started_at IS NOT NULL"):
        local = clock.parse_iso(c["started_at"]).astimezone(tz)
        assert pol["window_start_local"] <= local.strftime("%H:%M") < pol["window_end_local"], c["id"]
        assert 1 <= c["attempt"] <= pol["max_attempts"]
    assert (
        seeded.db.one(
            "SELECT COUNT(*) AS n FROM calls WHERE status IN ('scheduled','dialing','ringing','answered')"
        )["n"]
        == 0
    )


def test_the_scheduler_has_nothing_to_retry_or_redial_from_the_past(seeded):
    # a no_answer / failed row below the attempt limit would be retried now; each must have its next row already
    pending = rows(
        seeded,
        "SELECT * FROM calls WHERE status IN ('no_answer','failed') AND attempt < ?",
        (seeded.policy["calls"]["max_attempts"],),
    )
    for c in pending:
        base = re.sub(r":a\d+$", "", c["job_key"])
        assert seeded.db.one("SELECT 1 FROM calls WHERE job_key = ?", (f"{base}:a{c['attempt'] + 1}",)), c[
            "job_key"
        ]


def test_retries_follow_the_policy_gaps(seeded):
    gaps = seeded.policy["calls"]["retry_gaps_min"]
    for c in rows(seeded, "SELECT * FROM calls WHERE attempt > 1 AND status != 'cancelled'"):
        base = re.sub(r":a\d+$", "", c["job_key"])
        prev_key = base if c["attempt"] == 2 else f"{base}:a{c['attempt'] - 1}"
        prev = seeded.db.one("SELECT * FROM calls WHERE job_key = ?", (prev_key,))
        gap = clock.parse_iso(c["scheduled_for"]) - clock.parse_iso(prev["ended_at"])
        assert gap == timedelta(minutes=gaps[c["attempt"] - 2])


def test_a_dose_taken_answer_leaves_the_dose_open_for_the_clinic_to_confirm(seeded):
    """'Yes, I took it' is the patient's word, not the clinic's mark: the dose stays open on the clinic's list."""
    yes = rows(
        seeded,
        "SELECT c.course_id, c.dose_no FROM answers a JOIN calls c ON c.id = a.call_id "
        "WHERE a.question = 'dose_taken' AND a.value = 'yes'",
    )
    assert yes
    for y in yes:
        d = seeded.db.one(
            "SELECT * FROM doses WHERE course_id = ? AND dose_no = ?", (y["course_id"], y["dose_no"])
        )
        assert d["status"] != "given", d["status"]  # (a clinic may later say the dose is not needed)
        assert d["marked_by"] is None and d["given_on"] is None


# ----------------------------------------------------------------- reports and statistics
def test_first_call_answers_match_the_reports(seeded):
    """The answers use the words the call runner stores, and agree with the report built from them."""
    got = rows(
        seeded,
        "SELECT c.course_id, a.question, a.value FROM answers a JOIN calls c ON c.id = a.call_id WHERE c.kind = 'first'",
    )
    by_course: dict[str, dict[str, str]] = {}
    for g in got:
        by_course.setdefault(g["course_id"], {})[g["question"]] = g["value"]
    for r in rows(seeded, "SELECT * FROM bite_reports"):
        a = by_course[r["course_id"]]
        assert a["consent_reminders"] == "yes" and a["consent_questions"] == "yes" and a["dose_today"] == "1"
        assert a["place"] == ("none" if r["location_status"] == "no_place_heard" else "recorded")
        for question in ("place_type", "animal", "signs"):
            if r[question] == "no_answer":
                assert question not in a
            else:
                assert a[question] == r[question]
        assert "notch" not in a and "lang" not in a


def test_declined_and_withdrawn_courses_are_stopped_the_way_the_core_does_it(seeded):
    declined = rows(
        seeded,
        "SELECT c.* FROM courses c JOIN calls k ON k.course_id = c.id JOIN answers a ON a.call_id = k.id "
        "WHERE a.question = 'withdraw' AND a.value = 'declined'",
    )
    assert declined
    for c in declined:
        assert (c["status"], c["stopped_reason"]) == ("stopped", "the patient said no to reminders")
        assert c["consent_reminders_at"] is None
        assert seeded.db.one(
            "SELECT 1 FROM purge_log WHERE course_id = ? AND reason = 'withdrawn'", (c["id"],)
        )
        assert seeded.db.one("SELECT 1 FROM contacts WHERE course_id = ?", (c["id"],)) is None
    withdrawn = rows(
        seeded, "SELECT * FROM courses WHERE stopped_reason = 'the patient pressed 9 to stop the calls'"
    )
    assert withdrawn and all(
        seeded.db.one(
            "SELECT 1 FROM answers a JOIN calls k ON k.id = a.call_id WHERE k.course_id = ? AND a.question = 'withdraw'",
            (c["id"],),
        )
        for c in withdrawn
    )


def test_reports_carry_ward_and_cluster_together(seeded):
    for r in rows(seeded, "SELECT * FROM bite_reports"):
        placed = r["location_status"] in ("matched", "decided")
        assert (r["ward_no"] is not None) == placed == (r["cluster_id"] is not None)
        if placed:
            assert seeded.clusters.for_ward(r["ward_no"]) == r["cluster_id"]
        assert date.fromisoformat(r["bite_week"]).weekday() == 0


def test_two_clusters_run_lower_and_are_flagged(seeded):
    cov = coverage(seeded)
    weak = seeded.report.weak_cluster_ids
    assert len(weak) == 2
    shares = {c.id: c.share.share for c in cov.clusters}
    assert all(c.share.status == "ok" for c in cov.clusters)
    assert all(
        0.30 <= shares[w] <= 0.55 for w in weak
    )  # clear enough gaps for the corrected rule to flag on a small demo
    strong = [s for cid, s in shares.items() if cid not in weak]
    assert all(s >= 0.70 for s in strong) and min(strong) > max(shares[w] for w in weak)
    assert set(weak) <= set(cov.look_here) and len(cov.look_here) <= 3


def test_could_not_see_rate_is_realistic(seeded):
    cov = coverage(seeded)
    unsure = cov.city.unsure
    street = cov.city.n + unsure
    assert 0.08 <= unsure / street <= 0.25
    assert cov.city.bound_low < cov.city.share < cov.city.bound_high


def test_place_types_vary(seeded):
    got = Counter(r["place_type"] for r in rows(seeded, "SELECT place_type FROM bite_reports"))
    assert set(got) == {"home_street", "school", "hospital", "bus_market", "other", "no_answer"}


# --------------------------------------------------------------------------- alerts etc.
def test_alerts_in_every_state_all_seen_by_a_vet(seeded):
    got = Counter(r["status"] for r in rows(seeded, "SELECT status FROM alerts"))
    assert set(got) == {"new", "claimed", "acted", "not_acted"} and got["new"] == 2
    for a in rows(seeded, "SELECT * FROM alerts"):
        assert a["first_seen_by_vet_at"] and a["local_day"]
        assert (a["claimed_by"] is not None) == (a["status"] != "new")
        assert (a["closed_at"] is not None) == (a["status"] in ("acted", "not_acted"))
        if a["status"] == "not_acted":
            assert a["outcome_note"]
        r = seeded.db.one(
            "SELECT signs, place_type, ward_no FROM bite_reports WHERE id = ?", (a["report_id"],)
        )
        assert (r["signs"], r["place_type"], r["ward_no"]) == ("yes", a["place_type"], a["ward_no"])
    from chaarteeke import alerts

    assert alerts.unseen(seeded) == []
    assert Counter(c["status"] for c in rows(seeded, "SELECT status FROM callback_requests")) == {
        "open": 1,
        "done": 1,
    }


def test_review_queue(seeded):
    caps = rows(seeded, "SELECT * FROM place_captures ORDER BY report_id")
    assert len(caps) == 12
    for c in caps:
        r = seeded.db.one("SELECT * FROM bite_reports WHERE id = ?", (c["report_id"],))
        assert r["location_status"] == "needs_review" and r["ward_no"] is None and r["cluster_id"] is None
        assert c["transcript"] and c["asr_status"] == "done" and c["decided_at"] is None
        clip = seeded.settings.data_dir / "demo" / "clips" / f"{c['audio_path'] or ''}".rsplit("/", 1)[-1]
        # A clip is claimed only when its file is there: the recorded demo clips are optional data.
        assert (c["audio_path"] is None) or clip.is_file()
        assert c["confidence"] in ("possible", "none")
        assert (
            json.loads(c["candidates_json"]) == []
            if c["confidence"] == "none"
            else json.loads(c["candidates_json"])
        )
        assert clock.parse_iso(c["expires_at"]) - clock.parse_iso(r["created_at"]) == timedelta(days=7)
        assert clock.parse_iso(c["expires_at"]) > T0
    assert any(re.search(r"[ऀ-ॿ]", c["transcript"]) for c in caps)  # some Hindi-script transcripts


def test_audio_path_is_set_only_for_clips_that_exist(tmp_path, fixed_clock):
    data = tmp_path / "data"
    (data / "demo" / "clips").mkdir(parents=True)
    (data / "demo" / "clips" / "3.wav").write_bytes(b"RIFF")
    for name in ("wards.geojson", "gazetteer.json"):
        (data / name).symlink_to(DATA_DIR / name)
    ctx = build_ctx(tmp_path)
    ctx.settings = ctx.settings.model_copy(update={"data_dir": data})
    gen = seeder._Gen(ctx, 5)
    gen.build()
    paths = {c["report_id"]: c["audio_path"] for c in gen.rows["place_captures"]}
    assert sorted(p for p in paths.values() if p) == ["data/demo/clips/3.wav"]
    assert len(paths) == 12


def test_field_work(seeded):
    assert seeded.db.one("SELECT COUNT(*) AS n FROM worked_wards")["n"] == 10
    assert seeded.db.one("SELECT COUNT(*) AS n FROM sight_counts")["n"] == 28
    assert seeded.db.one("SELECT COUNT(*) AS n FROM aliases")["n"] == 4
    assert (
        seeded.db.one("SELECT COUNT(*) AS n FROM sight_counts WHERE notched + unnotched + unsure < 12")["n"]
        == 0
    )
    weak = seeded.report.weak_cluster_ids
    worked = {r["ward_no"] for r in rows(seeded, "SELECT ward_no FROM worked_wards")}
    assert all(any(w in worked for w in seeded.clusters.get(c).ward_nos) for c in weak)


def test_the_data_file_the_seed_reads_is_the_committed_one():
    assert Path(DATA_DIR / "clusters.json").exists()


def test_every_seeded_course_is_demo_history_the_scheduler_leaves_alone(seeded):
    assert seeded.db.one("SELECT COUNT(*) AS n FROM courses WHERE source != 'demo'")["n"] == 0


# ------------------------------------------------------------------ a reset takes the voice clips with it
def test_reset_deletes_the_voice_clips_the_demo_phone_recorded(tmp_path, fixed_clock):
    ctx = build_ctx(tmp_path)
    seeder.seed(ctx)
    clips = ctx.settings.var_dir / "clips"
    clips.mkdir(parents=True)
    mine = clips / "rep_visitor.wav"
    mine.write_bytes(b"RIFF....WAVE")
    with ctx.db.transaction() as conn:
        rid = conn.execute("SELECT id FROM bite_reports LIMIT 1").fetchone()["id"]
        conn.execute("DELETE FROM place_captures WHERE report_id=?", (rid,))
        conn.execute(
            "INSERT INTO place_captures(report_id, audio_path, asr_status, expires_at) VALUES (?,?, 'pending', ?)",
            (rid, str(mine), clock.iso(clock.now())),
        )
    seeder.seed(ctx, reset=True)
    assert not mine.exists()
    ctx.db.close()


def test_a_clip_that_cannot_be_deleted_stops_the_reset_before_any_row_is_lost(tmp_path, fixed_clock):
    ctx = build_ctx(tmp_path)
    before = seeder.seed(ctx).digest
    with ctx.db.transaction() as conn:
        rid = conn.execute("SELECT id FROM bite_reports LIMIT 1").fetchone()["id"]
        conn.execute("DELETE FROM place_captures WHERE report_id=?", (rid,))
        conn.execute(
            "INSERT INTO place_captures(report_id, audio_path, asr_status, expires_at) VALUES (?,?, 'pending', ?)",
            (rid, str(tmp_path / "elsewhere.wav"), clock.iso(clock.now())),
        )
    with pytest.raises(seeder.SeedError) as e:
        seeder.seed(ctx, reset=True)
    assert e.value.code == "clip_not_deleted"
    assert ctx.db.one("SELECT COUNT(*) AS n FROM courses")["n"] > 1000 and before
    ctx.db.close()
