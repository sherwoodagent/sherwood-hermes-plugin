"""0.8.0 open-protocol mode on Telegram: open deposits and agent-signed proposals and registrations.

Card renderers and handlers against the fake gateway, then the whole loop through
the real `register()`, FundService and bridge (as in test_fund_telegram_integration)
with `proposal_signer: agent` and a fund created with open deposits.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

pytest.importorskip("telegram")

from sherwood_monitor.fund import applink  # noqa: E402
from sherwood_monitor.fund import telegram as fund_tg  # noqa: E402
from sherwood_monitor.fund import txbuilder  # noqa: E402
from sherwood_monitor.fund.models import Agent  # noqa: E402
from sherwood_monitor.telegram.cards import render_personal_link, render_shipped_command  # noqa: E402
from sherwood_monitor.telegram.flow import IncomingMessage, ReactionChange  # noqa: E402
from sherwood_monitor.telegram.handler import FundHandlers  # noqa: E402
from tests.fake_ctx import FakeCtx  # noqa: E402
from tests.fund_helpers import GOVERNOR, GUEST_ADDR, HOST_ADDR, TX  # noqa: E402
from tests.fund_helpers import VAULT as FUND_VAULT  # noqa: E402
from tests.test_fund_telegram_integration import (  # noqa: E402
    ALICE,
    API_TOKEN,
    BOB,
    CAROL,
    CHAT,
    DAVE,
    HOST_CFG,
    HOST_TG,
    SECRET,
    AppLinkStub,
    ChainStub,
    buttons,
    connect,
    sent_texts,
    settle_tasks,
)
from tests.test_fund_telegram_integration import tap as itap  # noqa: E402
from tests.test_tg_handler import make_runtime, start_update, tap  # noqa: E402
from tests.tg_fakes import CAROL as FAKE_CAROL, HOST, VAULT, make_proposal  # noqa: E402

JOIN_URL = "https://app.example/tg/join?fund=0x1&tg=1&ts=1&sig=a"
DEPOSIT_URL = "https://app.example/tg/deposit?fund=0x1&tg=1&ts=1&sig=b"


def all_urls(markup) -> list[str]:
    return [b["url"] for row in markup.to_dict()["inline_keyboard"] for b in row if b.get("url")]


# -- renderers ----------------------------------------------------------------------------------
def test_join_link_carries_the_deposit_link_only_when_deposits_are_open():
    text, markup = render_personal_link("join", JOIN_URL, "Robin Fund", deposit_url=DEPOSIT_URL)
    assert all_urls(markup) == [JOIN_URL, DEPOSIT_URL]
    assert "Deposits are open on this fund" in text and "expires in 15 minutes" in text and "24 hours" in text
    plain, plain_markup = render_personal_link("join", JOIN_URL, "Robin Fund")
    assert all_urls(plain_markup) == [JOIN_URL] and "Deposits are open" not in plain


def test_shipped_command_card_escapes_and_falls_back_when_too_long():
    text = render_shipped_command("<b>card</b>", "sherwood strategy propose portfolio --description 'a <b> & c'",
                                  shipped_by="@host", proposer="alpha")
    assert text.startswith("<b>card</b>\n\n<i>🚀 Shipped by @host. alpha proposes it from its own wallet")
    assert text.endswith("<pre>sherwood strategy propose portfolio --description 'a &lt;b&gt; &amp; c'</pre>")
    long = render_shipped_command("card", "x" * 5000, shipped_by="@host", proposer="alpha")
    assert "<pre>" not in long and "fund_pending_commands" in long and len(long) < 4000


# -- handlers (fake gateway) --------------------------------------------------------------------
async def test_join_on_an_open_fund_hands_out_the_join_and_deposit_links():
    rt, gw, bot = make_runtime()
    gw.fund = dataclasses.replace(gw.fund, deposits_open=True)
    await tap(rt, bot, f"shfund:join:{VAULT}", FAKE_CAROL)
    urls = all_urls(bot.send_message.await_args.kwargs["reply_markup"])
    assert urls[0].startswith(f"https://app.sherwood.sh/tg/join?fund={VAULT}&tg={FAKE_CAROL}&ts=")
    assert urls[1].startswith(f"https://app.sherwood.sh/tg/deposit?fund={VAULT}&tg={FAKE_CAROL}&ts=")
    assert len(urls) == 2


async def test_join_on_a_restricted_fund_hands_out_the_join_link_only():
    rt, gw, bot = make_runtime()
    await tap(rt, bot, f"shfund:join:{VAULT}", FAKE_CAROL)
    assert len(all_urls(bot.send_message.await_args.kwargs["reply_markup"])) == 1


async def test_start_join_link_in_dm_on_an_open_fund():
    rt, gw, bot = make_runtime()
    gw.fund = dataclasses.replace(gw.fund, deposits_open=True)
    upd, msg = start_update(f"/start sfj_{VAULT[2:]}", FAKE_CAROL)
    await FundHandlers(rt).cmd_start(upd, SimpleNamespace(bot=bot))
    urls = all_urls(msg.reply_html.await_args.kwargs["reply_markup"])
    assert [urlsplit(u).path for u in urls] == ["/tg/join", "/tg/deposit"]


async def test_ship_it_puts_the_command_on_the_draft_card():
    rt, gw, bot = make_runtime()
    command = f"sherwood strategy propose portfolio --vault {VAULT} --tokens NVDA --weights 10000"
    gw.proposals["d7"] = make_proposal(id="d7", status="draft", propose_command=command)
    q = await tap(rt, bot, "shfund:shipit:d7", HOST, username="host")
    kwargs = q.edit_message_text.await_args.kwargs
    assert kwargs["text"].endswith(f"<pre>{command}</pre>") and "Shipped by @host" in kwargs["text"]
    assert kwargs["reply_markup"] is None and kwargs["parse_mode"] == "HTML"
    q.edit_message_reply_markup.assert_not_awaited()


async def test_ship_it_without_a_command_only_clears_the_buttons():
    rt, gw, bot = make_runtime()
    gw.proposals["d7"] = make_proposal(id="d7", status="draft")
    q = await tap(rt, bot, "shfund:shipit:d7", HOST)
    q.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    q.edit_message_text.assert_not_awaited()


# -- the loop through register() ------------------------------------------------------------------
class OpenChainStub(ChainStub):
    """tg-card with the `fund` section (vault info + launch rules: depositsOpen)."""

    def __init__(self) -> None:
        super().__init__()
        self.deposits_open = True

    def __call__(self, url: str, timeout: float):
        status, payload = super().__call__(url, timeout)
        payload["data"]["fund"] = {"openDeposits": True, "depositsRestricted": not self.deposits_open,
                                   "depositsOpen": self.deposits_open, "ownerOnlyProposals": False}
        return status, payload


def boot(tmp_path: Path, monkeypatch, chain: ChainStub, *, signer: str) -> FakeCtx:
    for key in ("SHERWOOD_FUND_STATE_DIR", "TX_BUILDER_URL", "APP_BASE_URL", "TELEGRAM_BOT_USERNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SHERWOOD_TG_LINK_SECRET", SECRET)
    monkeypatch.setenv("SHERWOOD_TG_API_TOKEN", API_TOKEN)
    monkeypatch.setattr(txbuilder, "_urllib_get_json", chain)
    monkeypatch.setattr(fund_tg, "_get_json", lambda url, timeout: {"data": {"prices": []}})
    monkeypatch.setattr(applink, "_http_get_json", AppLinkStub())
    cfg_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    fund_cfg = {**HOST_CFG, "tx_builder_url": "https://api.example/api/v1", "proposal_signer": signer}
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"funds": [], "fund": fund_cfg}))
    ctx = FakeCtx()
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(side_effect=FileNotFoundError)):
        from sherwood_monitor import register

        register(ctx)
    return ctx


def setup_fund(ctx: FakeCtx, **extra) -> None:
    out = ctx.call_tool("fund_setup", {
        "fund": "alpha-fund", "vault": FUND_VAULT, "governor": GOVERNOR, "telegram_chat_id": str(CHAT),
        "host_telegram_id": str(HOST_TG), "host_address": HOST_ADDR, "max_capital": "1000",
        "max_drawdown_bps": 2500, "voting_period": 1800, "agent_fee_bps": 500, **extra,
    })
    assert out["ok"] and out["created"], out


async def test_open_protocol_loop_in_memory(tmp_path, monkeypatch):
    chain = OpenChainStub()
    ctx = boot(tmp_path, monkeypatch, chain, signer="agent")
    setup_fund(ctx, open_deposits=True)
    rt, bridge, bot, app = connect(ctx)
    svc = bridge.service

    # -- 🎟 Join on an open fund: no Approve card for the host; the member gets join + deposit links
    n_before = bot.send_message.await_count
    await itap(rt, bot, f"shfund:join:{FUND_VAULT.lower()}", ALICE, username="alice")
    await settle_tasks()
    texts = sent_texts(bot)[n_before:]
    assert not any("wants to join" in t for t in texts)
    personal = next(c for c in bot.send_message.await_args_list[n_before:] if "ephemeral_message_parameters" in c.kwargs.get("api_kwargs", {}))
    urls = [b["url"] for b in buttons(personal)]
    assert [urlsplit(u).path for u in urls] == ["/tg/join", "/tg/deposit"]
    assert all(parse_qs(urlsplit(u).query)["tg"] == [str(ALICE)] for u in urls)
    alice = svc.load("alpha-fund").member(str(ALICE))
    assert alice.approved and alice.member_no == 1
    assert any("🎟 <b>Member</b>" in t for t in texts)  # admitted: the member card, no host step
    # A stale ✅ Approve member button: nothing to approve, no signing link.
    q = await itap(rt, bot, f"shfund:approve_member:{BOB}", HOST_TG)
    assert "Deposits are open on this fund" in q.answer.await_args.kwargs["text"]
    assert bridge.host_action_url("alpha-fund", "approve_member", str(ALICE), HOST_TG) is None

    # -- pitch, 3 🔥, the agent drafts: the draft card has no Co-sign button under the agent signer
    await rt.flow.on_message(IncomingMessage(chat_id=CHAT, message_id=41, user_id=ALICE, username="alice",
                                             text="@sherwoodagentbot long $NVDA and $AAPL into earnings", mentions_bot=True))
    for user in (BOB, CAROL, DAVE):
        await rt.flow.on_reaction(ReactionChange(chat_id=CHAT, message_id=41, user_id=user, new=("🔥",)))
    await settle_tasks()
    d = ctx.call_tool("fund_draft_proposal", {
        "topic": "NVDA AAPL pitch", "thesis": "AI capex keeps beating",
        "legs": [{"summary": "buy NVDA", "symbol": "NVDA", "weight_bps": 6000},
                 {"summary": "buy AAPL", "symbol": "AAPL", "weight_bps": 4000}],
        "max_capital": "100", "max_drawdown_bps": 2000, "strategy_duration_seconds": 21600,
    })
    assert d["ok"], d
    await settle_tasks()
    draft_call = next(c for c in bot.send_message.await_args_list if "DRAFT" in c.kwargs.get("text", ""))
    assert [b["callback_data"] for b in buttons(draft_call)] == ["shfund:shipit:p-1"]

    # -- 🚀 Ship it: the record, the event and the card carry the exact signed CLI command
    expected = (
        f"sherwood strategy propose portfolio --vault {FUND_VAULT} --tokens NVDA,AAPL --weights 6000,4000 "
        "--amount 100 --duration 6h --max-drawdown-bps 2000 --name 'alpha-fund: NVDA AAPL pitch' "
        "--description 'AI capex keeps beating'"
    )
    shipped_events = []
    svc.subscribe(lambda ev: shipped_events.append(ev) if ev.kind == "shipped" else None)
    q = await itap(rt, bot, "shfund:shipit:p-1", HOST_TG, username="host")
    assert "Sherwood CLI" in q.answer.await_args.kwargs["text"]
    assert q.edit_message_text.await_args.kwargs["text"].endswith(f"<pre>{expected}</pre>")
    assert svc.load("alpha-fund").proposal("p-1").propose_command == expected
    assert shipped_events[-1].data == {"propose_command": expected}
    # The host's next chat turn hears it was shipped and where the command runs (not in this chat).
    (hook,) = ctx.hooks["pre_llm_call"]
    out = await hook(platform="telegram", chat_id=str(CHAT), sender_id=str(HOST_TG), user_message="status?")
    assert "<sherwood-fund-shipped>" in out["context"] and "fund_pending_commands" in out["context"]
    assert "has no terminal" in out["context"] and "prepare/propose-portfolio" not in out["context"]

    # -- the one-shot fetches the command, runs it outside the plugin, records what landed
    (entry,) = ctx.call_tool("fund_pending_commands")["proposals"]
    assert entry["command"] == expected
    ctx.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "proposed", "onchain_id": 9, "tx_hash": TX})
    ctx.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "voting"})
    await settle_tasks(40)
    cards = [c for c in bot.send_message.await_args_list if "<b>Proposal #9</b>" in c.kwargs.get("text", "")]
    assert len(cards) == 1
    assert ctx.call_tool("fund_pending_commands")["proposals"] == []
    assert svc.load("alpha-fund").fund.open_deposits is True  # the chain read agreed

    # -- the chain says deposits are restricted after all: the chain beats the record
    chain.deposits_open = False
    bridge.chain_sync._next.clear()
    await rt.refresh_proposal_card(CHAT, "9")
    await settle_tasks(40)
    assert svc.load("alpha-fund").fund.open_deposits is False
    assert ctx.call_tool("fund_status")["deposits_open"] is False


async def test_agent_signer_registers_guest_agents_with_the_cli_not_a_signing_link(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, ChainStub(), signer="agent")
    setup_fund(ctx)
    rt, bridge, bot, app = connect(ctx)
    bridge.service.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    toast = bridge.approve_agent("alpha-fund", "beta", HOST_TG)
    assert toast == "✅ beta approved. The host agent registers it with the Sherwood CLI: /fund-approve beta shows the command."
    assert bridge.host_action_url("alpha-fund", "approve_agent", "beta", HOST_TG) is None


async def test_host_signer_and_restricted_deposits_are_unchanged(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, ChainStub(), signer="host")
    setup_fund(ctx)
    rt, bridge, bot, app = connect(ctx)
    # Join: the host's Approve card, and the member's join link alone.
    n_before = bot.send_message.await_count
    await itap(rt, bot, f"shfund:join:{FUND_VAULT.lower()}", ALICE, username="alice")
    assert any("wants to join" in t for t in sent_texts(bot)[n_before:])
    personal = bot.send_message.await_args_list[-1]
    assert [urlsplit(b["url"]).path for b in buttons(personal)] == ["/tg/join"]
    assert not bridge.service.load("alpha-fund").member(str(ALICE)).approved
    # Agents: approve hands the host a /tg/sign link for registerAgent.
    bridge.service.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    assert bridge.approve_agent("alpha-fund", "beta", HOST_TG) == "✅ beta approved. Sign registerAgent in your browser."
    url = bridge.host_action_url("alpha-fund", "approve_agent", "beta", HOST_TG)
    assert url and urlsplit(url).path == "/tg/sign" and parse_qs(urlsplit(url).query)["action"] == ["register-agent"]
    # Ship it: no command anywhere; the toast points at the co-sign button.
    ctx.call_tool("fund_claim_topic", {"topic": "NVDA"})
    assert ctx.call_tool("fund_draft_proposal", {
        "topic": "NVDA", "thesis": "t", "legs": [{"summary": "buy NVDA", "symbol": "NVDA", "weight_bps": 10000}],
        "max_capital": "10", "max_drawdown_bps": 100, "strategy_duration_seconds": 3600,
    })["ok"]
    await settle_tasks()
    q = await itap(rt, bot, "shfund:shipit:p-1", HOST_TG)
    assert q.answer.await_args.kwargs["text"] == "🚀 Shipped #p-1. Tap ✍️ Co-sign to sign the proposal in your browser."
    q.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    q.edit_message_text.assert_not_awaited()
    assert bridge.service.load("alpha-fund").proposal("p-1").propose_command == ""
