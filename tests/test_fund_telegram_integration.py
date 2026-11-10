"""The two lanes wired together: plugin register() -> Telegram factory -> FundService.

One in-memory scenario through the real plugin entry point, the real
FundService on a temp state dir, the real Telegram runtime/handlers/flow, a
fake PTB bot (tests/tg_fakes.py) and a mocked tx-builder tg-card read:

setup fund -> member taps Join -> host approves (gateway callback, signing link)
-> pitch + 3 fire reactions -> agent context -> agent drafts -> draft card ->
host ships + co-signs -> proposed/voting -> ONE proposal card -> back it ->
Against (chain read) -> card refresh -> guardian review -> execute -> settle
-> receipt + cooldown + leaderboard.
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

pytest.importorskip("telegram")

from sherwood_monitor.fund import applink  # noqa: E402
from sherwood_monitor.fund import telegram as fund_tg  # noqa: E402
from sherwood_monitor.fund import txbuilder  # noqa: E402
from sherwood_monitor.hooks import make_pre_llm_call_hook  # noqa: E402
from sherwood_monitor.event_buffer import EventBuffer  # noqa: E402
from sherwood_monitor.telegram.flow import IncomingMessage, ReactionChange  # noqa: E402
from sherwood_monitor.telegram.handler import FundHandlers  # noqa: E402
from sherwood_monitor.telegram.links import decode_sign_body, sign_link_message  # noqa: E402
from tests.fake_ctx import FakeCtx  # noqa: E402
from tests.fund_helpers import GOVERNOR, HOST_ADDR, MEMBER_ADDR, VAULT  # noqa: E402
from tests.tg_fakes import FakeJobQueue, fake_bot, refusal_text  # noqa: E402

CHAT = -100500
HOST_TG = 900
ALICE, BOB, CAROL, DAVE, EVE = 10, 11, 12, 13, 66
GROUP = {HOST_TG, ALICE, BOB, CAROL, DAVE}  # who Hermes' Telegram allowlist admits (EVE is a stranger)
SECRET = "integration-link-secret"
API_TOKEN = "integration-app-api-token"
BOT_ID = 999
HOST_CFG = {
    "agent_name": "alpha", "agent_identity_id": "101", "agent_address": HOST_ADDR,
    "owner_telegram_id": str(HOST_TG), "default_fund": "alpha-fund", "app_base_url": "https://app.example", "app_link_hosts": "app.example",
}


class ChainStub:
    """The tx-builder GET funds/<vault>/tg-card read, as sherwood docs/tx-builder.md shapes it."""

    def __init__(self) -> None:
        self.against_bps = 0
        self.state = "Pending"
        self.calls = 0

    def __call__(self, url: str, timeout: float):
        self.calls += 1
        assert url.startswith(f"https://api.example/api/v1/funds/{VAULT}/tg-card?chain=4663")
        return 200, {"success": True, "data": {
            "proposeGate": {"openProposals": 1, "cooldownUntil": "0", "ready": False},
            "governor": {"cooldownPeriod": "3600", "vetoThresholdBps": "2000"},
            "proposals": {"open": [{
                "id": "9", "state": self.state, "againstBps": self.against_bps, "vetoThresholdBps": 2000,
                "review": {"opened": True, "resolved": False, "blocked": False, "coverage": {"kind": "full"}},
            }], "recent": []},
        }}


class AppLinkStub:
    """sherwood-app #152: GET /api/tg/link?fund=&tg= (Bearer SHERWOOD_TG_API_TOKEN) -> linked address."""

    def __init__(self) -> None:
        self.links: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, headers: dict, timeout: float):
        self.calls.append((url, headers))
        assert headers["authorization"] == f"Bearer {API_TOKEN}"  # never the link-signing secret
        tg = parse_qs(urlsplit(url).query)["tg"][0]
        return (200, {"success": True, "data": {"address": self.links[tg]}}) if tg in self.links else (404, {})


def boot(tmp_path: Path, monkeypatch, chain: ChainStub, app_link: AppLinkStub | None = None) -> FakeCtx:
    for key in ("SHERWOOD_FUND_STATE_DIR", "TX_BUILDER_URL", "APP_BASE_URL", "TELEGRAM_BOT_USERNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SHERWOOD_TG_LINK_SECRET", SECRET)
    monkeypatch.setenv("SHERWOOD_TG_API_TOKEN", API_TOKEN)
    # No network: the tg-card read, the WOOD price read and the app link are stubbed at the transport.
    monkeypatch.setattr(txbuilder, "_urllib_get_json", chain)
    monkeypatch.setattr(fund_tg, "_get_json", lambda url, timeout: {"data": {"prices": [{"symbol": "WOOD", "price_usd": 0.25}]}})
    monkeypatch.setattr(applink, "_http_get_json", app_link or AppLinkStub())
    cfg_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    fund_cfg = {**HOST_CFG, "tx_builder_url": "https://api.example/api/v1"}
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"funds": [], "fund": fund_cfg}))
    ctx = FakeCtx()
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(side_effect=FileNotFoundError)):
        from sherwood_monitor import register

        register(ctx)
    return ctx


def connect(ctx: FakeCtx):
    """What Hermes does when its Telegram adapter connects: call the factory with the PTB Application."""
    (factory,) = ctx.platform_handlers["telegram"]
    added: list = []
    bot = fake_bot(BOT_ID)
    app = SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), add_handler=lambda h, group=0: added.append((h, group)))
    adapter = SimpleNamespace(_is_callback_user_authorized=lambda uid, **kw: int(uid) in GROUP)
    factory(app, adapter)
    handlers = next(h.callback.__self__ for h, _ in added if hasattr(h.callback, "__self__"))
    assert isinstance(handlers, FundHandlers)
    rt = handlers.rt
    bridge = rt.gateway.get_fund_for_chat.__self__
    assert isinstance(bridge, fund_tg.FundTelegramBridge) and bridge.runtime is rt and rt.connected
    return rt, bridge, bot, app


def query(data: str, user_id: int, *, username: str | None = None, message_id: int = 77):
    q = MagicMock()
    q.id = f"q-{user_id}-{data}"
    q.data = data
    q.from_user = SimpleNamespace(id=user_id, username=username or f"u{user_id}", first_name="F", last_name=None, is_bot=False)
    q.message = SimpleNamespace(chat=SimpleNamespace(id=CHAT, type="supergroup"), message_id=message_id,
                                text_html="card", is_topic_message=False)
    q.answer = AsyncMock(return_value=True)
    q.edit_message_reply_markup = AsyncMock(return_value=True)
    q.edit_message_text = AsyncMock(return_value=True)
    return q


async def tap(rt, bot, data, user_id, **kw):
    q = query(data, user_id, **kw)
    await FundHandlers(rt).on_callback(SimpleNamespace(callback_query=q), SimpleNamespace(bot=bot))
    return q


async def settle_tasks(n: int = 20) -> None:
    """Let submitted card tasks run (chain reads hop through a worker thread)."""
    for _ in range(n):
        await asyncio.sleep(0.005)


def sent_texts(bot) -> list[str]:
    return [c.kwargs.get("text", "") for c in bot.send_message.await_args_list]


def buttons(call) -> list[dict]:
    markup = call.kwargs.get("reply_markup")
    return [b for row in markup.to_dict()["inline_keyboard"] for b in row] if markup is not None else []


def verify_sign_url(url: str) -> tuple[str, dict]:
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert parts.path == "/tg/sign" and q["fund"] == VAULT.lower() and q["tg"] == str(HOST_TG)
    msg = sign_link_message(q["fund"], q["action"], q["body"], q["tg"], int(q["ts"]))
    assert hmac.compare_digest(q["sig"], hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest())
    return q["action"], decode_sign_body(q["body"])


async def test_register_wires_one_pre_llm_hook_and_a_lazy_telegram_factory(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    assert len(ctx.hooks["pre_llm_call"]) == 1  # the pitch context is composed in, not a second hook
    assert len(ctx.platform_handlers["telegram"]) == 1
    # Nothing touched Telegram before the adapter connected, and a card post is a no-op until then.
    ctx.call_tool("fund_setup", {"fund": "alpha-fund", "vault": VAULT, "governor": GOVERNOR,
                                 "telegram_chat_id": str(CHAT), "host_telegram_id": str(HOST_TG), "agent_fee_bps": 500})
    assert ctx.call_tool("fund_claim_topic", {"topic": "NVDA"})["ok"]


async def test_full_loop_in_memory(tmp_path, monkeypatch):
    chain = ChainStub()
    app_link = AppLinkStub()
    ctx = boot(tmp_path, monkeypatch, chain, app_link)
    out = ctx.call_tool("fund_setup", {
        "fund": "alpha-fund", "vault": VAULT, "governor": GOVERNOR, "telegram_chat_id": str(CHAT),
        "host_telegram_id": str(HOST_TG), "host_address": HOST_ADDR, "max_capital": "1000",
        "max_drawdown_bps": 2500, "voting_period": 1800, "agent_fee_bps": 500,
    })
    assert out["ok"] and out["created"]
    rt, bridge, bot, app = connect(ctx)
    svc = bridge.service

    # -- a member taps 🎟 Join: request recorded, host gets an Approve card, member a personal link
    q = await tap(rt, bot, f"shfund:join:{VAULT.lower()}", ALICE, username="alice")
    texts = sent_texts(bot)
    assert any("wants to join" in t for t in texts)
    ephemeral = bot.send_message.await_args_list[-1]
    assert "/tg/join?fund=" in buttons(ephemeral)[0]["url"] and "ephemeral_message_parameters" in ephemeral.kwargs["api_kwargs"]
    assert svc.load("alpha-fund").member(str(ALICE)) is not None

    # Host approves before the wallet is linked: the app lookup finds nothing, refused with the fallback.
    q = await tap(rt, bot, f"shfund:approve_member:{ALICE}", HOST_TG)
    assert "linked a wallet" in refusal_text(q, bot)
    assert f"/fund-approve {ALICE} <address>" in refusal_text(q, bot)  # plain text: shown literally
    # Alice finishes /tg/join; the app now answers GET /api/tg/link for her. The host's
    # ✅ Approve picks the address up (no /fund-approve typing) and hands the host a
    # signed /tg/sign link for approveDepositor.
    app_link.links[str(ALICE)] = MEMBER_ADDR
    n_before = bot.send_message.await_count
    q = await tap(rt, bot, f"shfund:approve_member:{ALICE}", HOST_TG, username="host")
    await settle_tasks()
    alice = svc.load("alpha-fund").member(str(ALICE))
    assert alice.approved and alice.address == MEMBER_ADDR
    assert app_link.calls[-1][0] == f"https://app.example/api/tg/link?fund={VAULT.lower()}&tg={ALICE}"
    signed = [c for c in bot.send_message.await_args_list[n_before:] if buttons(c) and "/tg/sign" in buttons(c)[0].get("url", "")]
    action, body = verify_sign_url(buttons(signed[0])[0]["url"])
    assert action == "approve-depositor" and body == {"chainId": 4663, "vault": VAULT, "depositor": MEMBER_ADDR}
    # Member #N is deposit order on chain (sherwood-app #153): no number before the first deposit.
    member_card = next(t for t in sent_texts(bot) if "🎟 <b>Member</b>" in t)
    assert "deposit order" in member_card
    assert f"🔗 wallet linked <code>{MEMBER_ADDR}</code>" in member_card  # full address, never truncated
    sign_msg = signed[0].kwargs["text"]
    assert f"Depositor: <code>{MEMBER_ADDR}</code>" in sign_msg and "approve-depositor" in sign_msg
    # A stranger cannot approve.
    q = await tap(rt, bot, f"shfund:approve_member:{BOB}", BOB)
    assert "Only the fund host" in refusal_text(q, bot)

    # -- a pitch, then 🔥 x3 -> DraftRequested -> the topic is claimed for the agent
    pitch = IncomingMessage(chat_id=CHAT, message_id=41, user_id=ALICE, username="alice",
                            text="@sherwoodagentbot long $NVDA and $AAPL into earnings", mentions_bot=True)
    rt.note_person(CHAT, SimpleNamespace(tg_id=ALICE, username="alice", first_name=None, last_name=None, handle="@alice"))
    await rt.flow.on_message(pitch)
    for user in (ALICE, EVE, BOB, CAROL):  # the author and a stranger do not count
        await rt.flow.on_reaction(ReactionChange(chat_id=CHAT, message_id=41, user_id=user, new=("🔥",)))
    await settle_tasks()
    assert ctx.call_tool("fund_status")["pitch_claims"] == []
    for user in (DAVE,):
        await rt.flow.on_reaction(ReactionChange(chat_id=CHAT, message_id=41, user_id=user, new=("🔥",)))
    await settle_tasks()
    claims = ctx.call_tool("fund_status")["pitch_claims"]
    assert [(c["agent"], c["topic"], c["idea_by"]) for c in claims] == [("alpha", "NVDA AAPL pitch", str(ALICE))]

    # The agent's next turn sees both the pitch context and the draft request, through ONE hook.
    (hook,) = ctx.hooks["pre_llm_call"]
    said = "@sherwoodagentbot long $NVDA and $AAPL into earnings"
    # Bound to the chat and sender: another chat or another member's turn gets nothing, not even
    # the fund scope. A CLI/cron turn (the operator's own surface) gets no chat-bound block; it
    # does get the scope of the fund the tools resolve to (here the only fund).
    def fund_blocks(out):
        return "sherwood-fund" in (out or {}).get("context", "")

    def chat_bound(out):
        text = (out or {}).get("context", "")
        return any(tag in text for tag in (
            "<sherwood-fund-pitch>", "<sherwood-fund-draft-requested>", "<sherwood-fund-shipped>",
        ))

    cli_turn = await hook(user_message=said)  # (drains the monitor's own warnings too)
    assert not chat_bound(cli_turn) and "<sherwood-fund-scope>" in cli_turn["context"]
    assert not fund_blocks(await hook(platform="telegram", chat_id="-100999", sender_id=str(ALICE), user_message=said))
    assert not fund_blocks(await hook(platform="telegram", chat_id=str(CHAT), sender_id=str(EVE), user_message=said))
    ctx_out = await hook(platform="telegram", chat_id=str(CHAT), sender_id=str(ALICE), user_message=said)
    assert "<sherwood-fund-scope>" in ctx_out["context"] and VAULT in ctx_out["context"]
    assert "<sherwood-fund-pitch>" in ctx_out["context"]
    assert "<sherwood-fund-draft-requested>" in ctx_out["context"] and 'topic="NVDA AAPL pitch"' in ctx_out["context"]

    # -- the agent drafts it (tool) -> the draft card posts as a reply to the pitch
    d = ctx.call_tool("fund_draft_proposal", {
        "topic": "NVDA AAPL pitch", "thesis": "AI capex keeps beating",
        "legs": [{"summary": "buy NVDA", "symbol": "NVDA", "weight_bps": 6000},
                 {"summary": "buy AAPL", "symbol": "AAPL", "weight_bps": 4000}],
        "max_capital": "100", "max_drawdown_bps": 2000, "strategy_duration_seconds": 21600,
        "bond_quote_wood": "40.8", "wood_balance": "10",
    })
    assert d["ok"] and d["proposal"]["idea_by"] == str(ALICE) and d["prepare"]["route"] == "propose-portfolio"
    await settle_tasks()
    draft_call = next(c for c in bot.send_message.await_args_list if "DRAFT" in c.kwargs.get("text", ""))
    assert draft_call.kwargs["reply_to_message_id"] == 41
    assert "from @alice's idea" in draft_call.kwargs["text"] and "swapping" in draft_call.kwargs["text"]
    assert [b["callback_data"] for b in buttons(draft_call)] == ["shfund:shipit:p-1", "shfund:propose:p-1"]

    # -- host ships it, then co-signs (pre-GA): a /tg/sign link for propose-portfolio
    q = await tap(rt, bot, "shfund:shipit:p-1", HOST_TG)
    assert "Shipped" in q.answer.await_args.kwargs["text"]
    n_before = bot.send_message.await_count
    await tap(rt, bot, "shfund:propose:p-1", HOST_TG)
    cosign = bot.send_message.await_args_list[n_before]
    action, body = verify_sign_url(buttons(cosign)[0]["url"])
    assert action == "propose-portfolio" and body["proposer"] == HOST_ADDR and body["maxCapital"] == "100000000"
    assert body["legs"] == [{"symbol": "NVDA", "weightBps": 6000}, {"symbol": "AAPL", "weightBps": 4000}]

    # -- proposed + voting in a burst -> exactly ONE proposal card, keyed by the on-chain id
    ctx.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "proposed", "onchain_id": 9, "tx_hash": "0x" + "ab" * 32})
    ctx.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "voting"})
    await settle_tasks(40)
    cards = [c for c in bot.send_message.await_args_list if "<b>Proposal #9</b>" in c.kwargs.get("text", "")]
    assert len(cards) == 1
    card_msg_id = int(svc.load("alpha-fund").proposal("9").card_message_id)
    assert {b.get("callback_data") for b in buttons(cards[0])} >= {"shfund:back:9", "shfund:vote:9"}

    # -- 👍 Back it (social) -> card edited in place
    q = await tap(rt, bot, "shfund:back:9", ALICE, message_id=card_msg_id)
    assert "Social only" in q.answer.await_args.kwargs["text"]
    assert svc.load("alpha-fund").proposal("9").backers == [str(ALICE)]
    await settle_tasks()
    # ❌ Object -> Alice's personal /tg/vote link names <governor>:<onchain id>
    n_before = bot.send_message.await_count
    await tap(rt, bot, "shfund:vote:9", ALICE)
    vote_url = buttons(bot.send_message.await_args_list[n_before])[0]["url"]
    assert f"/tg/vote?proposal={GOVERNOR.lower()}:9&tg={ALICE}" in vote_url

    # -- she signs Against in the browser; the 60 s refresh reads the chain -> card shows Against 5%
    chain.against_bps = 500
    bridge.chain_sync._next.clear()
    (job,) = [j for j in app.job_queue.jobs if not j.removed]
    edits_before = bot.edit_message_text.await_count
    await app.job_queue.fire(job)
    await settle_tasks(40)
    assert svc.load("alpha-fund").proposal("9").against_bps == 500
    edits = bot.edit_message_text.await_args_list[edits_before:]
    assert edits and "Against 5.0%" in edits[-1].kwargs["text"] and edits[-1].kwargs["message_id"] == card_msg_id

    # -- voting ends; the chain says GuardianReview -> card shows the guardian review
    chain.state = "GuardianReview"
    bridge.chain_sync._next.clear()
    await rt.refresh_proposal_card(CHAT, "9")
    await settle_tasks(40)
    assert svc.load("alpha-fund").proposal("9").status == "review"
    assert "GUARDIAN REVIEW" in bot.edit_message_text.await_args_list[-1].kwargs["text"]

    # -- approved, executed, settled -> receipt; cooldown; leaderboard
    for status in ("approved", "executed"):
        assert ctx.call_tool("fund_set_proposal_status", {"proposal_id": "9", "status": status})["ok"]
    # The share image (sherwood-app #153) answers 503 ("not readable yet") once, then 200: the bot
    # waits and sends the photo only after the 200 (Telegram keeps the first image it fetches).
    statuses = iter([503, 200])
    seen_urls = []
    monkeypatch.setattr(applink, "_image_status", lambda url, timeout: seen_urls.append(url) or next(statuses))
    bridge.image_retry_delays = (0.01,)
    s = ctx.call_tool("fund_record_settle", {"proposal_id": "9", "pnl": "8.2", "pnl_bps": 820, "tx_hash": "0x" + "cd" * 32})
    assert s["ok"] and "14-day challenge window" in s["post"]
    await settle_tasks(60)
    photo = next(c for c in bot.send_photo.await_args_list if "settled</b>" in c.kwargs.get("caption", ""))
    receipt = photo.kwargs["caption"]
    assert "<b>+8.2%</b> on NVDA/AAPL" in receipt and "idea by @alice" in receipt and "backed by 1" in receipt
    assert "14-day challenge window" in receipt
    from sherwood_monitor.telegram.links import checksum_address

    assert photo.kwargs["photo"] == seen_urls[0] == seen_urls[1]
    assert photo.kwargs["photo"].startswith(f"https://app.example/tg/receipt/{checksum_address(VAULT)}/9.png?by=alice&backers=1&sig=")
    assert "SETTLED" in bot.edit_message_text.await_args_list[-1].kwargs["text"]
    assert svc.load("alpha-fund").fund.cooldown_until is not None
    blocked = ctx.call_tool("fund_claim_topic", {"topic": "TSLA"})
    assert blocked["blocked"] and blocked["code"] == "cooldown"
    rows = await rt.gateway.leaderboard(CHAT)
    assert [(r.tg_id, r.pnl_bps, r.ideas, r.wins) for r in rows] == [(ALICE, 820, 1, 1)]


async def test_cards_are_a_noop_until_the_adapter_connects(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    ctx.call_tool("fund_setup", {"fund": "alpha-fund", "vault": VAULT, "governor": GOVERNOR,
                                 "telegram_chat_id": str(CHAT), "host_telegram_id": str(HOST_TG), "agent_fee_bps": 500})
    ctx.call_tool("fund_claim_topic", {"topic": "NVDA"})
    d = ctx.call_tool("fund_draft_proposal", {"topic": "NVDA", "thesis": "t", "legs": [{"summary": "buy NVDA", "symbol": "NVDA", "weight_bps": 10000}],
                                              "max_capital": "10", "max_drawdown_bps": 100, "strategy_duration_seconds": 3600})
    assert d["ok"]  # no adapter, no PTB bot: the observer drops the card, the write stands


async def test_composed_pre_llm_hook_skips_failing_sources():
    buf = EventBuffer()
    buf.push("<a/>")

    def broken(**_):
        raise RuntimeError("down")

    async def tg(**kwargs):
        return {"context": "<b/>"} if kwargs.get("platform") == "telegram" else None

    hook = make_pre_llm_call_hook(buf, [broken, tg])
    assert await hook(platform="telegram") == {"context": "<a/>\n\n<b/>"}
    assert await hook(platform="cli") is None


def test_wood_price_reader_is_cached_and_never_raises():
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        raise OSError("offline")

    t = [0.0]
    price = fund_tg.WoodPrice("https://api.example/api/v1", fetch=fetch, clock=lambda: t[0])
    assert price() is None and price() is None and len(calls) == 1
    t[0] = 601.0
    price._fetch = lambda url, timeout: {"success": True, "data": {"prices": [{"symbol": "USDG", "price_usd": 1.0}]}}
    assert price() is None  # /sources/prices has no WOOD feed today
    t[0] = 1300.0
    price._fetch = lambda url, timeout: [{"symbol": "WOOD", "price_usd": "0.00086"}]
    assert price() == pytest.approx(0.00086)


# -- live group test, 2026-11-10: bare "ship it", direct drafts, card logs ---------------------------


def _setup(ctx):
    out = ctx.call_tool("fund_setup", {
        "fund": "alpha-fund", "vault": VAULT, "governor": GOVERNOR, "telegram_chat_id": str(CHAT),
        "host_telegram_id": str(HOST_TG), "host_address": HOST_ADDR, "max_capital": "1000",
        "max_drawdown_bps": 2500, "voting_period": 1800, "agent_fee_bps": 500,
    })
    assert out["ok"]


NVDA_DRAFT = {
    "thesis": "AI capex keeps beating", "legs": [{"summary": "buy NVDA", "symbol": "NVDA", "weight_bps": 10000}],
    "max_capital": "40", "max_drawdown_bps": 2000, "strategy_duration_seconds": 21600,
}


async def test_bare_host_ship_it_claims_the_one_open_pitch_and_the_draft_card_replies_to_it(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    _setup(ctx)
    rt, bridge, bot, app = connect(ctx)
    await rt.flow.on_message(IncomingMessage(chat_id=CHAT, message_id=41, user_id=HOST_TG, username="host",
                                             text="@sherwoodagentbot 100% $NVDA for 6 hours", mentions_bot=True))
    ev = await rt.flow.on_message(IncomingMessage(chat_id=CHAT, message_id=42, user_id=HOST_TG, username="host",
                                                  text="@sherwoodagentbot ship it", mentions_bot=True))
    assert ev is not None and ev.source_message_id == 41
    await settle_tasks()
    claims = ctx.call_tool("fund_status")["pitch_claims"]
    assert [(c["agent"], c["topic"]) for c in claims] == [("alpha", "NVDA pitch")]
    (hook,) = ctx.hooks["pre_llm_call"]
    turn = await hook(platform="telegram", chat_id=str(CHAT), sender_id=str(HOST_TG), user_message="ship it")
    assert "<sherwood-fund-draft-requested>" in turn["context"]
    d = ctx.call_tool("fund_draft_proposal", {"topic": "NVDA pitch", **NVDA_DRAFT})
    assert d["ok"]
    await settle_tasks()
    draft_call = next(c for c in bot.send_message.await_args_list if "DRAFT" in c.kwargs.get("text", ""))
    assert draft_call.kwargs["reply_to_message_id"] == 41


PRICES = {"data": {"records": [
    {"symbol": "TSLA", "address": "0x" + "31" * 20}, {"symbol": "NVDA", "address": "0x" + "32" * 20},
]}}


async def test_ship_it_on_an_unpriced_pitch_is_refused_in_the_group_from_the_cached_prices(tmp_path, monkeypatch):
    from sherwood_monitor.fund import symbols

    ctx = boot(tmp_path, monkeypatch, ChainStub())
    _setup(ctx)
    rt, bridge, bot, app = connect(ctx)
    monkeypatch.setattr(symbols, "_get_json", lambda url, timeout: PRICES)
    symbols.source_for(bridge.service.tx_builder_url)()  # a claim or draft warmed the cache earlier
    monkeypatch.setattr(symbols, "_get_json", lambda url, timeout: pytest.fail("no network on the message path"))
    await rt.flow.on_message(IncomingMessage(chat_id=CHAT, message_id=41, user_id=HOST_TG, username="host",
                                             text="@sherwoodagentbot 100% $NFLX for 6 hours", mentions_bot=True))
    await rt.flow.on_message(IncomingMessage(chat_id=CHAT, message_id=42, user_id=HOST_TG, username="host",
                                             text="@sherwoodagentbot ship it", mentions_bot=True))
    await settle_tasks()
    (call,) = bot.send_message.await_args_list
    assert call.kwargs["reply_to_message_id"] == 41
    assert "NFLX is not in the Sherwood prices source" in call.kwargs["text"] and "NVDA" in call.kwargs["text"]
    assert ctx.call_tool("fund_status")["pitch_claims"] == []


async def test_ship_it_with_a_cold_prices_cache_claims_without_a_network_call(tmp_path, monkeypatch):
    from sherwood_monitor.fund import symbols

    ctx = boot(tmp_path, monkeypatch, ChainStub())
    _setup(ctx)
    rt, bridge, bot, app = connect(ctx)
    monkeypatch.setattr(symbols, "_get_json", lambda url, timeout: pytest.fail("no network on the message path"))
    await rt.flow.on_message(IncomingMessage(chat_id=CHAT, message_id=41, user_id=HOST_TG, username="host",
                                             text="@sherwoodagentbot 100% $NFLX for 6 hours", mentions_bot=True))
    await rt.flow.on_message(IncomingMessage(chat_id=CHAT, message_id=42, user_id=HOST_TG, username="host",
                                             text="@sherwoodagentbot ship it", mentions_bot=True))
    await settle_tasks()
    assert [c["topic"] for c in ctx.call_tool("fund_status")["pitch_claims"]] == ["NFLX pitch"]


async def test_ambiguous_host_ship_it_is_answered_in_the_group_by_the_plugin(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    _setup(ctx)
    rt, bridge, bot, app = connect(ctx)
    assert await rt.flow.on_message(IncomingMessage(chat_id=CHAT, message_id=42, user_id=HOST_TG, username="host",
                                                    text="@sherwoodagentbot ship it", mentions_bot=True)) is None
    (call,) = bot.send_message.await_args_list
    assert call.kwargs["chat_id"] == CHAT and call.kwargs["reply_to_message_id"] == 42
    assert "No open pitch" in call.kwargs["text"]
    assert ctx.call_tool("fund_status")["pitch_claims"] == []


async def test_direct_draft_without_a_pitch_posts_the_draft_card_and_logs_it(tmp_path, monkeypatch, caplog):
    """Item 5 of the live test: the agent called fund_draft_proposal itself, with no pitch claim."""
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    _setup(ctx)
    rt, bridge, bot, app = connect(ctx)
    assert ctx.call_tool("fund_claim_topic", {"topic": "100% NVDA 6h"})["ok"]
    caplog.set_level("INFO", logger="sherwood_monitor")
    d = ctx.call_tool("fund_draft_proposal", {"topic": "100% NVDA 6h", **NVDA_DRAFT})
    assert d["ok"] and d["proposal"]["pitch_message_id"] is None
    await settle_tasks()
    draft_call = next(c for c in bot.send_message.await_args_list if "DRAFT" in c.kwargs.get("text", ""))
    assert draft_call.kwargs.get("reply_to_message_id") is None
    posted = [r for r in caplog.records if r.getMessage().startswith("draft card posted")]
    assert len(posted) == 1 and posted[0].levelname == "INFO"
    assert f"chat={CHAT}" in posted[0].getMessage() and "draft=p-1" in posted[0].getMessage()


async def test_a_skipped_card_post_is_a_warning(tmp_path, monkeypatch, caplog):
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    _setup(ctx)  # no adapter connected
    ctx.call_tool("fund_claim_topic", {"topic": "NVDA"})
    caplog.set_level("INFO", logger="sherwood_monitor")
    assert ctx.call_tool("fund_draft_proposal", {"topic": "NVDA", **NVDA_DRAFT})["ok"]
    warned = [r for r in caplog.records if r.levelname == "WARNING" and "not connected" in r.getMessage()]
    assert warned and "draft card (chat=" in warned[0].getMessage()


async def test_a_failed_card_post_is_a_warning_without_the_error_text(tmp_path, monkeypatch, caplog):
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    _setup(ctx)
    rt, bridge, bot, app = connect(ctx)
    leak = "https://api.telegram.org/bot123:SECRET/sendMessage failed"
    bot.send_message.side_effect = RuntimeError(leak)
    bot.send_photo.side_effect = RuntimeError(leak)
    ctx.call_tool("fund_claim_topic", {"topic": "NVDA"})
    caplog.set_level("INFO", logger="sherwood_monitor")
    assert ctx.call_tool("fund_draft_proposal", {"topic": "NVDA", **NVDA_DRAFT})["ok"]
    await settle_tasks()
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "draft card" in r.getMessage()]
    assert warned and all("SECRET" not in w and "api.telegram.org" not in w for w in warned)


async def test_cards_from_tool_threads_post_when_the_factory_ran_outside_a_loop(tmp_path, monkeypatch):
    """If Hermes called the factory with no running loop, the first handler call binds it."""
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    _setup(ctx)
    rt, bridge, bot, app = connect(ctx)
    rt.loop = None
    await FundHandlers(rt).on_group_message(SimpleNamespace(message=None), SimpleNamespace(bot=bot))
    assert rt.loop is asyncio.get_running_loop()
    ctx.call_tool("fund_claim_topic", {"topic": "NVDA"})
    # A tool runs off the event loop (Hermes' worker thread): the card is submitted thread-safe.
    d = await asyncio.to_thread(ctx.call_tool, "fund_draft_proposal", {"topic": "NVDA", **NVDA_DRAFT})
    assert d["ok"]
    await settle_tasks()
    assert any("DRAFT" in c.kwargs.get("text", "") for c in bot.send_message.await_args_list)
