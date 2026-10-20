"""The pure call flows: every path, driven by scripted events with no telephone and no database."""

from __future__ import annotations

from collections import deque
from datetime import date, timedelta

import pytest
from ivr_support import fast_policy

from chaarteeke.ivr import flow as fl
from chaarteeke.ivr.flow import (
    Collect,
    DoseInfo,
    End,
    FirstCallFlow,
    Hangup,
    Key,
    MarkTaken,
    MorningAfterFlow,
    NoInput,
    NoticeFlow,
    Play,
    PlayDone,
    Record,
    Recorded,
    ReminderFlow,
    Save,
    Start,
    Withdraw,
)

DAY0 = date(2026, 10, 19)
HEARD = Recorded("r1", True, 2.0, 1.2)
SILENT = Recorded("r1", True, 0.0, 0.0)
POLICY = fast_policy()
REPEATS = POLICY["calls"]["no_input_repeats"]
SEEN: set[str] = set()  # every prompt id any scenario played


def K(*digits: str) -> list:
    return [Key(d) for d in digits]


def schedule(days: tuple[int, ...]) -> list[DoseInfo]:
    return [DoseInfo(i + 1, DAY0 + timedelta(days=d)) for i, d in enumerate(days)]


TRC = schedule((0, 3, 7, 28))
ESSEN = schedule((0, 3, 7, 14, 28))


def first(doses=TRC, ask_questions=True, today=DAY0, next_dose="table", **kw) -> FirstCallFlow:
    """A first call for a schedule: the next dose still to come is the first one not yet given. Dose 1 is the
    enrolment visit, so by default dose 2 is next; `next_dose` overrides it (None: nothing is left)."""
    nxt = doses[1] if next_dose == "table" else next_dose
    return FirstCallFlow(
        policy=kw.pop("policy", POLICY),
        lang="hi",
        clinic_id="cln_a",
        next_dose=nxt,
        total=len(doses),
        visit_time="0900",
        today=today,
        ask_questions=ask_questions,
        clinic_phone=kw.pop("clinic_phone", "0191234567"),
        **kw,
    )


def run(flow: fl.Flow, answers: list) -> list:
    """Start the flow and answer each question or recording from `answers`; return every action."""
    pending, out = deque(answers), []
    actions = flow.step(Start())
    while True:
        out.extend(actions)
        if flow.done:
            for a in out:
                if isinstance(a, Play):
                    SEEN.update(a.ids)
            return out
        actions = flow.step(pending.popleft())


def plays(out) -> list[list[str]]:
    return [a.ids for a in out if isinstance(a, Play)]


def flat(out) -> list[str]:
    return [i for ids in plays(out) for i in ids]


def saves(out) -> list[tuple[str, str]]:
    return [(a.question, a.value) for a in out if isinstance(a, Save)]


def slots_of(out, prompt_id: str) -> dict:
    for a in out:
        if isinstance(a, Play):
            for s in a.prompts:
                if s.id == prompt_id:
                    return dict(s.slots)
    raise AssertionError(f"{prompt_id} was not played")


# ------------------------------------------------------------------ first call
def test_first_call_happy_path_four_dose_regimen():
    out = run(first(), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    assert flat(out) == [
        "FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q", "FC_QCONSENT", "FC_PLACE", "FC_PLACETYPE",
        "FC_ANIMAL", "FC_NOTCH", "FC_SICK", "FC_CLOSE",
    ]  # fmt: skip
    assert saves(out) == [
        ("consent_reminders", "yes"),
        ("consent_questions", "yes"),
        ("place", "recorded"),
        ("place_type", "home_street"),
        ("animal", "notched"),
        ("signs", "no"),
    ]
    assert isinstance(out[-1], End) and out[-1].reason == "completed"
    # the next dose is read from the schedule, never from an answer about the dog
    assert slots_of(out, "FC_CLOSE") == {
        "n": 2, "total": 4, "date": DAY0 + timedelta(days=3), "time": "0900", "clinic": "cln_a"
    }  # fmt: skip
    assert slots_of(out, "FC_HELLO") == {"clinic": "cln_a"}


def test_the_place_question_records_with_the_policy_limits():
    out = run(first(), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    rec = next(a for a in out if isinstance(a, Record))
    assert (rec.max_s, rec.silence_s) == (
        POLICY["calls"]["place_record_max_s"],
        POLICY["calls"]["place_record_silence_s"],
    )
    waits = {a.timeout_s for a in out if isinstance(a, Collect)}
    assert waits == {POLICY["calls"]["no_input_wait_s"]}


def test_the_close_reads_the_next_dose_from_the_table_whatever_the_patient_pressed():
    """FC_DOSE is gone: no key the patient presses decides which dose or which date is read out."""
    expected = {"n": 2, "total": 4, "date": DAY0 + timedelta(days=3), "time": "0900", "clinic": "cln_a"}
    for answers in (
        [*K("1", "1"), HEARD, *K("1", "1", "1", "2")],
        [*K("1", "2"), *K()],
        [*K("1", "1"), HEARD, *K("2", "4", "3")],
        [*K("1", "1"), HEARD, *K("5", "1", "1", "1", "1")],
    ):
        out = run(first(), answers)
        assert slots_of(out, "FC_CLOSE") == expected
        assert "FC_DOSE" not in flat(out) and not any(q == "dose_today" for q, _ in saves(out))


def test_five_dose_close_names_dose_and_total_from_the_table():
    out = run(first(ESSEN, next_dose=ESSEN[2]), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    close = slots_of(out, "FC_CLOSE")
    assert (close["n"], close["total"], close["date"]) == (3, 5, DAY0 + timedelta(days=7))


def test_two_dose_reexposure_regimen():
    out = run(first(schedule((0, 3))), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    assert slots_of(out, "FC_CLOSE")["total"] == 2 and slots_of(out, "FC_CLOSE")["n"] == 2


def test_no_dose_left_closes_with_the_last_dose_line():
    out = run(first(next_dose=None), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    assert flat(out)[-1] == "FC_CLOSE_LAST" and "FC_CLOSE" not in flat(out)


def test_a_next_dose_date_already_past_asks_the_patient_to_come_and_never_says_the_clinic_will_set_dates():
    # dose 2 was due 3 days after day 0; on day 5 that date has gone
    out = run(first(today=DAY0 + timedelta(days=5)), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    assert flat(out)[-1] == "FC_CLOSE_COME" and "FC_CLOSE" not in flat(out)
    assert slots_of(out, "FC_CLOSE_COME") == {"clinic": "cln_a", "clinic_phone": "0191234567"}
    assert "FC_CLOSE_NODATE" not in fl.REQUIRED_PROMPTS


def test_a_past_due_close_needs_the_clinic_phone_number():
    with pytest.raises(ValueError, match="phone"):
        first(today=DAY0 + timedelta(days=5), clinic_phone=None)
    first(today=DAY0 + timedelta(days=2), clinic_phone=None)  # a date still to come needs none


def test_a_next_dose_due_today_is_still_read_out():
    out = run(first(today=DAY0 + timedelta(days=3)), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    assert flat(out)[-1] == "FC_CLOSE"
    assert slots_of(out, "FC_CLOSE")["date"] == DAY0 + timedelta(days=3)


@pytest.mark.parametrize(
    ("keys", "animal", "asks_notch"),
    [
        (("1", "1"), "notched", True),
        (("1", "2"), "unnotched", True),
        (("1", "3"), "unsure", True),
        (("2",), "pet", False),
        (("3",), "other", False),
        (("4",), "animal_unknown", False),
    ],
)
def test_animal_answers_map_to_the_stored_values(keys, animal, asks_notch):
    out = run(first(), [*K("1", "1"), HEARD, *K("1", *keys, "2")])
    assert ("animal", animal) in saves(out)
    assert ("FC_NOTCH" in flat(out)) is asks_notch


@pytest.mark.parametrize(
    ("key", "stored"),
    [("1", "home_street"), ("2", "school"), ("3", "hospital"), ("4", "bus_market"), ("5", "other")],
)
def test_place_type_keys_map_to_the_stored_values(key, stored):
    out = run(first(), [*K("1", "1"), HEARD, *K(key, "2", "2")])
    assert ("place_type", stored) in saves(out)


@pytest.mark.parametrize(("key", "stored"), [("1", "yes"), ("2", "no"), ("3", "dont_know")])
def test_signs_answers_map_to_the_stored_values(key, stored):
    out = run(first(), [*K("1", "1"), HEARD, *K("1", "2", key)])
    assert ("signs", stored) in saves(out)
    assert ("FC_SICK_YES" in flat(out)) is (key == "1")
    assert flat(out).index("FC_SICK_YES") < flat(out).index("FC_CLOSE") if key == "1" else True


def test_unconfirmed_eligibility_asks_no_data_questions_at_all():
    out = run(first(ask_questions=False), K("1"))
    assert flat(out) == ["FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q", "FC_CLOSE"]
    assert saves(out) == [("consent_reminders", "yes")]


def test_declining_the_questions_skips_them_but_not_the_reminders():
    out = run(first(), K("1", "2"))
    assert flat(out) == ["FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q", "FC_QCONSENT", "FC_CLOSE"]
    assert ("consent_questions", "no") in saves(out)
    assert not {"FC_PLACE", "FC_PLACETYPE", "FC_ANIMAL", "FC_SICK"} & set(flat(out))


def test_place_is_asked_again_once_then_the_call_moves_on_without_a_guess():
    out = run(first(), [*K("1", "1"), SILENT, Recorded("r2", False, None), *K("1", "1", "1", "2")])
    assert flat(out)[flat(out).index("FC_PLACE") : flat(out).index("FC_PLACETYPE")] == [
        "FC_PLACE",
        "FC_PLACE_AGAIN",
    ]
    assert ("place", "none") in saves(out)
    assert ("place", "recorded") not in saves(out)
    assert len([a for a in out if isinstance(a, Record)]) == 2


def test_place_heard_on_the_second_try_is_kept():
    out = run(first(), [*K("1", "1"), SILENT, HEARD, *K("1", "1", "1", "2")])
    assert "FC_PLACE_AGAIN" in flat(out) and ("place", "recorded") in saves(out)


def test_a_short_answer_the_phone_system_counted_as_zero_seconds_is_kept():
    """Asterisk reports whole seconds, so a place named in under a second arrives with talking_s = 0. The
    measured voiced seconds decide, not that figure."""
    short = Recorded("r1", True, 0.0, 0.8)
    out = run(first(), [*K("1", "1"), short, *K("1", "1", "1", "2")])
    assert ("place", "recorded") in saves(out) and "FC_PLACE_AGAIN" not in flat(out)
    assert len([a for a in out if isinstance(a, Record)]) == 1


def test_the_phone_systems_talking_time_alone_does_not_make_speech():
    quiet = Recorded("r1", True, 3.0, 0.1)  # Asterisk says 3 s of talking; the audio holds 0.1 s of voice
    out = run(first(), [*K("1", "1"), quiet, quiet, *K("1", "1", "1", "2")])
    assert ("place", "none") in saves(out) and "FC_PLACE_AGAIN" in flat(out)


def test_the_voiced_seconds_minimum_is_the_policys_and_is_inclusive():
    floor = POLICY["calls"]["speech_min_voiced_s"]
    just_enough = Recorded("r1", True, 0.0, floor)
    assert ("place", "recorded") in saves(run(first(), [*K("1", "1"), just_enough, *K("1", "1", "1", "2")]))
    just_short = Recorded("r1", True, 0.0, round(floor - 0.02, 2))
    out = run(first(), [*K("1", "1"), just_short, just_short, *K("1", "1", "1", "2")])
    assert ("place", "none") in saves(out)
    stricter = first(policy=fast_policy(speech_min_voiced_s=floor + 0.5))
    out = run(stricter, [*K("1", "1"), Recorded("r1", True, 0.0, floor + 0.2), HEARD, *K("1", "1", "1", "2")])
    assert "FC_PLACE_AGAIN" in flat(out)  # the threshold read is the flow's own policy


def test_a_recording_that_finished_ok_without_a_measurement_is_refused():
    flow = first()
    flow.step(Start())
    for key in K("1", "1"):
        flow.step(key)
    with pytest.raises(ValueError, match="not measured"):
        flow.step(Recorded("r1", True, 2.0))


def test_declining_reminders_withdraws_and_saves_nothing():
    out = run(first(), K("2"))
    assert Withdraw("declined") in out
    assert flat(out) == ["FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q", "FC_CONSENT_NO"]
    assert saves(out) == []
    assert out[-1] == End("declined")


def test_key_nine_asks_to_confirm_and_a_second_nine_withdraws():
    out = run(first(), K("9", "9"))
    assert flat(out) == ["FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q", "OPT_CONFIRM", "OPT_DONE"]
    assert Withdraw("withdrawn") in out and out[-1] == End("withdrawn")
    assert saves(out) == []


def test_key_nine_then_one_keeps_the_calls_and_repeats_the_question():
    out = run(first(), K("9", "1", "1", "2", "1"))
    ids = plays(out)
    assert ids[ids.index(["OPT_CONFIRM"]) + 1] == ["FC_CONSENT_Q"]
    assert Withdraw("withdrawn") not in out
    assert ("consent_reminders", "yes") in saves(out)


def test_key_nine_works_at_a_later_question_too():
    out = run(first(), [*K("1", "1"), HEARD, Key("9"), Key("9")])
    assert Withdraw("withdrawn") in out and out[-1] == End("withdrawn")
    assert ("consent_reminders", "yes") in saves(out)  # what was already agreed stays saved


def test_key_nine_while_the_place_is_asked_or_recorded_asks_to_stop_and_a_second_nine_withdraws():
    out = run(first(), [*K("1", "1"), Key("9"), Key("9")])
    assert flat(out)[-3:] == ["FC_PLACE", "OPT_CONFIRM", "OPT_DONE"]
    assert Withdraw("withdrawn") in out and out[-1] == End("withdrawn")
    assert ("place", "recorded") not in saves(out) and ("place", "none") not in saves(out)


def test_key_nine_then_one_at_the_place_question_asks_the_same_question_again():
    out = run(first(), [*K("1", "1"), Key("9"), Key("1"), HEARD, *K("1", "1", "1", "2")])
    ids = flat(out)
    assert ids[ids.index("OPT_CONFIRM") + 1] == "FC_PLACE"  # not FC_PLACE_AGAIN: nothing was missed
    assert "FC_PLACE_AGAIN" not in ids and ("place", "recorded") in saves(out)
    assert Withdraw("withdrawn") not in out


def test_the_withdraw_question_has_its_own_tries_and_silence_does_not_withdraw():
    out = run(first(), [Key("9"), NoInput(), NoInput(), NoInput()])
    assert Withdraw("withdrawn") not in out
    assert flat(out)[-1] == "SYS_GIVEUP_FIRST" and out[-1] == End("gave_up")


def test_silence_repeats_twice_then_gives_up_and_saves_nothing_for_that_question():
    out = run(first(), [NoInput(), NoInput(), NoInput()])
    assert plays(out) == [
        ["FC_HELLO"], ["FC_NOTICE"], ["FC_CONSENT_Q"],
        ["SYS_NOINPUT", "FC_CONSENT_Q"], ["SYS_NOINPUT", "FC_CONSENT_Q"], ["SYS_GIVEUP_FIRST"],
    ]  # fmt: skip
    assert out[-1] == End("gave_up") and saves(out) == []
    assert REPEATS == 2


def test_a_wrong_key_uses_up_a_try_just_like_silence():
    out = run(first(), K("7", "7", "7"))
    assert plays(out)[3:] == [
        ["SYS_WRONGKEY", "FC_CONSENT_Q"],
        ["SYS_WRONGKEY", "FC_CONSENT_Q"],
        ["SYS_GIVEUP_FIRST"],
    ]
    assert out[-1] == End("gave_up")


def test_wrong_keys_and_silence_share_the_same_three_tries():
    out = run(first(), [Key("7"), NoInput(), Key("5")])
    assert out[-1] == End("gave_up")
    assert flat(out)[-1] == "SYS_GIVEUP_FIRST"


def test_a_right_key_on_the_last_try_still_counts():
    out = run(first(ask_questions=False), [NoInput(), Key("7"), Key("1"), Key("1")])
    assert ("consent_reminders", "yes") in saves(out)


def test_giving_up_mid_questions_keeps_earlier_answers_and_not_the_missing_one():
    out = run(first(), [*K("1", "1"), HEARD, NoInput(), NoInput(), NoInput()])
    assert [q for q, _ in saves(out)] == ["consent_reminders", "consent_questions", "place"]
    assert out[-1] == End("gave_up")
    assert "FC_CLOSE" not in flat(out)


def test_a_call_cut_mid_way_is_a_hangup_and_nothing_more_is_asked():
    f = first()
    f.step(Start())
    out = f.step(Hangup())
    assert out == [End("hangup")] and f.done
    assert f.step(Key("1")) == []


def test_the_next_attempt_asks_only_the_questions_still_missing():
    prior = {
        "consent_reminders": "yes", "consent_questions": "yes", "place": "recorded", "place_type": "other",
    }  # fmt: skip
    out = run(first(prior=prior), K("2", "3"))
    assert flat(out) == ["FC_HELLO", "FC_ANIMAL", "FC_SICK", "FC_CLOSE"]
    assert saves(out) == [("animal", "pet"), ("signs", "dont_know")]
    assert slots_of(out, "FC_CLOSE")["n"] == 2


def test_a_call_with_everything_answered_only_says_hello_and_the_date():
    prior = {
        "consent_reminders": "yes", "consent_questions": "yes", "place": "none",
        "place_type": "other", "animal": "pet", "signs": "no",
    }  # fmt: skip
    out = run(first(prior=prior), [])
    assert flat(out) == ["FC_HELLO", "FC_CLOSE"] and saves(out) == []
    assert slots_of(out, "FC_CLOSE")["n"] == 2


def test_questions_declined_earlier_stay_declined():
    prior = {"consent_reminders": "yes", "consent_questions": "no"}
    assert flat(run(first(prior=prior), [])) == ["FC_HELLO", "FC_CLOSE"]


def test_the_language_menu_plays_all_three_languages_and_switches_the_call():
    f = first(ask_language=True)
    assert f.langs == ("hi", "doi", "ur")
    out = run(f, [Key("2"), *K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    menu = next(a for a in out if isinstance(a, Play))
    assert [(s.id, s.lang) for s in menu.prompts] == [
        ("SYS_LANG", "hi"),
        ("SYS_LANG", "doi"),
        ("SYS_LANG", "ur"),
    ]
    assert saves(out)[0] == ("lang", "doi") and f.lang == "doi"
    assert first().langs == ("hi",)


def test_a_failed_playback_ends_the_call():
    f = first()
    f.step(Start())
    assert f.step(PlayDone(True)) == []
    assert f.step(PlayDone(False)) == [End("playback_failed")]


def test_stepping_a_new_flow_with_anything_but_start_is_refused():
    with pytest.raises(ValueError):
        first().step(Key("1"))


# ------------------------------------------------------------------- reminder
def reminder(**kw) -> ReminderFlow:
    args = {
        "policy": POLICY,
        "lang": "hi",
        "clinic_id": "cln_a",
        "dose_no": 3,
        "total": 4,
        "due": DAY0 + timedelta(days=7),
        "visit_time": "0900",
    }
    return ReminderFlow(**{**args, **kw})


def test_reminder_says_nothing_about_the_clinic_a_dose_or_a_ward_until_the_listener_presses_1():
    f = reminder(team_ward=7)
    first_step = f.step(Start())
    assert [type(a) for a in first_step] == [Play, Collect]
    assert first_step[0].ids == ["RM_GATE"] and first_step[0].prompts[0].slots == {}
    assert set(first_step[1].valid_keys) == {"1", "8"}  # not even 9: nobody has said who they are yet
    out = [*first_step, *f.step(Key("1"))]
    heard = [i for ids in plays(out) for i in ids]
    assert heard == ["RM_GATE", "RM_HELLO", "RM_EVE_BODY", "FIN_TEAM", "RM_EVE_END"]
    # nothing that names the clinic, a dose or a ward comes before the key
    before = [i for ids in plays(first_step) for i in ids]
    assert not {"RM_HELLO", "RM_EVE_BODY", "RM_EVE_BODY_OUT", "FIN_TEAM", "RM_EVE_END", "RM_WHO"} & set(
        before
    )


def test_reminder_says_clinic_dose_and_date_then_waits_for_a_key():
    out = run(reminder(), [Key("1"), NoInput()])
    assert plays(out) == [["RM_GATE"], ["RM_HELLO", "RM_EVE_BODY", "RM_EVE_END"]]
    assert next(a for a in out if isinstance(a, Play) and "RM_EVE_BODY" in a.ids).listen
    assert slots_of(out, "RM_EVE_BODY") == {
        "n": 3, "total": 4, "date": DAY0 + timedelta(days=7), "time": "0900", "clinic": "cln_a"
    }  # fmt: skip
    keys = [a for a in out if isinstance(a, Collect)][1].valid_keys
    assert set(keys) == {"9", "*"}
    assert out[-1] == End("completed") and saves(out) == []


@pytest.mark.parametrize(
    "answers", [[NoInput()] * 3, [Key("2"), Key("3"), Key("4")], [Key("9"), NoInput(), Key("0")]]
)
def test_reminder_without_a_1_ends_politely_and_says_nothing_else(answers):
    out = run(reminder(team_ward=7, vaccine_out=True, clinic_phone="0191234567"), answers)
    assert flat(out)[-1] == "SYS_BYE" and out[-1] == End("gave_up")
    assert not {"RM_HELLO", "RM_EVE_BODY", "RM_EVE_BODY_OUT", "FIN_TEAM", "RM_EVE_END"} & set(flat(out))
    assert Withdraw("withdrawn") not in out  # a 9 at the gate stops nothing


def test_reminder_key_eight_at_the_gate_is_a_wrong_number_after_confirming():
    out = run(reminder(), K("8", "8"))
    assert Withdraw("wrong_number") in out and flat(out)[-2:] == ["WN_CONFIRM", "WN_DONE"]
    assert out[-1] == End("wrong_number")
    assert not {"RM_HELLO", "RM_EVE_BODY"} & set(flat(out))
    kept = run(
        reminder(), K("8", "1", "1", "9", "1")
    )  # keeps listening: asked the gate again, then the reminder
    assert Withdraw("wrong_number") not in kept and "RM_EVE_BODY" in flat(kept)


def test_reminder_when_the_vaccine_is_out_says_to_call_the_clinic_first():
    out = run(reminder(vaccine_out=True, clinic_phone="0191234567"), [Key("1"), NoInput()])
    assert flat(out) == ["RM_GATE", "RM_HELLO", "RM_EVE_BODY_OUT", "RM_EVE_END"]
    assert slots_of(out, "RM_EVE_BODY_OUT")["clinic_phone"] == "0191234567"


def test_reminder_vaccine_out_needs_the_clinic_phone():
    with pytest.raises(ValueError):
        reminder(vaccine_out=True)


def test_reminder_adds_the_worked_area_line_only_after_the_gate():
    out = run(reminder(team_ward=7), [Key("1"), NoInput()])
    assert flat(out) == ["RM_GATE", "RM_HELLO", "RM_EVE_BODY", "FIN_TEAM", "RM_EVE_END"]
    assert slots_of(out, "FIN_TEAM") == {"ward": 7}


def test_reminder_on_a_shared_phone_says_whose_register_entry_it_is():
    out = run(reminder(register="2345"), [Key("1"), NoInput()])
    assert flat(out) == ["RM_GATE", "RM_HELLO", "RM_WHO", "RM_EVE_BODY", "RM_EVE_END"]
    assert slots_of(out, "RM_WHO") == {"register": "2345"}
    assert "RM_WHO" not in flat(run(reminder(), [Key("1"), NoInput()]))


def test_reminder_star_hears_it_again_without_the_hello_and_only_so_many_times():
    out = run(reminder(), [Key("1"), *K("*", "*", "*")])
    assert (
        plays(out)
        == [["RM_GATE"], ["RM_HELLO", "RM_EVE_BODY", "RM_EVE_END"]]
        + [["RM_EVE_BODY", "RM_EVE_END"]] * REPEATS
    )
    assert out[-1] == End("completed")


def test_reminder_nine_then_nine_withdraws():
    out = run(reminder(), K("1", "9", "9"))
    assert Withdraw("withdrawn") in out and flat(out)[-2:] == ["OPT_CONFIRM", "OPT_DONE"]
    assert out[-1] == End("withdrawn")


def test_reminder_nine_then_one_keeps_the_reminders():
    out = run(reminder(), K("1", "9", "1"))
    assert Withdraw("withdrawn") not in out and out[-1] == End("completed")


def test_reminder_nine_then_silence_does_not_withdraw_and_does_not_play_a_giveup():
    out = run(reminder(), [Key("1"), Key("9"), NoInput(), NoInput(), NoInput()])
    assert Withdraw("withdrawn") not in out and out[-1] == End("completed")
    assert not any(i.startswith("SYS_GIVEUP") for i in flat(out))


# ------------------------------------------------------------- morning after
def morning(next_dose="auto", **kw) -> MorningAfterFlow:
    nxt = DoseInfo(4, DAY0 + timedelta(days=28)) if next_dose == "auto" else next_dose
    args = {
        "policy": POLICY,
        "lang": "hi",
        "clinic_id": "cln_a",
        "dose_no": 3,
        "total": 4,
        "due": DAY0 + timedelta(days=7),
        "next_dose": nxt,
        "visit_time": "0900",
    }
    return MorningAfterFlow(**{**args, **kw})


def test_morning_after_says_nothing_about_a_dose_until_the_listener_presses_1():
    f = morning()
    step = f.step(Start())
    assert [a.ids for a in step if isinstance(a, Play)] == [["RM_GATE"]]
    assert set(next(a for a in step if isinstance(a, Collect)).valid_keys) == {"1", "8"}
    out = run(morning(), [NoInput()] * 3)
    assert flat(out) == ["RM_GATE", "SYS_NOINPUT", "RM_GATE", "SYS_NOINPUT", "RM_GATE", "SYS_BYE"]
    assert "RM_MISS_Q" not in flat(out) and not any(isinstance(a, MarkTaken) for a in out)


def test_morning_after_took_it_notes_the_patients_word_and_names_the_next_dose():
    out = run(morning(), K("1", "1"))
    assert flat(out) == ["RM_GATE", "RM_HELLO", "RM_MISS_Q", "RM_MISS_TAKEN"]
    assert saves(out) == [("dose_taken", "yes")]
    assert MarkTaken(3) in out
    assert out.index(MarkTaken(3)) < out.index(
        next(a for a in out if isinstance(a, Play) and "RM_MISS_TAKEN" in a.ids)
    )
    assert slots_of(out, "RM_MISS_TAKEN") == {
        "n": 4, "total": 4, "date": DAY0 + timedelta(days=28), "time": "0900"
    }  # fmt: skip
    assert slots_of(out, "RM_MISS_Q") == {"n": 3, "total": 4, "date": DAY0 + timedelta(days=7)}


def test_morning_after_on_a_shared_phone_names_the_register_entry():
    out = run(morning(register="77"), K("1", "2"))
    assert flat(out)[:4] == ["RM_GATE", "RM_HELLO", "RM_WHO", "RM_MISS_Q"]


def test_morning_after_took_the_last_dose():
    out = run(morning(next_dose=None), K("1", "1"))
    assert flat(out)[-1] == "RM_MISS_TAKEN_LAST" and MarkTaken(3) in out


def test_morning_after_did_not_take_it_says_to_go_to_the_clinic_and_marks_nothing():
    out = run(morning(), K("1", "2"))
    assert flat(out) == ["RM_GATE", "RM_HELLO", "RM_MISS_Q", "RM_MISS_NOT"]
    assert saves(out) == [("dose_taken", "no")]
    assert not any(isinstance(a, MarkTaken) for a in out)
    assert slots_of(out, "RM_MISS_NOT") == {"clinic": "cln_a"}


def test_morning_after_silence_uses_its_own_giveup_prompt_and_marks_nothing():
    out = run(morning(), [Key("1"), NoInput(), NoInput(), NoInput()])
    assert plays(out) == [
        ["RM_GATE"],
        ["RM_HELLO", "RM_MISS_Q"],
        ["SYS_NOINPUT", "RM_MISS_Q"],
        ["SYS_NOINPUT", "RM_MISS_Q"],
        ["SYS_GIVEUP_MISS"],
    ]
    assert out[-1] == End("gave_up") and saves(out) == []


def test_morning_after_wrong_key_counts_as_a_try():
    out = run(morning(), K("1", "5", "5", "5"))
    assert flat(out)[-1] == "SYS_GIVEUP_MISS" and out[-1] == End("gave_up")


def test_morning_after_eight_at_the_gate_and_nine_after_it():
    assert Withdraw("wrong_number") in run(morning(), K("8", "8"))
    assert Withdraw("withdrawn") in run(morning(), K("1", "9", "9"))
    kept = run(morning(), K("1", "9", "1", "2"))
    assert flat(kept)[-1] == "RM_MISS_NOT" and Withdraw("withdrawn") not in kept


# -------------------------------------------------------------------- notices
def test_the_repeat_call_notice_is_one_message_and_offers_9_to_stop():
    f = NoticeFlow(policy=POLICY, lang="ur")
    step = f.step(Start())
    assert [a.ids for a in step if isinstance(a, Play)] == [["ENR_DUP"]]
    assert next(a for a in step if isinstance(a, Collect)).valid_keys == "9"
    quiet = run(NoticeFlow(policy=POLICY, lang="ur"), [NoInput()])
    assert flat(quiet) == ["ENR_DUP"] and quiet[-1] == End("completed")
    assert Withdraw("withdrawn") not in quiet and f.essential == {"ENR_DUP"}


def test_the_notice_stops_the_calls_only_after_a_second_nine():
    stop = run(NoticeFlow(policy=POLICY, lang="hi"), K("9", "9"))
    assert Withdraw("withdrawn") in stop and flat(stop) == ["ENR_DUP", "OPT_CONFIRM", "OPT_DONE"]
    assert stop[-1] == End("withdrawn")
    kept = run(NoticeFlow(policy=POLICY, lang="hi"), K("9", "1"))
    assert Withdraw("withdrawn") not in kept and kept[-1] == End("completed")
    other = run(NoticeFlow(policy=POLICY, lang="hi"), K("5"))
    assert other[-1] == End("completed") and flat(other) == ["ENR_DUP"]


# ---------------------------------------------------------- guardian and under 18
def test_a_guardian_is_asked_to_agree_and_told_who_answers_the_questions():
    out = run(first(guardian=True), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    assert flat(out)[:6] == [
        "FC_HELLO",
        "FC_NOTICE",
        "FC_CONSENT_Q_G",
        "FC_GUARD_OK",
        "FC_QCONSENT",
        "FC_PLACE",
    ]
    assert "FC_CONSENT_Q" not in flat(out)
    assert ("consent_reminders", "yes") in saves(out)


def test_a_guardian_who_says_no_hears_only_the_goodbye():
    out = run(first(guardian=True), K("2"))
    assert flat(out) == ["FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q_G", "FC_CONSENT_NO"]
    assert Withdraw("declined") in out and "FC_GUARD_OK" not in flat(out)


def test_an_adult_never_hears_the_guardian_lines_and_neither_does_a_later_call_of_a_guardian_course():
    assert not {"FC_CONSENT_Q_G", "FC_GUARD_OK"} & set(
        flat(run(first(), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")]))
    )
    prior = {"consent_reminders": "yes", "consent_questions": "no"}
    assert flat(run(first(guardian=True, prior=prior), [])) == ["FC_HELLO", "FC_CLOSE"]


def test_unconfirmed_eligibility_has_no_guardian_line_even_for_a_guardian_flag():
    out = run(first(guardian=True, ask_questions=False), K("1"))
    assert "FC_GUARD_OK" not in flat(out)


# -------------------------------------------------------------- the layered notice
def consent_keys(out) -> list[str]:
    """The keys each consent-question Collect accepted, in order."""
    return [a.valid_keys for a in out if isinstance(a, Collect)]


def test_the_short_notice_holds_a_zero_pressed_over_it_and_the_consent_question_accepts_zero():
    f = first(ask_questions=False)
    step = f.step(Start())
    notice = next(a for a in step if isinstance(a, Play) and a.ids == ["FC_NOTICE"])
    assert notice.keep == "0" and not notice.listen
    assert next(a for a in step if isinstance(a, Collect)).valid_keys == "1209"  # 1, 2, 0 and 9 to stop


def test_zero_plays_the_full_notice_and_then_asks_for_consent_again():
    out = run(first(ask_questions=False), K("0", "1"))
    assert flat(out) == [
        "FC_HELLO",
        "FC_NOTICE",
        "FC_CONSENT_Q",
        "FC_NOTICE_FULL",
        "FC_CONSENT_Q",
        "FC_CLOSE",
    ]
    assert saves(out) == [("consent_reminders", "yes")]  # one consent, whichever notice was heard
    full = next(a for a in out if isinstance(a, Play) and a.ids == ["FC_NOTICE_FULL"])
    assert not full.listen and full.keep == ""


def test_zero_works_twice_and_a_third_zero_is_a_wrong_key():
    out = run(first(ask_questions=False), K("0", "0", "0", "1"))
    assert flat(out).count("FC_NOTICE_FULL") == 2
    assert consent_keys(out) == ["1209", "1209", "129", "129"]  # after two, 0 is no longer a key
    assert flat(out)[-3:] == ["SYS_WRONGKEY", "FC_CONSENT_Q", "FC_CLOSE"]
    assert saves(out) == [("consent_reminders", "yes")]


def test_a_third_zero_uses_up_one_of_the_tries_a_question_gets():
    out = run(first(ask_questions=False), K("0", "0", "0", "0", "0"))
    assert flat(out).count("FC_NOTICE_FULL") == 2
    assert out[-1] == End("gave_up") and flat(out)[-1] == "SYS_GIVEUP_FIRST"
    assert saves(out) == []  # three wrong keys are no consent


def test_zero_then_no_declines_and_zero_then_nine_asks_to_stop():
    declined = run(first(), K("0", "2"))
    assert Withdraw("declined") in declined and flat(declined)[-1] == "FC_CONSENT_NO"
    stopped = run(first(), K("0", "9", "9"))
    assert flat(stopped)[-4:] == ["FC_NOTICE_FULL", "FC_CONSENT_Q", "OPT_CONFIRM", "OPT_DONE"]
    assert Withdraw("withdrawn") in stopped and saves(stopped) == []


def test_a_guardian_hears_the_guardian_question_again_after_the_full_notice():
    out = run(first(guardian=True, ask_questions=False), K("0", "1"))
    assert flat(out) == [
        "FC_HELLO", "FC_NOTICE", "FC_CONSENT_Q_G", "FC_NOTICE_FULL", "FC_CONSENT_Q_G", "FC_CLOSE",
    ]  # fmt: skip


def test_a_consent_already_given_hears_neither_notice():
    prior = {"consent_reminders": "yes", "consent_questions": "no"}
    assert flat(run(first(prior=prior), [])) == ["FC_HELLO", "FC_CLOSE"]


def test_the_policy_sets_how_many_times_the_full_notice_may_be_asked_for():
    out = run(first(ask_questions=False, policy=fast_policy(full_notice_max_plays=1)), K("0", "0", "1"))
    assert flat(out).count("FC_NOTICE_FULL") == 1 and consent_keys(out) == ["1209", "129", "129"]
    assert POLICY["calls"]["full_notice_max_plays"] == 2


# ---------------------------------------------------------------- the contract
def test_every_prompt_a_flow_can_play_is_in_required_prompts_and_none_is_left_over():
    run(first(ask_language=True), [Key("1"), *K("1", "1"), HEARD, *K("1", "1", "1", "1")])
    run(first(), K("0", "2"))
    run(first(), K("9", "9"))
    run(first(), [NoInput(), NoInput(), NoInput()])
    run(first(), [*K("1", "1"), SILENT, SILENT, *K("7", "1", "1", "1", "2")])
    run(first(next_dose=None), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    run(first(today=DAY0 + timedelta(days=5)), [*K("1", "1"), HEARD, *K("1", "1", "1", "2")])
    run(first(guardian=True), [*K("1", "1"), HEARD, *K("1", "1", "1", "1")])
    run(first(guardian=True), K("2"))
    run(reminder(vaccine_out=True, clinic_phone="1", team_ward=3, register="12"), K("1", "*", "9", "9"))
    run(reminder(), K("8", "8"))
    run(reminder(), [NoInput()] * 3)
    run(morning(register="5"), K("1", "1"))
    run(morning(next_dose=None), K("1", "1"))
    run(morning(), K("1", "2"))
    run(morning(), [Key("1"), NoInput(), NoInput(), NoInput()])
    run(NoticeFlow(policy=POLICY, lang="hi"), K("9", "9"))
    assert SEEN == fl.REQUIRED_PROMPTS
