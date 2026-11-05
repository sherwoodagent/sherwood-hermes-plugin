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

from sherwood_monitor.fund import telegram as fund_tg  # noqa: E402
from sherwood_monitor.fund import txbuilder  # noqa: E402
from sherwood_monitor.hooks import make_pre_llm_call_hook  # noqa: E402
from sherwood_monitor.event_buffer import EventBuffer  # noqa: E402
from sherwood_monitor.telegram.flow import IncomingMessage, ReactionChange  # noqa: E402
from sherwood_monitor.telegram.handler import FundHandlers  # noqa: E402
from sherwood_monitor.telegram.links import decode_sign_body, sign_link_message  # noqa: E402
from tests.fake_ctx import FakeCtx  # noqa: E402
from tests.fund_helpers import GOVERNOR, HOST_ADDR, MEMBER_ADDR, VAULT  # noqa: E402
from tests.tg_fakes import FakeJobQueue, fake_bot  # noqa: E402

CHAT = -100500
HOST_TG = 900
ALICE, BOB, CAROL = 10, 11, 12
SECRET = "integration-link-secret"
BOT_ID = 999
HOST_CFG = {
    "agent_name": "alpha", "agent_identity_id": "101", "agent_address": HOST_ADDR,
    "owner_telegram_id": str(HOST_TG), "default_fund": "alpha-fund", "app_base_url": "https://app.example",
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


def boot(tmp_path: Path, monkeypatch, chain: ChainStub) -> FakeCtx:
    for key in ("SHERWOOD_FUND_STATE_DIR", "TX_BUILDER_URL", "APP_BASE_URL", "TELEGRAM_BOT_USERNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SHERWOOD_TG_LINK_SECRET", SECRET)
    # No network: the tg-card read and the WOOD price read are stubbed at the transport.
    monkeypatch.setattr(txbuilder, "_urllib_get_json", chain)
    monkeypatch.setattr(fund_tg, "_get_json", lambda url, timeout: {"data": {"prices": [{"symbol": "WOOD", "price_usd": 0.25}]}})
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
    factory(app, MagicMock())
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
                                 "telegram_chat_id": str(CHAT), "host_telegram_id": str(HOST_TG)})
    assert ctx.call_tool("fund_claim_topic", {"topic": "NVDA"})["ok"]


async def test_full_loop_in_memory(tmp_path, monkeypatch):
    chain = ChainStub()
    ctx = boot(tmp_path, monkeypatch, chain)
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

    # Host approves before the wallet is linked: refused with the reason.
    q = await tap(rt, bot, f"shfund:approve_member:{ALICE}", HOST_TG)
    assert "linked a wallet" in q.answer.await_args.kwargs["text"]
    # /tg/join links the wallet (the app lane's job); then the host's approve goes through
    # and hands the host a signed /tg/sign link for approveDepositor.
    svc.request_membership("alpha-fund", str(ALICE), address=MEMBER_ADDR, username="alice")
    n_before = bot.send_message.await_count
    q = await tap(rt, bot, f"shfund:approve_member:{ALICE}", HOST_TG, username="host")
    await settle_tasks()
    assert svc.load("alpha-fund").member(str(ALICE)).approved
    signed = [c for c in bot.send_message.await_args_list[n_before:] if buttons(c) and "/tg/sign" in buttons(c)[0].get("url", "")]
    action, body = verify_sign_url(buttons(signed[0])[0]["url"])
    assert action == "approve-depositor" and body == {"chainId": 4663, "vault": VAULT, "depositor": MEMBER_ADDR}
    assert any("Member #1" in t for t in sent_texts(bot))  # the member card
    # A stranger cannot approve.
    q = await tap(rt, bot, f"shfund:approve_member:{BOB}", BOB)
    assert "Only the fund host" in q.answer.await_args.kwargs["text"]

    # -- a pitch, then 🔥 x3 -> DraftRequested -> the topic is claimed for the agent
    pitch = IncomingMessage(chat_id=CHAT, message_id=41, user_id=ALICE, username="alice",
                            text="@sherwoodagentbot long $NVDA and $AAPL into earnings", mentions_bot=True)
    rt.note_person(CHAT, SimpleNamespace(tg_id=ALICE, username="alice", first_name=None, last_name=None, handle="@alice"))
    await rt.flow.on_message(pitch)
    for user in (ALICE, BOB, CAROL):
        await rt.flow.on_reaction(ReactionChange(chat_id=CHAT, message_id=41, user_id=user, new=("🔥",)))
    await settle_tasks()
    claims = ctx.call_tool("fund_status")["pitch_claims"]
    assert [(c["agent"], c["topic"], c["idea_by"]) for c in claims] == [("alpha", "NVDA AAPL pitch", str(ALICE))]

    # The agent's next turn sees both the pitch context and the draft request, through ONE hook.
    (hook,) = ctx.hooks["pre_llm_call"]
    ctx_out = await hook(platform="telegram", user_message="@sherwoodagentbot long $NVDA and $AAPL into earnings")
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
    s = ctx.call_tool("fund_record_settle", {"proposal_id": "9", "pnl": "8.2", "pnl_bps": 820, "tx_hash": "0x" + "cd" * 32})
    assert s["ok"] and "14-day challenge window" in s["post"]
    await settle_tasks(40)
    receipt = next(t for t in sent_texts(bot) if "settled</b>" in t)
    assert "<b>+8.2%</b> on NVDA/AAPL" in receipt and "idea by @alice" in receipt and "backed by 1" in receipt
    assert "14-day challenge window" in receipt
    assert "SETTLED" in bot.edit_message_text.await_args_list[-1].kwargs["text"]
    assert svc.load("alpha-fund").fund.cooldown_until is not None
    blocked = ctx.call_tool("fund_claim_topic", {"topic": "TSLA"})
    assert blocked["blocked"] and blocked["code"] == "cooldown"
    rows = await rt.gateway.leaderboard(CHAT)
    assert [(r.tg_id, r.pnl_bps, r.ideas, r.wins) for r in rows] == [(ALICE, 820, 1, 1)]


async def test_cards_are_a_noop_until_the_adapter_connects(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, ChainStub())
    ctx.call_tool("fund_setup", {"fund": "alpha-fund", "vault": VAULT, "governor": GOVERNOR,
                                 "telegram_chat_id": str(CHAT), "host_telegram_id": str(HOST_TG)})
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
