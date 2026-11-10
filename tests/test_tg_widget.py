"""👤 My position: the DM widget (Bot API 10.4 sendWidget), its fallbacks, data safety, refreshes."""
from __future__ import annotations

import dataclasses
import json
import zlib
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")

from telegram.error import BadRequest, Forbidden, InvalidToken

from sherwood_monitor.telegram.handler import FundHandlers
from sherwood_monitor.telegram.types import MemberView
from sherwood_monitor.telegram.widget import (
    CAPTION_MAX,
    DATA_MAX_BYTES,
    HTML_MAX_BYTES,
    HTML_MAX_ZLIB,
    WIDGET_HTML,
    PositionProposal,
    build_position,
    position_text,
    widget_data,
)
from tests.test_tg_handler import SECRET, answered_text, make_runtime, start_update, tap
from tests.tg_fakes import (
    ALICE,
    BOB,
    CHAT,
    HOST,
    NOW,
    VAULT,
    FakeGateway,
    make_proposal,
)

AS_OF = int(NOW) - 60
HOSTILE = "</script><img src=x onerror=\"alert(1)\">'\\ & <b>\u202e\x01"
TAP = f"shfund:position:{VAULT}"


def set_fund(gw, **kw):
    gw.fund = dataclasses.replace(gw.fund, **kw)
    return gw.fund


def api_calls(bot, endpoint="sendWidget"):
    return [c for c in bot.do_api_request.await_args_list if c.args and c.args[0] == endpoint]


# -- the document --------------------------------------------------------------------------------


def test_widget_html_fits_the_limits_and_carries_no_data():
    raw = WIDGET_HTML.encode("utf-8")
    assert 0 < len(raw) <= HTML_MAX_BYTES
    assert len(zlib.compress(raw, 9)) <= HTML_MAX_ZLIB  # Python's zlib counts as the server does
    assert raw.isascii() and "\x00" not in WIDGET_HTML
    low = WIDGET_HTML.lower()
    for banned in ("http", "src=", "fetch", "xmlhttprequest", "websocket", "@import", "url(", "innerhtml", "eval("):
        assert banned not in low, banned
    # State comes from the data channel and is drawn as text.
    assert "TelegramWidget" in WIDGET_HTML and "chatData" in WIDGET_HTML and "textContent" in WIDGET_HTML


# -- what the position shows -----------------------------------------------------------------------


def test_rows_come_only_from_values_the_plugin_has():
    f = FakeGateway().fund  # NAV 10,000 but never read from the chain (nav_as_of None)
    pos = build_position(f, BOB)
    assert pos.rows == ()  # no NAV row, no P&L row: never a made-up number; no member row for a stranger
    assert pos.note.startswith("Not a member yet")
    f = dataclasses.replace(
        f, nav_as_of=AS_OF, share_price=1.082, members=(MemberView(ALICE, 3, None, joined_at=AS_OF),)
    )
    assert dict(build_position(f, ALICE).rows) == {
        "NAV": "10,000 USDC", "P&L": "+8.2%", "Share price": "1.082 USDC", "You": "Member #3", "Joined": "Nov 14, 2023",
    }
    assert build_position(f, ALICE).note == ""
    partial = dict(build_position(dataclasses.replace(f, nav_partial=True), ALICE).rows)
    assert partial["NAV"] == "≥10,000 USDC" and "P&L" not in partial and "Share price" not in partial
    at_cost = dict(build_position(dataclasses.replace(f, nav_at_cost=True), ALICE).rows)
    assert at_cost["NAV"] == "10,000 USDC (at cost)" and "P&L" not in at_cost
    first_deposit_pending = dataclasses.replace(f, members=(MemberView(ALICE, 0, None),), host_tg_id=ALICE)
    assert dict(build_position(first_deposit_pending, ALICE).rows)["You"] == "Host · Member"
    assert dict(build_position(f, HOST).rows)["You"] == "Host"


def test_open_proposals_with_vote_state():
    voting = make_proposal(backers=(ALICE,))
    proposed = make_proposal(id="43", status="proposed", objectors=(ALICE,))
    f = dataclasses.replace(FakeGateway().fund, open_proposals=(voting, proposed))
    pos = build_position(f, ALICE)
    assert pos.proposals == (
        PositionProposal("42", "Long NVDA into earnings", "🗳 VOTING", 500, 2000, int(NOW) + 1800, "b"),
        PositionProposal("43", "Long NVDA into earnings", "📨 PROPOSED", None, None, None, "o"),
    )
    text = position_text(pos, now=NOW)
    assert "#42 Long NVDA into earnings · 🗳 VOTING · Against 5.0% of 20% · ends 22:43 UTC · 👍 you back this" in text
    assert "#43 Long NVDA into earnings · 📨 PROPOSED · ❌ you object in chat" in text


# -- data safety -------------------------------------------------------------------------------------


def test_hostile_strings_travel_as_data_and_show_literally():
    evil = make_proposal(id="7", thesis=HOSTILE, objectors=(ALICE,))
    f = dataclasses.replace(
        FakeGateway().fund, name=HOSTILE, nav_symbol=HOSTILE, nav_as_of=AS_OF, open_proposals=(evil,),
        members=(MemberView(ALICE, 1, None, username=HOSTILE),),
    )
    pos = build_position(f, ALICE)
    data = json.loads(widget_data(pos, now=NOW))
    assert data["f"].startswith("</script><img src=x onerror=\"alert(1)\">")  # verbatim, as a JSON string
    assert data["p"][0]["t"].startswith("</script><img") and data["p"][0]["m"] == "o"
    assert "</script><img" not in WIDGET_HTML  # the document is fixed; the widget draws data with textContent
    assert position_text(pos, now=NOW).startswith("👤 Your position · </script><img")  # plain text, no markup


def test_data_and_caption_stay_within_telegram_limits():
    big = "😈" * 300
    f = dataclasses.replace(
        FakeGateway().fund, name=big, nav_symbol=big, nav_as_of=AS_OF, share_price=1.5,
        open_proposals=tuple(make_proposal(id=str(i), thesis=big, backers=(ALICE,)) for i in range(12)),
        members=(MemberView(ALICE, 2, None, joined_at=AS_OF),),
    )
    pos = build_position(f, ALICE)
    raw = widget_data(pos, now=NOW)
    assert len(raw.encode("utf-8")) <= DATA_MAX_BYTES
    data = json.loads(raw)
    assert data["u"] == int(NOW) and data["x"] + len(data.get("p", [])) == 12
    assert len(position_text(pos, now=NOW)) <= CAPTION_MAX
    ctrl = "\x01" * 3000  # JSON-escaped to 6 bytes each
    worst = dataclasses.replace(f, name=ctrl, open_proposals=tuple(make_proposal(id=str(i), thesis=ctrl) for i in range(5)))
    assert len(widget_data(build_position(worst, ALICE), now=NOW).encode("utf-8")) <= DATA_MAX_BYTES


async def test_widget_carries_no_links_or_secrets():
    rt, gw, bot = make_runtime()
    set_fund(gw, nav_as_of=AS_OF, members=(MemberView(ALICE, 1, None),), deposits_open=True)
    await tap(rt, bot, TAP, ALICE)
    kw = api_calls(bot)[0].kwargs["api_kwargs"]
    blob = kw["chat_data"] + kw["caption"]
    assert SECRET not in blob and "sig=" not in blob and "/tg/" not in blob and "http" not in blob
    # The one URL is the public, unsigned fund page.
    assert kw["reply_markup"].to_dict()["inline_keyboard"] == [
        [{"text": "📊 View fund", "url": f"https://app.sherwood.sh/tg/fund/{VAULT}"}]
    ]


# -- delivery -------------------------------------------------------------------------------------


async def test_my_position_sends_the_widget_to_the_members_dm_only():
    rt, gw, bot = make_runtime()
    set_fund(gw, nav_as_of=AS_OF, members=(MemberView(ALICE, 3, None),))
    q = await tap(rt, bot, TAP, ALICE)
    (call,) = api_calls(bot)
    kw = call.kwargs["api_kwargs"]
    assert kw["chat_id"] == ALICE != CHAT and kw["html"] is WIDGET_HTML
    assert "allow_queries" not in kw and "is_shared" not in kw and "parse_mode" not in kw
    data = json.loads(kw["chat_data"])
    assert data["f"] == "Robin Fund" and ["You", "Member #3"] in data["r"] and data["o"] == AS_OF
    assert kw["caption"].startswith("👤 Your position · Robin Fund\nNAV: 10,000 USDC")
    bot.send_message.assert_not_awaited()  # nothing in the group, no text fallback
    assert answered_text(q) == "Sent your position to our DM chat (@sherwoodagentbot)."
    assert rt.widgets.sent[CHAT][ALICE][0] == 501
    q = await tap(rt, bot, "shfund:position:0x3333333333333333333333333333333333333333", ALICE)
    assert answered_text(q) == "This button belongs to another fund." and len(api_calls(bot)) == 1


@pytest.mark.parametrize(
    "refusal",
    [
        InvalidToken("Not Found"),  # an older Bot API: 404 for an unknown method (PTB raises InvalidToken)
        BadRequest("Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE"),
        AttributeError("'Bot' object has no attribute 'do_api_request'"),
    ],
)
async def test_a_refused_widget_falls_back_to_a_text_dm(refusal):
    rt, gw, bot = make_runtime()
    set_fund(gw, name=HOSTILE, nav_as_of=AS_OF)
    bot.do_api_request.side_effect = refusal
    q = await tap(rt, bot, TAP, BOB)
    kw = bot.send_message.await_args.kwargs
    assert kw["chat_id"] == BOB and "parse_mode" not in kw and "api_kwargs" not in kw
    assert kw["text"].startswith("👤 Your position · </script><img")  # the caption's text, literally
    assert kw["reply_markup"].to_dict()["inline_keyboard"][0][0]["text"] == "📊 View fund"
    assert answered_text(q) == "Sent your position to our DM chat (@sherwoodagentbot)."
    assert rt.widgets.sent == {}


async def test_no_dm_yet_opens_the_start_link_and_start_sends_the_widget():
    rt, _, bot = make_runtime()
    working = bot.do_api_request.side_effect
    bot.do_api_request.side_effect = Forbidden("Forbidden: bot can't initiate conversation with a user")
    q = await tap(rt, bot, TAP, BOB)
    assert q.answer.await_args.kwargs["url"] == f"https://t.me/sherwoodagentbot?start=sfm_{VAULT[2:]}"
    bot.send_message.assert_not_awaited()  # Forbidden: no text DM attempt either
    bot.do_api_request.side_effect = working
    upd, msg = start_update(f"/start sfm_{VAULT[2:]}", BOB)
    msg.chat = SimpleNamespace(id=BOB, type="private")
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    assert api_calls(bot)[-1].kwargs["api_kwargs"]["chat_id"] == BOB
    upd, msg = start_update("/start sfm_" + "4" * 40, BOB)
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    msg.reply_text.assert_awaited_once_with("That fund isn't available here.")


async def test_a_failed_text_dm_opens_the_start_link():
    rt, _, bot = make_runtime()
    bot.do_api_request.side_effect = BadRequest("Bad Request: chat not found")
    bot.send_message.side_effect = BadRequest("Bad Request: chat not found")
    q = await tap(rt, bot, TAP, BOB)
    assert q.answer.await_args.kwargs["url"].endswith(f"?start=sfm_{VAULT[2:]}")


# -- refreshes ------------------------------------------------------------------------------------


async def test_refresh_rewrites_changed_widgets_only():
    rt, gw, bot = make_runtime()
    set_fund(gw, nav_as_of=AS_OF, open_proposals=(gw.proposals["42"],), members=(MemberView(ALICE, 1, None),))
    await tap(rt, bot, TAP, ALICE)
    message_id = rt.widgets.sent[CHAT][ALICE][0]
    assert await rt.refresh_position_widgets(CHAT) == 0  # nothing changed: no call
    assert api_calls(bot, "editMessageWidgetData") == []
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], against_bps=1500),))
    rt.clock.t += 120
    assert await rt.refresh_position_widgets(CHAT) == 1
    (edit,) = api_calls(bot, "editMessageWidgetData")
    kw = edit.kwargs["api_kwargs"]
    assert kw["chat_id"] == ALICE and kw["message_id"] == message_id
    data = json.loads(kw["chat_data"])
    assert data["p"][0]["a"] == 1500 and data["u"] == int(NOW) + 120
    caption = bot.edit_message_caption.await_args.kwargs  # clients without widgets see the same numbers
    assert caption["chat_id"] == ALICE and "Against 15.0% of 20%" in caption["caption"]
    assert caption["reply_markup"].to_dict()["inline_keyboard"][0][0]["text"] == "📊 View fund"
    assert await rt.refresh_position_widgets(CHAT) == 0


async def test_a_proposal_card_refresh_moves_the_widgets_too():
    rt, gw, bot = make_runtime()
    gw.proposals["42"] = dataclasses.replace(gw.proposals["42"], card_message_id=77)
    set_fund(gw, open_proposals=(gw.proposals["42"],), members=(MemberView(ALICE, 1, None),))
    await tap(rt, bot, TAP, ALICE)
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], against_bps=1900),))
    await rt.refresh_proposal_card(CHAT, "42")
    (edit,) = api_calls(bot, "editMessageWidgetData")
    assert json.loads(edit.kwargs["api_kwargs"]["chat_data"])["p"][0]["a"] == 1900


async def test_refresh_forgets_widgets_it_cannot_edit():
    rt, gw, bot = make_runtime()
    set_fund(gw, open_proposals=(gw.proposals["42"],))
    await tap(rt, bot, TAP, ALICE)
    await tap(rt, bot, TAP, BOB)
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], against_bps=900),))

    async def api(endpoint, api_kwargs=None, **kw):
        if api_kwargs["chat_id"] == ALICE:
            raise BadRequest("Bad Request: message is not modified")
        raise Forbidden("Forbidden: bot was blocked by the user")

    bot.do_api_request.side_effect = api
    assert await rt.refresh_position_widgets(CHAT) == 1  # "not modified" counts as done
    assert set(rt.widgets.sent[CHAT]) == {ALICE}  # BOB blocked the bot: forgotten
