"""In-place edits: idempotency, error handling, media, refresh job start/stop."""
from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto  # noqa: E402
from telegram.error import BadRequest, RetryAfter  # noqa: E402

from sherwood_monitor.telegram.cards_live import CardGone, CardRef, LiveCards, ProposalCards  # noqa: E402
from tests.tg_fakes import CHAT, NOW, Clock, FakeGateway, FakeJobQueue, fake_bot, make_proposal  # noqa: E402

KB = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="shfund:back:1")]])


async def test_edit_skips_when_unchanged():
    bot = fake_bot()
    live = LiveCards(bot)
    ref = CardRef(CHAT, 7)
    assert await live.edit(ref, "a", KB) is True
    assert await live.edit(ref, "a", KB) is False
    assert bot.edit_message_text.await_count == 1
    assert await live.edit(ref, "b", KB) is True
    assert bot.edit_message_text.await_count == 2
    other = InlineKeyboardMarkup([[InlineKeyboardButton("y", callback_data="shfund:back:1")]])
    assert await live.edit(ref, "b", other) is True  # keyboard change counts


async def test_post_then_identical_edit_is_skipped():
    bot = fake_bot()
    live = LiveCards(bot)
    ref = await live.post(CHAT, "card", KB)
    assert await live.edit(ref, "card", KB) is False
    bot.edit_message_text.assert_not_awaited()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML" and kwargs["reply_markup"] is KB


async def test_not_modified_is_success_and_remembered():
    bot = fake_bot()
    bot.edit_message_text.side_effect = BadRequest("Message is not modified: specified new message content ...")
    live = LiveCards(bot)
    ref = CardRef(CHAT, 7)
    assert await live.edit(ref, "a", KB) is False
    assert await live.edit(ref, "a", KB) is False
    assert bot.edit_message_text.await_count == 1


async def test_gone_message_raises_card_gone():
    bot = fake_bot()
    bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    with pytest.raises(CardGone):
        await LiveCards(bot).edit(CardRef(CHAT, 7), "a", KB)


async def test_rate_limit_is_retried_next_tick():
    bot = fake_bot()
    bot.edit_message_text.side_effect = [RetryAfter(5), True]
    live = LiveCards(bot)
    ref = CardRef(CHAT, 7)
    assert await live.edit(ref, "a", KB) is False
    assert await live.edit(ref, "a", KB) is True


CLOSED_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🗳 Voting closed", api_kwargs={"disabled": {}})],
    [InlineKeyboardButton("📊 View fund", url="https://app.sherwood.sh/tg/fund/0x1")],
])


async def test_refused_disabled_buttons_are_dropped_not_left_live():
    """A server without Bot API 10.3 refuses the disabled button: the card is edited again without it
    (the closed card as before 10.3), and that identical render is not retried."""
    bot = fake_bot()
    bot.edit_message_text.side_effect = [BadRequest("Bad Request: can't parse inline keyboard button"), True]
    live = LiveCards(bot)
    ref = CardRef(CHAT, 7)
    assert await live.edit(ref, "closed", CLOSED_KB) is True
    first, second = (c.kwargs["reply_markup"] for c in bot.edit_message_text.await_args_list)
    assert first is CLOSED_KB
    assert second.to_dict() == {"inline_keyboard": [[{"text": "📊 View fund", "url": "https://app.sherwood.sh/tg/fund/0x1"}]]}
    assert await live.edit(ref, "closed", CLOSED_KB) is False
    assert bot.edit_message_text.await_count == 2


async def test_other_edit_errors_do_not_retry_and_posts_degrade_too():
    bot = fake_bot()
    bot.edit_message_text.side_effect = BadRequest("Bad Request: can't parse entities")
    assert await LiveCards(bot).edit(CardRef(CHAT, 7), "a", KB) is False  # nothing disabled: no second try
    assert bot.edit_message_text.await_count == 1
    bot = fake_bot()

    async def refuse_first(**kwargs):
        if bot.send_message.await_count == 1:
            raise BadRequest("Bad Request: can't parse inline keyboard button")
        return SimpleNamespace(message_id=600)

    bot.send_message.side_effect = refuse_first
    ref = await LiveCards(bot).post(CHAT, "closed", CLOSED_KB)
    assert ref.message_id == 600 and bot.send_message.await_count == 2
    assert bot.send_message.await_args.kwargs["reply_markup"].to_dict()["inline_keyboard"] == [
        [{"text": "📊 View fund", "url": "https://app.sherwood.sh/tg/fund/0x1"}]
    ]


async def test_media_cards_edit_caption_or_media():
    bot = fake_bot()
    live = LiveCards(bot)
    ref = await live.post(CHAT, "cap", KB, image_url="https://img/1.png")
    assert bot.send_photo.await_args.kwargs["caption"] == "cap"
    assert await live.edit(ref, "cap2", KB) is True
    bot.edit_message_caption.assert_awaited_once()
    bot.edit_message_media.assert_not_awaited()
    assert await live.edit(ref, "cap3", KB, image_url="https://img/2.png") is True
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert isinstance(media, InputMediaPhoto) and media.caption == "cap3" and media.media == "https://img/2.png"


async def test_job_queue_refresh_start_stop():
    jq = FakeJobQueue()
    live = LiveCards(fake_bot(), job_queue=jq, interval_s=60)
    ticks = iter([True, False])

    async def tick():
        return next(ticks)

    live.start_refresh("k", tick)
    live.start_refresh("k", tick)  # idempotent
    assert len(jq.jobs) == 1 and jq.jobs[0].interval == 60 and jq.jobs[0].name == "shfund:refresh:k"
    await jq.fire(jq.jobs[0])
    assert live.is_refreshing("k") and not jq.jobs[0].removed
    await jq.fire(jq.jobs[0])
    assert not live.is_refreshing("k") and jq.jobs[0].removed


async def test_asyncio_fallback_without_job_queue():
    live = LiveCards(fake_bot(), job_queue=None, interval_s=0.01)
    calls = []

    async def tick():
        calls.append(1)
        return len(calls) < 3

    live.start_refresh("k", tick)
    assert live.is_refreshing("k")
    for _ in range(100):
        if not live.is_refreshing("k"):
            break
        await asyncio.sleep(0.01)
    assert len(calls) == 3 and not live.is_refreshing("k")


async def test_asyncio_fallback_stop_cancels():
    live = LiveCards(fake_bot(), job_queue=None, interval_s=10)

    async def tick():
        return True

    live.start_refresh("k", tick)
    task = live._tasks["k"]
    live.stop_refresh("k")
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert not live.is_refreshing("k")


async def test_tick_errors_keep_job_card_gone_stops():
    jq = FakeJobQueue()
    live = LiveCards(fake_bot(), job_queue=jq)
    seq = iter([RuntimeError("flaky"), CardGone("gone")])

    async def tick():
        raise next(seq)

    live.start_refresh("k", tick)
    await jq.fire(jq.jobs[0])
    assert live.is_refreshing("k")
    await jq.fire(jq.jobs[0])
    assert not live.is_refreshing("k")


async def test_proposal_cards_post_watch_and_final_edit():
    gw = FakeGateway()
    clock = Clock()
    jq = FakeJobQueue()
    bot = fake_bot()
    live = LiveCards(bot, job_queue=jq, clock=clock)
    cards = ProposalCards(live, gw, clock=clock)

    ref = await cards.post(CHAT, "42")
    assert gw.card_ids["42"] == ref.message_id
    assert "ends in 30m" in bot.send_message.await_args.kwargs["text"]
    assert len(jq.jobs) == 1

    clock.t = NOW + 600
    await jq.fire(jq.jobs[0])
    assert "ends in 20m" in bot.edit_message_text.await_args.kwargs["text"]
    assert not jq.jobs[0].removed

    gw.proposals["42"] = dataclasses.replace(gw.proposals["42"], against_bps=1000)
    clock.t = NOW + 1800
    await jq.fire(jq.jobs[0])
    final = bot.edit_message_text.await_args.kwargs["text"]
    assert "Against 10.0% ▮▮▮▮░░░░ 20% · voting closed" in final
    assert jq.jobs[0].removed and not live.is_refreshing(cards.key(CHAT, "42"))


async def test_proposal_cards_do_not_watch_closed_proposals():
    gw = FakeGateway(proposals=[make_proposal(status="passed")])
    jq = FakeJobQueue()
    cards = ProposalCards(LiveCards(fake_bot(), job_queue=jq), gw)
    await cards.post(CHAT, "42")
    assert jq.jobs == []


async def test_refresh_uses_stored_card_id_after_restart():
    gw = FakeGateway(proposals=[make_proposal(card_message_id=321)])
    bot = fake_bot()
    cards = ProposalCards(LiveCards(bot), gw, clock=Clock())
    assert await cards.refresh(CHAT, "42") is True
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 321
