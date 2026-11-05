"""Pitch -> riff -> DraftRequested (pure, no PTB)."""
from __future__ import annotations

import pytest

from sherwood_monitor.telegram.flow import IncomingMessage, PitchFlow, ReactionChange

CHAT = -1001
HOST = 1
ALICE, BOB, CAROL, DAVE = 10, 11, 12, 13


class Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_flow(**kw):
    events = []
    clock = Clock()
    flow = PitchFlow(host_for_chat=lambda chat: HOST, on_draft_requested=events.append, clock=clock, **kw)
    return flow, events, clock


def msg(mid, user, text, **kw):
    return IncomingMessage(chat_id=CHAT, message_id=mid, user_id=user, text=text, username=f"u{user}", **kw)


def fire(mid, user, *, on=True):
    return ReactionChange(chat_id=CHAT, message_id=mid, user_id=user, old=() if on else ("🔥",), new=("🔥",) if on else ())


PITCH = "@sherwoodagentbot long $NVDA into earnings, AI capex keeps going"


async def test_pitch_needs_mention_and_trade_idea():
    flow, _, _ = make_flow()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    await flow.on_message(msg(101, BOB, "long $AMD too"))  # no mention
    await flow.on_message(msg(102, BOB, "@sherwoodagentbot how are you", mentions_bot=True))  # no idea
    assert [p.message_id for p in flow.open_pitches(CHAT)] == [100]


async def test_three_fire_reactions_on_pitch_emit_once():
    flow, events, _ = make_flow()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    assert await flow.on_reaction(fire(100, BOB)) is None
    assert await flow.on_reaction(fire(100, CAROL)) is None
    ev = await flow.on_reaction(fire(100, DAVE))
    assert ev is not None
    assert (ev.idea_by, ev.text, ev.source_message_id) == (ALICE, PITCH, 100)
    assert ev.chat_id == CHAT and ev.trigger == "fire" and ev.fire_count == 3
    assert await flow.on_reaction(fire(100, HOST)) is None  # already drafted
    assert len(events) == 1


async def test_unreact_and_duplicate_and_bots_do_not_count():
    flow, events, _ = make_flow()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    await flow.on_reaction(fire(100, BOB))
    await flow.on_reaction(fire(100, BOB))  # same user again (old had no 🔥 in a malformed update)
    await flow.on_reaction(fire(100, CAROL))
    await flow.on_reaction(fire(100, CAROL, on=False))  # took it back
    await flow.on_reaction(ReactionChange(CHAT, 100, 99, (), ("🔥",), user_is_bot=True))
    await flow.on_reaction(ReactionChange(CHAT, 100, DAVE, (), ("👍",)))  # not fire
    assert events == []
    await flow.on_reaction(fire(100, CAROL))
    ev = await flow.on_reaction(fire(100, DAVE))
    assert ev is not None and ev.fire_count == 3


async def test_reactions_on_unseen_riff_count_with_pitch():
    flow, events, _ = make_flow()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    await flow.on_message(msg(101, BOB, "nice"))  # a human message we saw
    # 102 never arrived as an update: the bot's own riff
    await flow.on_reaction(fire(101, CAROL))  # 🔥 on Bob's chatter does not count
    await flow.on_reaction(fire(102, CAROL))
    await flow.on_reaction(fire(102, DAVE))
    assert events == []
    ev = await flow.on_reaction(fire(100, BOB))  # union of pitch + riff reactors
    assert ev is not None and ev.source_message_id == 100
    assert 102 in flow.open_pitches(CHAT)[0].riff_ids


async def test_fire_replies_to_riff_without_admin():
    """Privacy off but not admin: no reactions, 🔥 replies to the bot's riff count."""
    flow, events, _ = make_flow()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    for mid, user in ((103, BOB), (104, CAROL)):
        assert await flow.on_message(msg(mid, user, "🔥", reply_to_message_id=102, reply_to_is_bot=True)) is None
    ev = await flow.on_message(msg(105, DAVE, "🔥🔥", reply_to_message_id=100))
    assert ev is not None and ev.trigger == "fire" and ev.idea_by == ALICE


async def test_fire_reply_to_known_card_is_ignored():
    flow, events, _ = make_flow()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    flow.note_bot_card(CHAT, 101)  # our fund card, not a riff
    for mid, user in ((102, BOB), (103, CAROL), (104, DAVE)):
        await flow.on_message(msg(mid, user, "🔥", reply_to_message_id=101, reply_to_is_bot=True))
    assert events == []


async def test_ship_it_by_host_only():
    flow, events, _ = make_flow()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    assert await flow.on_message(msg(101, BOB, "ship it", reply_to_message_id=100)) is None
    ev = await flow.on_message(msg(102, HOST, "Ship it! 🚀", reply_to_message_id=100))
    assert ev is not None and ev.trigger == "ship_it" and ev.triggered_by == HOST
    assert events == [ev]


async def test_ship_it_without_reply_targets_latest_open_pitch():
    flow, events, _ = make_flow()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    await flow.on_message(msg(110, BOB, "@sherwoodagentbot short $TSLA, margins", mentions_bot=True))
    ev = await flow.on_message(msg(111, HOST, "@sherwoodagentbot ship it", mentions_bot=True))
    assert ev is not None and ev.source_message_id == 110 and ev.idea_by == BOB


async def test_async_host_lookup_and_callback_failure_allows_retry():
    calls = []

    async def host(chat):
        return HOST

    async def boom(event):
        calls.append(event)
        if len(calls) == 1:
            raise RuntimeError("store down")

    flow = PitchFlow(host_for_chat=host, on_draft_requested=boom, clock=Clock())
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    assert await flow.on_message(msg(101, HOST, "ship it", reply_to_message_id=100)) is None
    assert await flow.on_message(msg(102, HOST, "ship it", reply_to_message_id=100)) is not None
    assert len(calls) == 2


async def test_pitches_expire():
    flow, events, clock = make_flow(pitch_ttl_s=60)
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    clock.t += 61
    assert flow.open_pitches(CHAT) == []
    for user in (BOB, CAROL, DAVE):
        await flow.on_reaction(fire(100, user))
    assert events == []


async def test_pre_llm_call_hook_injects_pitch_context_once():
    flow, _, clock = make_flow()
    hook = flow.make_pre_llm_call_hook()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    assert hook(platform="discord", user_message=PITCH) is None
    out = hook(platform="telegram", user_message=f"[alice|10] {PITCH.replace('@sherwoodagentbot ', '')}")
    assert out is not None and "<sherwood-fund-pitch>" in out["context"]
    assert "@u10" in out["context"] and "$NVDA" in out["context"]
    assert hook(platform="telegram", user_message=PITCH) is None  # consumed


async def test_context_hook_ignores_unrelated_turns_and_expires():
    flow, _, clock = make_flow()
    hook = flow.make_pre_llm_call_hook()
    await flow.on_message(msg(100, ALICE, PITCH, mentions_bot=True))
    await flow.on_message(msg(101, BOB, "@sherwoodagentbot short $TSLA on margins", mentions_bot=True))
    assert hook(platform="telegram", user_message="what's the weather") is None  # 2 pending, no match
    clock.t += 601
    assert hook(platform="telegram", user_message=PITCH) is None


@pytest.mark.parametrize(
    "text,expected",
    [("long $NVDA", True), ("thinking we short ETH here", True), ("gm", False), ("what is NAV?", False)],
)
def test_trade_idea_heuristic(text, expected):
    assert PitchFlow.is_trade_idea(text) is expected
