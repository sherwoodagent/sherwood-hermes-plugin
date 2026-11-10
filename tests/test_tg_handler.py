"""Handler wiring, callback routing, host checks, personal links, reactions."""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram")

from telegram import Chat, Message, MessageEntity, User  # noqa: E402
from telegram.error import BadRequest, Forbidden  # noqa: E402
from telegram.ext import (  # noqa: E402
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from sherwood_monitor.telegram.handler import (  # noqa: E402
    COMMAND_GROUP,
    OBSERVER_GROUP,
    FundHandlers,
    incoming_from_message,
    wire,
)
from sherwood_monitor.telegram.links import sign_link, vote_link_message  # noqa: E402
from sherwood_monitor.telegram.runtime import TelegramRuntime  # noqa: E402
from sherwood_monitor.telegram.settings import TelegramSettings  # noqa: E402
from tests.tg_fakes import (  # noqa: E402
    ALICE,
    BOB,
    CAROL,
    CHAT,
    DAVE,
    GOV,
    HOST,
    VAULT,
    Clock,
    FakeGateway,
    FakeJobQueue,
    fake_bot,
    make_proposal,
    refusal_text,
)

SECRET = "sherwood-test-vector-secret"
BOT_ID = 999


def make_runtime(gw=None, **settings_kw):
    gw = gw or FakeGateway()
    settings = TelegramSettings(bot_username="sherwoodagentbot", **settings_kw)
    rt = TelegramRuntime(gw, settings, clock=Clock(), secret=lambda: SECRET)
    bot = fake_bot(BOT_ID)
    rt.attach(SimpleNamespace(bot=bot, job_queue=FakeJobQueue()))
    rt.sender_check = lambda chat_id, user_id: True  # Hermes' allowlist admits the group (chat-scoped grant)
    return rt, gw, bot


def make_query(data, user_id, *, chat_id=CHAT, chat_type="supergroup", message_id=77, username=None):
    q = MagicMock()
    q.id = "q1"
    q.data = data
    q.from_user = SimpleNamespace(id=user_id, username=username or f"u{user_id}", first_name="F", last_name=None, is_bot=False)
    q.message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type), message_id=message_id,
        text_html="🙋 @eve wants to join", is_topic_message=False,
    )
    q.answer = AsyncMock(return_value=True)
    q.edit_message_reply_markup = AsyncMock(return_value=True)
    q.edit_message_text = AsyncMock(return_value=True)
    return q


async def tap(rt, bot, data, user_id, **kw):
    q = make_query(data, user_id, **kw)
    await FundHandlers(rt).on_callback(SimpleNamespace(callback_query=q), SimpleNamespace(bot=bot))
    return q


def answered_text(q):
    call = q.answer.await_args
    return call.kwargs.get("text") if call else None


def url_of(markup):
    return markup.to_dict()["inline_keyboard"][0][0]["url"]


# -- wiring -------------------------------------------------------------------


def test_wire_registers_scoped_handlers_in_the_right_groups():
    app = ApplicationBuilder().token("123:TEST").build()
    rt = TelegramRuntime(FakeGateway(), TelegramSettings(), secret=lambda: SECRET)
    wire(app, rt)
    # Hermes core registers its catch-alls after plugin factories run
    core_cb = CallbackQueryHandler(AsyncMock())
    core_cmd = MessageHandler(filters.COMMAND, AsyncMock())
    app.add_handler(core_cmd)
    app.add_handler(core_cb)

    group0 = app.handlers[COMMAND_GROUP]
    ours_cb = next(h for h in group0 if isinstance(h, CallbackQueryHandler))
    assert ours_cb.pattern.pattern == "^shfund:"
    assert group0.index(ours_cb) < group0.index(core_cb)
    commands = {c for h in group0 if isinstance(h, CommandHandler) for c in h.commands}
    # No /join_fund or /join-fund here: /join-fund is the core command for guest
    # AGENTS; members join from the fund card's Join button.
    assert commands == {"fund", "leaderboard", "start"}
    assert not any(isinstance(h, MessageHandler) for h in group0 if h is not core_cmd)
    assert all(group0.index(h) < group0.index(core_cmd) for h in group0 if h not in (core_cb, core_cmd))

    observers = app.handlers[OBSERVER_GROUP]
    assert OBSERVER_GROUP < 0 < 99  # before core group 0, separate from the group-99 observer
    assert any(isinstance(h, MessageReactionHandler) for h in observers)
    assert any(isinstance(h, MessageHandler) for h in observers)
    assert not any(isinstance(h, (CallbackQueryHandler, CommandHandler)) for h in observers)
    assert rt.connected and rt.live.job_queue is app.job_queue


def test_callback_pattern_leaves_core_buttons_alone():
    app = ApplicationBuilder().token("123:TEST").build()
    wire(app, TelegramRuntime(FakeGateway(), TelegramSettings(), secret=lambda: SECRET))
    ours = next(h for h in app.handlers[COMMAND_GROUP] if isinstance(h, CallbackQueryHandler))
    assert ours.pattern.match("shfund:back:42")
    assert not ours.pattern.match("approve:exec:1") and not ours.pattern.match("model:pick")


def test_join_fund_is_left_to_the_core_command():
    """/join-fund (a guest agent asking to co-manage) must reach Hermes' core command."""
    from sherwood_monitor.telegram import handler as h

    assert not hasattr(h.FundHandlers, "cmd_join_fund") and not hasattr(h, "JOIN_FUND_HYPHEN_RE")


# -- callbacks ---------------------------------------------------------------------


async def test_back_toggles_social_backing_and_edits_card_in_place():
    rt, gw, bot = make_runtime()
    q = await tap(rt, bot, "shfund:back:42", BOB, username="bob")
    assert gw.proposals["42"].backers == (BOB,)
    assert answered_text(q).startswith("👍 You're backing this")
    edit = bot.edit_message_text.await_args.kwargs
    assert edit["message_id"] == 77 and edit["chat_id"] == CHAT
    assert "👍 @bob" in edit["text"]
    assert edit["reply_markup"].to_dict()["inline_keyboard"][0][0]["text"] == "👍 Back it · 1"
    q = await tap(rt, bot, "shfund:back:42", BOB, username="bob")
    assert gw.proposals["42"].backers == () and answered_text(q) == "Removed your 👍."


@pytest.mark.parametrize(
    "data", ["shfund:shipit:d7", "shfund:propose:d7", "shfund:approve_member:77",
             "shfund:approve_agent:nova", "shfund:remove_agent:atlas"],
)
async def test_host_only_callbacks_refuse_members(data):
    rt, gw, bot = make_runtime()
    q = await tap(rt, bot, data, BOB)
    # Bot API 10.4: an ephemeral reply to the callback (no receiver_user_id), seen only by the tapper.
    sent = bot.send_message.await_args.kwargs
    assert sent["text"] == "Only the fund host can do that." and sent["parse_mode"] is None
    assert sent["api_kwargs"] == {"ephemeral_message_parameters": {"callback_query_id": "q1"}}
    q.answer.assert_awaited_once()  # the query is still answered, with no pop-up
    assert answered_text(q) is None
    assert gw.calls == []
    assert bot.send_message.await_count == 1  # the refusal only: no link went out


async def test_refusal_falls_back_to_the_alert():
    """A server without 10.4 ephemeral replies, SHERWOOD_TG_PERSONAL_LINKS=dm, or a tap outside a group:
    today's pop-up alert."""
    rt, _, bot = make_runtime()
    bot.send_message.side_effect = BadRequest("Bad Request: receiver_user_id is required")
    q = await tap(rt, bot, "shfund:shipit:d7", BOB)
    assert answered_text(q) == "Only the fund host can do that." and q.answer.await_args.kwargs["show_alert"] is True
    q.answer.assert_awaited_once()
    rt, _, bot = make_runtime(personal_link_mode="dm")
    q = await tap(rt, bot, "shfund:shipit:d7", BOB)
    assert answered_text(q) == "Only the fund host can do that." and q.answer.await_args.kwargs["show_alert"] is True
    bot.send_message.assert_not_awaited()
    rt, _, bot = make_runtime()
    q = await tap(rt, bot, "shfund:shipit:d7", BOB, chat_type="private")
    assert q.answer.await_args.kwargs["show_alert"] is True
    bot.send_message.assert_not_awaited()


async def test_host_ship_it_calls_gateway_and_greys_out_the_button():
    rt, gw, bot = make_runtime()
    q = await tap(rt, bot, "shfund:shipit:d7", HOST)
    assert gw.calls == [("ship_draft", "d7", HOST)]
    assert answered_text(q) == "🚀 Proposal submitted."
    markup = q.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert markup.to_dict() == {"inline_keyboard": [[{"text": "🚀 Shipped", "disabled": {}}]]}


async def test_ship_it_keeps_the_co_sign_button_live():
    """Host co-sign: the toast says "Tap ✍️ Co-sign", so only 🚀 Ship it greys out; if Telegram refuses
    disabled buttons, the card keeps ✍️ Co-sign alone."""
    from sherwood_monitor.telegram.cards import render_draft_card

    rt, _, bot = make_runtime()
    _, card = render_draft_card(make_proposal(id="d7", status="draft"), requires_host_cosign=True)
    q = make_query("shfund:shipit:d7", HOST)
    q.message.reply_markup = card
    q.edit_message_reply_markup = AsyncMock(side_effect=[BadRequest("Bad Request: can't parse inline keyboard button"), True])
    await FundHandlers(rt).on_callback(SimpleNamespace(callback_query=q), SimpleNamespace(bot=bot))
    first, second = (c.kwargs["reply_markup"].to_dict()["inline_keyboard"] for c in q.edit_message_reply_markup.await_args_list)
    assert first == [[{"text": "🚀 Shipped", "disabled": {}}, {"text": "✍️ Co-sign (host)", "callback_data": "shfund:propose:d7"}]]
    assert second == [[{"text": "✍️ Co-sign (host)", "callback_data": "shfund:propose:d7"}]]


async def test_co_sign_after_the_proposal_is_on_chain_says_so():
    """✍️ Co-sign stays live after 🚀 Ship it; once the proposal is on chain the answer says that,
    not "not ready" (and a cancelled draft says it was cancelled)."""
    rt, gw, bot = make_runtime()
    gw.signing_url = None  # host_signing_url only signs a draft
    gw.proposals["d7"] = make_proposal(id="d7", status="draft")
    q = await tap(rt, bot, "shfund:propose:d7", HOST)
    assert refusal_text(q, bot) == "The signing page isn't ready for this draft yet."
    gw.proposals["d7"] = make_proposal(id="d7", status="voting", onchain_id=9)
    q = await tap(rt, bot, "shfund:propose:d7", HOST)
    assert refusal_text(q, bot) == "Proposal #9 is already on chain: there is nothing left to co-sign."
    gw.proposals["d7"] = make_proposal(id="d7", status="cancelled")
    q = await tap(rt, bot, "shfund:propose:d7", HOST)
    assert refusal_text(q, bot) == "This draft was cancelled: there is nothing to co-sign."


async def test_ship_it_failure_shows_reason_and_keeps_buttons():
    rt, gw, bot = make_runtime()
    gw.fail_ship = True
    q = await tap(rt, bot, "shfund:shipit:d7", HOST)
    assert refusal_text(q, bot) == "Bond swap still pending."
    q.edit_message_reply_markup.assert_not_awaited()


async def test_host_approve_member_stamps_the_card():
    rt, gw, bot = make_runtime()
    q = await tap(rt, bot, "shfund:approve_member:77", HOST, username="host")
    assert gw.calls == [("approve_member", 77, HOST)]
    kwargs = q.edit_message_text.await_args.kwargs
    assert kwargs["text"].endswith("<i>✅ Approved by @host</i>") and kwargs["reply_markup"] is None


async def test_no_host_configured_refuses_everyone():
    gw = FakeGateway(host=None)
    rt, gw, bot = make_runtime(gw)
    q = await tap(rt, bot, "shfund:shipit:d7", HOST)
    assert refusal_text(q, bot) == "Only the fund host can do that."


async def test_vote_sends_ephemeral_signed_link():
    rt, gw, bot = make_runtime()
    q = await tap(rt, bot, "shfund:vote:42", BOB)
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == CHAT
    assert kwargs["api_kwargs"] == {
        "ephemeral_message_parameters": {"receiver_user_id": BOB, "callback_query_id": "q1"}
    }
    url = url_of(kwargs["reply_markup"])
    ts = int(url.split("&ts=")[1].split("&")[0])  # build_link stamps wall-clock time
    sig = sign_link(SECRET, vote_link_message(GOV, "42", BOB, ts))
    assert url == f"https://app.sherwood.sh/tg/vote?proposal={GOV}:42&tg={BOB}&ts={ts}&sig={sig}"
    q.answer.assert_awaited()  # spinner stopped, no toast needed


async def test_vote_falls_back_to_dm_then_start_link():
    rt, gw, bot = make_runtime()
    calls = []

    async def send(**kw):
        calls.append(kw["chat_id"])
        if "api_kwargs" in kw:
            raise BadRequest("Bad Request: unknown parameter")
        return SimpleNamespace(message_id=1)

    bot.send_message = AsyncMock(side_effect=send)
    q = await tap(rt, bot, "shfund:vote:42", BOB)
    assert calls == [CHAT, BOB]
    assert "Sent your private link" in answered_text(q)

    async def send_forbidden(**kw):
        if "api_kwargs" in kw:
            raise BadRequest("nope")
        raise Forbidden("Forbidden: bot can't initiate conversation with a user")

    bot.send_message = AsyncMock(side_effect=send_forbidden)
    q = await tap(rt, bot, "shfund:vote:42", BOB)
    assert q.answer.await_args.kwargs == {"url": f"https://t.me/sherwoodagentbot?start=sfv_{VAULT[2:]}_42"}


async def test_long_proposal_id_never_falls_back_to_a_wrong_start_link():
    long_id = "1" * 25  # fits the vote URL, not a 64-char /start payload
    gw = FakeGateway(proposals=[make_proposal(id=long_id)])
    rt, gw, bot = make_runtime(gw)

    async def all_fail(**kw):
        if "api_kwargs" in kw:
            raise BadRequest("nope")
        raise Forbidden("Forbidden: bot can't initiate conversation with a user")

    bot.send_message = AsyncMock(side_effect=all_fail)
    q = await tap(rt, bot, f"shfund:vote:{long_id}", BOB)
    assert all("url" not in c.kwargs for c in q.answer.await_args_list)
    assert "press Start" in answered_text(q) and q.answer.await_args.kwargs["show_alert"]


async def test_dm_mode_skips_ephemeral():
    rt, gw, bot = make_runtime(personal_link_mode="dm")
    await tap(rt, bot, f"shfund:deposit:{VAULT}", BOB)
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == BOB and "api_kwargs" not in kwargs
    assert f"/tg/deposit?fund={VAULT}&tg={BOB}&" in url_of(kwargs["reply_markup"])


@pytest.mark.parametrize("kind", ["deposit", "withdraw", "join"])
async def test_fund_links_per_user(kind):
    rt, gw, bot = make_runtime()
    await tap(rt, bot, f"shfund:{kind}:{VAULT}", CAROL)
    url = url_of(bot.send_message.await_args.kwargs["reply_markup"])
    assert url.startswith(f"https://app.sherwood.sh/tg/{kind}?fund={VAULT}&tg={CAROL}&ts=")


async def test_link_guards():
    rt, gw, bot = make_runtime()
    q = await tap(rt, bot, "shfund:deposit:0x3333333333333333333333333333333333333333", BOB)
    assert answered_text(q) == "This button belongs to another fund."
    gw.proposals["42"] = make_proposal(status="passed")
    q = await tap(rt, bot, "shfund:vote:42", BOB)
    assert answered_text(q) == "Voting is closed for this proposal."
    rt._secret = lambda: None
    q = await tap(rt, bot, f"shfund:deposit:{VAULT}", BOB)
    assert "not configured" in refusal_text(q, bot)
    # The only message is that ephemeral refusal: no link went out.
    assert [c.kwargs["api_kwargs"] for c in bot.send_message.await_args_list] == [
        {"ephemeral_message_parameters": {"callback_query_id": "q1"}}
    ]


async def test_unlinked_or_disallowed_chat():
    rt, gw, bot = make_runtime()
    q = await tap(rt, bot, "shfund:back:42", BOB, chat_id=-555)
    assert answered_text(q) == "No Sherwood fund is linked to this chat."
    rt, gw, bot = make_runtime(allowed_chat_ids=frozenset({-1}))
    q = await tap(rt, bot, "shfund:back:42", BOB)
    assert answered_text(q) == "No Sherwood fund is linked to this chat."
    assert gw.proposals["42"].backers == ()


async def test_host_propose_delivers_signing_page():
    rt, gw, bot = make_runtime()
    await tap(rt, bot, "shfund:propose:d7", HOST)
    assert ("host_signing_url", "d7", HOST) in gw.calls
    assert url_of(bot.send_message.await_args.kwargs["reply_markup"]) == gw.signing_url


async def test_unknown_action():
    rt, gw, bot = make_runtime()
    q = await tap(rt, bot, "shfund:launch:1", BOB)
    assert answered_text(q) == "Unknown action."


# -- /start deep links ---------------------------------------------------------------


def start_update(text, user_id):
    msg = MagicMock()
    msg.text = text
    msg.reply_html = AsyncMock()
    msg.reply_text = AsyncMock()
    return SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=user_id)), msg


async def test_start_payload_sends_personal_link_in_dm():
    rt, gw, bot = make_runtime()
    upd, msg = start_update(f"/start sfd_{VAULT[2:]}", BOB)
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    markup = msg.reply_html.await_args.kwargs["reply_markup"]
    assert url_of(markup).startswith(f"https://app.sherwood.sh/tg/deposit?fund={VAULT}&tg={BOB}&ts=")

    upd, msg = start_update(f"/start sfv_{VAULT[2:]}_42", BOB)
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    assert f"/tg/vote?proposal={GOV}:42&tg={BOB}&" in url_of(msg.reply_html.await_args.kwargs["reply_markup"])


async def test_start_propose_is_host_only():
    rt, gw, bot = make_runtime()
    upd, msg = start_update(f"/start sfp_{VAULT[2:]}_d7", BOB)
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    msg.reply_text.assert_awaited_once_with("Only the fund host can co-sign.")
    upd, msg = start_update(f"/start sfp_{VAULT[2:]}_d7", HOST)
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    assert url_of(msg.reply_html.await_args.kwargs["reply_markup"]) == gw.signing_url


# -- commands -----------------------------------------------------------------------


def command_update(text="/fund", chat_id=CHAT):
    msg = MagicMock()
    msg.text = text
    msg.chat = SimpleNamespace(id=chat_id, type="supergroup")
    msg.reply_html = AsyncMock(return_value=SimpleNamespace(message_id=600))
    msg.reply_text = AsyncMock()
    return SimpleNamespace(effective_message=msg), msg


async def test_fund_command_posts_card_with_capability_notice():
    rt, gw, bot = make_runtime()
    upd, msg = command_update()
    await FundHandlers(rt).cmd_fund(upd, SimpleNamespace(bot=bot))
    text = msg.reply_html.await_args.args[0]
    assert text.startswith("🏦 <b>Robin Fund</b>")
    assert "Make me a group admin so 🔥 reactions count" in text  # privacy off, not admin
    assert 600 in rt.flow._cards[CHAT]

    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator"))
    rt._caps.clear()
    upd, msg = command_update()
    await FundHandlers(rt).cmd_fund(upd, SimpleNamespace(bot=bot))
    assert "group admin" not in msg.reply_html.await_args.args[0]


async def test_leaderboard_command():
    rt, gw, bot = make_runtime()
    upd, msg = command_update("/leaderboard")
    await FundHandlers(rt).cmd_leaderboard(upd, SimpleNamespace(bot=bot))
    assert "1. @alice <b>+12.4%</b> · 3 ideas" in msg.reply_html.await_args.args[0]


async def test_fund_command_in_unlinked_chat():
    rt, gw, bot = make_runtime()
    upd, msg = command_update(chat_id=-42)
    await FundHandlers(rt).cmd_fund(upd, SimpleNamespace(bot=bot))
    msg.reply_text.assert_awaited_once_with("No Sherwood fund is linked to this chat yet.")


# -- observers: pitch + reactions ------------------------------------------------------


def ptb_message(mid, user_id, text, *, entities=(), reply_to=None, username=None):
    return Message(
        message_id=mid,
        date=dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc),
        chat=Chat(id=CHAT, type="supergroup", title="testing sherwoodagentbot"),
        from_user=User(id=user_id, first_name="U", is_bot=False, username=username or f"u{user_id}"),
        text=text,
        entities=list(entities),
        reply_to_message=reply_to,
    )


def reaction_update(mid, user_id, emoji="🔥"):
    return SimpleNamespace(
        message_reaction=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT, type="supergroup"),
            message_id=mid,
            user=SimpleNamespace(id=user_id, username=f"u{user_id}", first_name="U", last_name=None, is_bot=False),
            old_reaction=(),
            new_reaction=(SimpleNamespace(emoji=emoji),),
        )
    )


async def test_pitch_then_three_fire_reactions_request_a_draft():
    rt, gw, bot = make_runtime()
    h = FundHandlers(rt)
    ctx = SimpleNamespace(bot=bot)
    pitch = ptb_message(
        100, ALICE, "@sherwoodagentbot long $NVDA into earnings",
        entities=[MessageEntity(MessageEntity.MENTION, 0, 17)], username="alice",
    )
    await h.on_group_message(SimpleNamespace(message=pitch), ctx)
    assert [p.message_id for p in rt.flow.open_pitches(CHAT)] == [100]
    for user in (BOB, CAROL):
        await h.on_reaction(reaction_update(100, user), ctx)
    assert gw.drafts == []
    await h.on_reaction(reaction_update(100, DAVE, "👍"), ctx)
    assert gw.drafts == []
    await h.on_reaction(reaction_update(100, DAVE), ctx)
    assert len(gw.drafts) == 1
    ev = gw.drafts[0]
    assert (ev.idea_by, ev.source_message_id, ev.chat_id) == (ALICE, 100, CHAT)
    assert ev.text == "@sherwoodagentbot long $NVDA into earnings"
    hook = rt.flow.make_pre_llm_call_hook()
    out = hook(platform="telegram", chat_id=str(CHAT), sender_id=str(ALICE), user_message="long $NVDA into earnings")
    assert "<sherwood-fund-pitch>" in out["context"]


async def test_host_ship_it_reply_to_riff_requests_a_draft():
    rt, gw, bot = make_runtime()
    h = FundHandlers(rt)
    ctx = SimpleNamespace(bot=bot)
    pitch = ptb_message(100, ALICE, "@sherwoodagentbot short $TSLA",
                        entities=[MessageEntity(MessageEntity.MENTION, 0, 17)])
    await h.on_group_message(SimpleNamespace(message=pitch), ctx)
    riff = Message(message_id=101, date=pitch.date, chat=pitch.chat,
                   from_user=User(id=BOT_ID, first_name="bot", is_bot=True, username="sherwoodagentbot"), text="riff")
    await h.on_group_message(SimpleNamespace(message=ptb_message(102, BOB, "ship it", reply_to=riff)), ctx)
    assert gw.drafts == []  # not the host
    await h.on_group_message(SimpleNamespace(message=ptb_message(103, HOST, "ship it", reply_to=riff)), ctx)
    assert len(gw.drafts) == 1 and gw.drafts[0].trigger == "ship_it"


def test_incoming_from_message_detects_mentions_commands_and_replies():
    bot_user = User(id=BOT_ID, first_name="bot", is_bot=True, username="sherwoodagentbot")
    m = ptb_message(5, ALICE, "hey bot long $ETH", entities=[MessageEntity(MessageEntity.TEXT_MENTION, 0, 7, user=bot_user)])
    inc = incoming_from_message(m, bot_id=BOT_ID, bot_username="sherwoodagentbot")
    assert inc.mentions_bot and not inc.is_command and inc.username == "u10"
    other = ptb_message(6, ALICE, "@someoneelse long $ETH", entities=[MessageEntity(MessageEntity.MENTION, 0, 12)])
    assert not incoming_from_message(other, bot_id=BOT_ID, bot_username="sherwoodagentbot").mentions_bot
    cmd = ptb_message(7, ALICE, "/fund", entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, 5)])
    assert incoming_from_message(cmd, bot_id=BOT_ID, bot_username="sherwoodagentbot").is_command
    riff = Message(message_id=8, date=m.date, chat=m.chat, from_user=bot_user, text="riff")
    rep = incoming_from_message(ptb_message(9, BOB, "🔥", reply_to=riff), bot_id=BOT_ID, bot_username="sherwoodagentbot")
    assert rep.reply_to_message_id == 8 and rep.reply_to_is_bot


async def test_observer_errors_are_swallowed():
    rt, gw, bot = make_runtime()
    rt.flow.on_message = AsyncMock(side_effect=RuntimeError("boom"))
    pitch = ptb_message(100, ALICE, "@sherwoodagentbot long $NVDA", entities=[MessageEntity(MessageEntity.MENTION, 0, 17)])
    assert await FundHandlers(rt).on_group_message(SimpleNamespace(message=pitch), SimpleNamespace(bot=bot)) is None


async def test_without_hermes_authz_only_members_and_host_can_pitch():
    """Review #28 F1(d): our observers run before Hermes' allowlist, so the flow checks the sender itself."""
    rt, gw, bot = make_runtime()
    rt.sender_check = None  # no adapter check wired: fall back to the fund's host + approved members
    h = FundHandlers(rt)
    ctx = SimpleNamespace(bot=bot)
    stranger = ptb_message(
        100, ALICE, "@sherwoodagentbot long $NVDA into earnings",
        entities=[MessageEntity(MessageEntity.MENTION, 0, 17)], username="alice",
    )
    await h.on_group_message(SimpleNamespace(message=stranger), ctx)
    assert rt.flow.open_pitches(CHAT) == []
    assert await rt.user_authorized(CHAT, HOST)


def test_wire_uses_the_adapter_sender_check():
    app = ApplicationBuilder().token("123:TEST").build()
    rt = TelegramRuntime(FakeGateway(), TelegramSettings(), secret=lambda: SECRET)
    seen = []
    adapter = SimpleNamespace(_is_callback_user_authorized=lambda uid, **kw: seen.append((uid, kw)) or uid == "10")
    wire(app, rt, adapter)
    assert asyncio.run(rt.user_authorized(CHAT, 10)) and not asyncio.run(rt.user_authorized(CHAT, 11))
    assert seen[0] == ("10", {"chat_id": str(CHAT), "chat_type": "group"})
