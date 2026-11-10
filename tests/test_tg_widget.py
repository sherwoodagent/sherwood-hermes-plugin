"""👤 My position: the DM widget (Bot API 10.4 sendWidget), its fallbacks, data safety, refreshes."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import subprocess
import zlib
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")

from telegram.error import (
    BadRequest,
    EndPointNotFound,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TimedOut,
)

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
from tests.test_tg_handler import (
    SECRET,
    answered_text,
    command_update,
    make_runtime,
    start_update,
    tap,
)
from tests.tg_fakes import (
    ALICE,
    BOB,
    CAROL,
    CHAT,
    DAVE,
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
        # An older Bot API: 404 for an unknown method. PTB raises EndPointNotFound once the bot is
        # initialized (always under a running Application), InvalidToken before.
        EndPointNotFound("Endpoint 'sendWidget' not found in Bot API"),
        InvalidToken("Not Found"),
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
    assert kw["chat_id"] == BOB and kw["parse_mode"] is None and "api_kwargs" not in kw
    assert kw["text"].startswith("👤 Your position · </script><img")  # the caption's text, literally
    assert kw["reply_markup"].to_dict()["inline_keyboard"][0][0]["text"] == "📊 View fund"
    assert answered_text(q) == "Sent your position to our DM chat (@sherwoodagentbot)."
    assert rt.widgets.sent == {}


async def test_no_dm_yet_opens_the_start_link_and_start_sends_the_widget():
    rt, gw, bot = make_runtime()
    set_fund(gw, members=(MemberView(BOB, 2, None),))
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


def members(*ids):
    return tuple(MemberView(i, n, None) for n, i in enumerate(ids, start=1))


async def edits_after(rt, bot):
    """The widget data edits once every scheduled refresh pass has finished."""
    idle = getattr(rt.widgets, "idle", None)
    if idle is not None:
        await idle()
    return api_calls(bot, "editMessageWidgetData")


async def test_refresh_rewrites_changed_widget_data_only():
    rt, gw, bot = make_runtime()
    set_fund(gw, nav_as_of=AS_OF, open_proposals=(gw.proposals["42"],), members=members(ALICE))
    await tap(rt, bot, TAP, ALICE)
    message_id = rt.widgets.sent[CHAT][ALICE][0]
    assert await rt.widgets.refresh(bot, CHAT) == 0  # nothing changed: no call
    assert api_calls(bot, "editMessageWidgetData") == []
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], against_bps=1500),))
    rt.clock.t += 120
    assert await rt.widgets.refresh(bot, CHAT) == 1
    (edit,) = api_calls(bot, "editMessageWidgetData")
    kw = edit.kwargs["api_kwargs"]
    assert kw["chat_id"] == ALICE and kw["message_id"] == message_id
    data = json.loads(kw["chat_data"])
    assert data["p"][0]["a"] == 1500 and data["u"] == int(NOW) + 120
    # One call per changed widget: the caption is a dated snapshot and is not edited.
    bot.edit_message_caption.assert_not_awaited()
    assert await rt.widgets.refresh(bot, CHAT) == 0


async def test_a_newer_read_with_the_same_values_edits_nothing():
    """nav_as_of moves on every chain read; alone it must not rewrite every widget."""
    rt, gw, bot = make_runtime()
    set_fund(gw, nav_as_of=AS_OF, members=members(ALICE, BOB))
    await tap(rt, bot, TAP, ALICE)
    await tap(rt, bot, TAP, BOB)
    set_fund(gw, nav_as_of=AS_OF + 600)
    assert await rt.widgets.refresh(bot, CHAT) == 0
    assert api_calls(bot, "editMessageWidgetData") == []


async def test_a_slow_refresh_never_holds_the_card_lock_or_the_fund_reply():
    """MEDIUM-1: the pass runs as its own task; the card sync under its lock and /fund return at once."""
    rt, gw, bot = make_runtime()
    gw.proposals["42"] = dataclasses.replace(gw.proposals["42"], card_message_id=77)
    set_fund(gw, open_proposals=(gw.proposals["42"],), members=members(ALICE))
    await tap(rt, bot, TAP, ALICE)
    gate = asyncio.Event()
    sent = bot.do_api_request.side_effect

    async def hanging(endpoint, api_kwargs=None, **kw):
        if endpoint == "editMessageWidgetData":
            await gate.wait()  # a Telegram call that never comes back
        return await sent(endpoint, api_kwargs, **kw)

    bot.do_api_request.side_effect = hanging
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], against_bps=1900),))
    lock = asyncio.Lock()  # as FundTelegramBridge._sync_card holds it around refresh_proposal_card

    async def sync_card():
        async with lock:
            await rt.refresh_proposal_card(CHAT, "42")

    await asyncio.wait_for(sync_card(), 1.0)
    assert not lock.locked()
    upd, msg = command_update()
    await asyncio.wait_for(FundHandlers(rt).cmd_fund(upd, SimpleNamespace(bot=bot)), 1.0)
    msg.reply_html.assert_awaited_once()
    gate.set()
    (edit,) = await edits_after(rt, bot)  # the pass still lands, once
    assert json.loads(edit.kwargs["api_kwargs"]["chat_data"])["p"][0]["a"] == 1900


async def test_overlapping_requests_collapse_and_edit_each_widget_once():
    """Three card refreshes from one chain read: one pass plus at most one follow-up, no repeated edits."""
    rt, gw, bot = make_runtime()
    for pid in ("42", "43", "44"):
        gw.proposals[pid] = make_proposal(id=pid, card_message_id=70 + int(pid))
    set_fund(gw, open_proposals=tuple(gw.proposals[p] for p in ("42", "43", "44")), members=members(ALICE, BOB))
    await tap(rt, bot, TAP, ALICE)
    await tap(rt, bot, TAP, BOB)
    sent = bot.do_api_request.side_effect

    async def slow(endpoint, api_kwargs=None, **kw):
        await asyncio.sleep(0.01)
        return await sent(endpoint, api_kwargs, **kw)

    bot.do_api_request.side_effect = slow
    set_fund(gw, open_proposals=tuple(dataclasses.replace(gw.proposals[p], against_bps=900) for p in ("42", "43", "44")))
    await asyncio.gather(*(rt.refresh_proposal_card(CHAT, p) for p in ("42", "43", "44")))
    edits = await edits_after(rt, bot)
    assert sorted(e.kwargs["api_kwargs"]["chat_id"] for e in edits) == [ALICE, BOB]


async def test_a_pass_is_bounded_and_rate_limits_end_it():
    from sherwood_monitor.telegram import cards_live

    bound = getattr(cards_live, "WIDGET_EDITS_PER_PASS", 20)
    rt, gw, bot = make_runtime()
    ids = tuple(range(1000, 1000 + bound + 5))
    set_fund(gw, open_proposals=(gw.proposals["42"],), members=members(*ids))
    for i in ids:
        await tap(rt, bot, TAP, i)
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], against_bps=700),))
    assert await rt.widgets.refresh(bot, CHAT) == bound
    assert await rt.widgets.refresh(bot, CHAT) == 5  # the rest, next pass
    assert len(api_calls(bot, "editMessageWidgetData")) == bound + 5
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], against_bps=800),))
    bot.do_api_request.side_effect = RetryAfter(3)
    assert await rt.widgets.refresh(bot, CHAT) == 0
    assert len(api_calls(bot, "editMessageWidgetData")) == bound + 6  # stopped after one


async def test_refresh_forgets_only_widgets_that_are_gone():
    rt, gw, bot = make_runtime()
    set_fund(gw, open_proposals=(gw.proposals["42"],), members=members(ALICE, BOB, CAROL, DAVE, HOST))
    for who in (ALICE, BOB, CAROL, DAVE, HOST):
        await tap(rt, bot, TAP, who)
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], against_bps=900),))
    errors = {
        ALICE: TimedOut(),
        BOB: NetworkError("httpx.ConnectError"),
        CAROL: Forbidden("Forbidden: bot was blocked by the user"),
        DAVE: BadRequest("Bad Request: message to edit not found"),
        HOST: BadRequest("Bad Request: message is not modified"),
    }

    async def api(endpoint, api_kwargs=None, **kw):
        raise errors[api_kwargs["chat_id"]]

    bot.do_api_request.side_effect = api
    assert await rt.widgets.refresh(bot, CHAT) == 1  # "not modified" counts as done
    assert set(rt.widgets.sent[CHAT]) == {ALICE, BOB, HOST}  # blocked and deleted ones are forgotten
    bot.do_api_request.side_effect = None
    bot.do_api_request.return_value = True
    assert await rt.widgets.refresh(bot, CHAT) == 2  # the transient failures are retried


async def test_an_implausible_vote_end_still_answers_and_refreshes():
    """LOW-2: a millisecond voteEnd leaves the countdown out; the tap is answered, refreshes run."""
    rt, gw, bot = make_runtime()
    set_fund(gw, open_proposals=(gw.proposals["42"],), members=members(ALICE, BOB))
    await tap(rt, bot, TAP, ALICE)
    set_fund(gw, open_proposals=(dataclasses.replace(gw.proposals["42"], voting_ends_at=10**15),))
    q = await tap(rt, bot, TAP, BOB)
    assert answered_text(q) == "Sent your position to our DM chat (@sherwoodagentbot)."
    kw = api_calls(bot)[-1].kwargs["api_kwargs"]
    assert "e" not in json.loads(kw["chat_data"])["p"][0] and "ends" not in kw["caption"]
    assert await rt.widgets.refresh(bot, CHAT) == 1
    assert [e.kwargs["api_kwargs"]["chat_id"] for e in api_calls(bot, "editMessageWidgetData")] == [ALICE]


# -- delivery edge cases (review of #42) ------------------------------------------------------------


async def test_a_slow_or_rate_limited_widget_does_not_step_down():
    """NIT-2: TimedOut (it may have arrived) sends no text copy; RetryAfter says try again, no start link."""
    rt, gw, bot = make_runtime()
    set_fund(gw, members=members(ALICE))
    bot.do_api_request.side_effect = TimedOut()
    q = await tap(rt, bot, TAP, ALICE)
    bot.send_message.assert_not_awaited()
    assert "took too long" in answered_text(q) and "url" not in q.answer.await_args.kwargs
    bot.do_api_request.side_effect = RetryAfter(5)
    q = await tap(rt, bot, TAP, ALICE)
    bot.send_message.assert_not_awaited()
    assert answered_text(q) == "Telegram is busy right now. Tap 👤 My position again in a few seconds."
    assert "url" not in q.answer.await_args.kwargs


async def test_start_link_position_is_for_members_only():
    """LOW-4: /start sfm_<vault> carries no secret, so a stranger gets one generic line and no slot."""
    rt, gw, bot = make_runtime()
    set_fund(gw, name="Secret Alpha", nav_as_of=AS_OF, members=members(ALICE))
    upd, msg = start_update(f"/start sfm_{VAULT[2:]}", CAROL)
    msg.chat = SimpleNamespace(id=CAROL, type="private")
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    assert api_calls(bot) == [] and bot.send_message.await_count == 0
    (line,) = msg.reply_text.await_args.args
    assert "member" in line and "Secret Alpha" not in line and "NAV" not in line
    assert rt.widgets.sent == {}
    # The host gets it through the same payload.
    upd, msg = start_update(f"/start sfm_{VAULT[2:]}", HOST)
    msg.chat = SimpleNamespace(id=HOST, type="private")
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    assert api_calls(bot)[-1].kwargs["api_kwargs"]["chat_id"] == HOST and HOST in rt.widgets.sent[CHAT]


async def test_a_group_tap_by_a_non_member_takes_no_refresh_slot():
    rt, gw, bot = make_runtime()
    set_fund(gw, members=members(ALICE))
    await tap(rt, bot, TAP, CAROL)
    assert api_calls(bot)[-1].kwargs["api_kwargs"]["chat_id"] == CAROL  # they still get the fund rows
    assert rt.widgets.sent.get(CHAT, {}) == {}


@pytest.mark.parametrize(
    ("text_error", "expected"),
    [
        (RetryAfter(5), "Telegram is busy right now. Tap 👤 My position again in a few seconds."),
        (BadRequest("Bad Request: chat not found"), "Couldn't send your position right now. Tap 👤 My position on the fund card in the group again."),
    ],
)
async def test_start_position_never_leaves_an_empty_chat(text_error, expected):
    """LOW-5: widget and text both fail after the start link: one plain line, not an empty DM."""
    rt, gw, bot = make_runtime()
    set_fund(gw, members=members(ALICE))
    bot.do_api_request.side_effect = BadRequest("Bad Request: INPUT_MEDIA_WIDGET_HTML_INVALID")
    bot.send_message.side_effect = text_error
    upd, msg = start_update(f"/start sfm_{VAULT[2:]}", ALICE)
    msg.chat = SimpleNamespace(id=ALICE, type="private")
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    msg.reply_text.assert_awaited_once_with(expected)


# -- the script, under Node with a stand-in TelegramWidget (not QuickJS) ------------------------------

WIDGET_HARNESS = r"""
const fs = require('fs');
const js = fs.readFileSync(process.argv[2], 'utf8');
function node(tag) {
  const n = {tag, className: '', children: [], style: {}, _t: null};
  Object.defineProperty(n, 'textContent', {set(v) { n._t = v; }, get() { return n._t; }});
  n.appendChild = (c) => { n.children.push(c); return c; };
  n.replaceChildren = () => { n.children = []; };
  return n;
}
const root = node('div');
const out = [];
for (const data of JSON.parse(process.argv[3])) {
  let ready = 0;
  global.document = {createElement: node, getElementById: () => root};
  global.window = {TelegramWidget: {chatData: data, onEvent() {}, ready() { ready++; }}};
  global.setInterval = () => 0;
  let error = null;
  try { eval(js); } catch (e) { error = String(e); }
  const texts = [];
  (function walk(n) { if (n._t != null) texts.push(n._t); n.children.forEach(walk); })(root);
  out.push({error, ready, texts});
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_widget_script_survives_bad_rows_and_always_calls_ready(tmp_path):
    """NIT-5: ready() on the "No data yet." path too; a null row or proposal is skipped, not thrown on."""
    import re

    script = re.search(r"<script>(.*)</script>", WIDGET_HTML, re.DOTALL).group(1)
    (tmp_path / "widget.js").write_text(script)
    (tmp_path / "harness.js").write_text(WIDGET_HARNESS)
    cases = [None, '{"f":"Fund","r":[null,["NAV","1 USDG"]],"p":[null,{"i":"7","t":"T","s":"S"}]}']
    run = subprocess.run(
        ["node", str(tmp_path / "harness.js"), str(tmp_path / "widget.js"), json.dumps(cases)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    empty, bad_rows = json.loads(run.stdout)
    assert empty == {"error": None, "ready": 1, "texts": ["No data yet."]}
    assert bad_rows["error"] is None and bad_rows["ready"] == 1
    assert "NAV" in bad_rows["texts"] and "#7 T" in bad_rows["texts"]
