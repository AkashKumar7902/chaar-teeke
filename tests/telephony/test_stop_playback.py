"""Cutting a prompt short (barge-in) and naming the line a missed call came in on, against the fake ARI."""

import asyncio

import pytest
from conftest import close_rig, make_rig

from chaarteeke.telephony import CallAnswered, CommandRejected, MissedCall, PlaybackFinished


def scenario(coro):
    async def wrapper():
        rig = await make_rig()
        try:
            await coro(rig)
        finally:
            await close_rig(rig)

    asyncio.run(wrapper())


def test_stopping_a_playback_deletes_it_on_the_pbx_and_the_playback_is_reported_finished():
    async def go(rig):
        cid = await rig.adapter.originate("+919876543210", "c1")
        await rig.fake.answer(cid)
        assert isinstance(await rig.next(), CallAnswered)
        await rig.adapter.play(cid, ["chaar/hi/FC_NOTICE"], "pb-1")
        await rig.adapter.stop_playback(cid, "pb-1")
        assert ("DELETE", "/ari/playbacks/pb-1") in [(m, p) for m, p, _ in rig.fake.requests]
        assert rig.fake.stopped_playbacks == ["pb-1"]
        done = await rig.next()
        assert isinstance(done, PlaybackFinished) and done.playback_id == "pb-1"

    scenario(go)


def test_stopping_a_playback_that_already_ended_is_a_404_the_caller_can_tell_apart():
    async def go(rig):
        cid = await rig.adapter.originate("+919876543210", "c1")
        await rig.fake.answer(cid)
        await rig.next()
        await rig.adapter.play(cid, ["chaar/hi/FC_NOTICE"], "pb-1")
        await rig.fake.finish_playback(cid)
        await rig.next()
        with pytest.raises(CommandRejected) as e:
            await rig.adapter.stop_playback(cid, "pb-1")
        assert e.value.status == 404

    scenario(go)


def test_a_pbx_that_refuses_the_stop_is_not_hidden():
    async def go(rig):
        cid = await rig.adapter.originate("+919876543210", "c1")
        await rig.fake.answer(cid)
        await rig.next()
        await rig.adapter.play(cid, ["chaar/hi/FC_NOTICE"], "pb-1")
        rig.fake.fail_next["stop_playback"] = 500
        with pytest.raises(CommandRejected) as e:
            await rig.adapter.stop_playback(cid, "pb-1")
        assert e.value.status == 500

    scenario(go)


def test_a_missed_call_says_which_modem_it_came_in_on():
    async def go(rig):
        await rig.fake.incoming_call("+919876543210")
        ev = await rig.next()
        assert isinstance(ev, MissedCall) and ev.line == "quectel0" and ev.clinic_id is None

    scenario(go)
