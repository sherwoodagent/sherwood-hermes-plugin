"""Defects from the offline smoke run of the Telegram fund flow at v0.8.4.

The fund in that run: a USDG vault on Robinhood Chain 4663, `proposal_signer: agent`,
ceilings 40 USDG max capital and 1000 bps max drawdown, governor voting 1800 s, veto
at 20% Against. Each section names the smoke step it reproduces.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sherwood_monitor import session
from sherwood_monitor.fund.guard import make_fund_guard
from sherwood_monitor.fund.models import Agent, Leg, ProposalRecord
from sherwood_monitor.fund.runtime import FundRuntime
from sherwood_monitor.fund.service import FundEvent, FundService
from sherwood_monitor.fund.settings import FundSettings
from sherwood_monitor.fund.signed_cli import propose_portfolio_command
from sherwood_monitor.fund.store import LocalJsonBackend
from sherwood_monitor.hooks import make_pre_tool_call_hook
from tests.fund_helpers import Clock, fund, host_agent

USDG_40 = "40000000"  # 40 USDG in base units (6 decimals)


def smoke_fund(**kw):
    """The smoke run's fund: 40 USDG / 1000 bps ceilings, 30 min voting."""
    base = dict(max_capital=USDG_40, max_drawdown_bps=1000, voting_period=1800)
    base.update(kw)
    return fund(**base)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(tmp_path: Path, clock):
    s = FundService(LocalJsonBackend(tmp_path), clock=clock, proposal_signer="agent")
    s.create_fund(smoke_fund(), host_agent())
    return s


@pytest.fixture
def events(svc):
    seen: list[FundEvent] = []
    svc.subscribe(seen.append)
    return seen


def portfolio_legs():
    return [Leg(summary="buy TSLA", symbol="TSLA", weight_bps=5000), Leg(summary="buy NVDA", symbol="NVDA", weight_bps=5000)]


def draft(svc, legs=None):
    svc.claim_topic("alpha-fund", "alpha", "TSLA NVDA pitch")
    return svc.draft_proposal(
        "alpha-fund", "alpha", topic="TSLA NVDA pitch", thesis="TSLA and NVDA into the next session",
        legs=legs or portfolio_legs(), max_capital=40_000_000, max_drawdown_bps=1000, strategy_duration=21600,
    ).proposal


# == 1. voting opens on the card ==========================================================
def test_recording_proposed_opens_voting_at_once(svc, events, clock):
    """Smoke 9a: `fund_set_proposal_status proposed` left the record at proposed, so the card never
    showed the veto meter or ❌ Object. The governor's window opens in the propose block."""
    draft(svc)
    n = len(events)
    rec = svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=9001, tx_hash="0x" + "ab" * 32)
    assert rec.status == "voting"
    assert rec.voting_ends_at == clock() + 1800 and rec.onchain_id == 9001
    status_events = [e for e in events[n:] if e.kind == "status"]
    assert len(status_events) == 1 and status_events[0].old_status == "draft"  # one event: one card post
    # A retried record, or the old two-step (proposed then voting), changes nothing.
    assert svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=9001).status == "voting"
    assert svc.set_proposal_status("alpha-fund", "alpha", "p-1", "voting").status == "voting"
    assert [e.kind for e in events[n:]].count("status") == 1


def test_chain_pending_moves_a_proposed_record_to_voting(svc, events):
    """Smoke 9F1: the tg-card read says `Pending` (the governor's ProposalState while LP voting is
    open, sherwood app funds/_lib/tg-card.ts) and the record stayed at proposed."""
    draft(svc)

    def as_proposed(state):  # a record from before the fix: recorded proposed, never moved on
        record = state.proposal("p-1")
        record.status, record.onchain_id = "proposed", 1
        return record

    svc.backend.update("alpha-fund", as_proposed)
    card = {"success": True, "data": {"proposals": {"open": [
        {"id": "1", "state": "Pending", "againstBps": 0, "voteEnd": "1800001800", "reviewEnd": "1800003600"},
    ], "recent": []}}}
    assert svc.apply_chain_card("alpha-fund", card) == 1
    assert svc.load("alpha-fund").proposal("1").status == "voting"
    assert events[-1].kind == "status" and events[-1].data == {"source": "chain"}
    # A stale Pending never drags a later status back.
    svc.set_proposal_status("alpha-fund", "alpha", "1", "review")
    svc.apply_chain_card("alpha-fund", card)
    assert svc.load("alpha-fund").proposal("1").status == "review"


# == 2. the terminal propose guard ========================================================
HOST_TG = "900"


@pytest.fixture
def as_caller(monkeypatch):
    def set_caller(platform: str = "", user: str = "", chat: str = "") -> None:
        env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_USER_ID": user, "HERMES_SESSION_CHAT_ID": chat}
        monkeypatch.setattr(session, "_session_env", lambda name: env.get(name, ""))

    set_caller()  # the operator's own CLI / hermes -z turn
    return set_caller


def guard_rt(**fund_kw):
    be = LocalJsonBackend(tempfile.mkdtemp())
    s = FundService(be, clock=Clock(), proposal_signer="agent")
    s.create_fund(smoke_fund(**fund_kw), host_agent())
    return FundRuntime(s, FundSettings(agent_name="alpha", owner_telegram_id=HOST_TG, proposal_signer="agent"), None)


def shipped_command(rt, **record_kw):
    """The exact command the plugin ships on the card for the smoke draft (40 USDG, 1000 bps, 6h)."""
    st = rt.service.load("alpha-fund")
    base = dict(id="p-1", proposer_agent="alpha", thesis="TSLA and NVDA into the next session",
                topic="TSLA NVDA pitch", legs=portfolio_legs(), max_capital=USDG_40, max_drawdown_bps=1000,
                strategy_duration=21600)
    base.update(record_kw)
    return propose_portfolio_command(st.fund, ProposalRecord(**base))


def run_hook(rt, command, state=None):
    fetched = []

    async def fetch(sub):
        fetched.append(sub)
        return state

    hook = make_pre_tool_call_hook(state_fetcher=fetch, fund_guard=make_fund_guard(rt))
    return asyncio.run(hook(tool_name="terminal", args={"command": command})), fetched


def test_the_plugins_own_40_usdg_command_passes_without_the_general_rule(as_caller):
    """Smoke 10b: with AUM 100 the general 25%-of-AUM rule blocked the fund's own 40 USDG ceiling."""
    rt = guard_rt()
    command = shipped_command(rt)
    assert "--amount 40 " in command and "--max-drawdown-bps 1000 " in command
    out, fetched = run_hook(rt, command, state={"vault_aum_usd": 100, "current_exposure_usd": 0, "allowed_protocols": []})
    assert out is None and fetched == []  # no chain read, no 40%-of-AUM block
    from sherwood_monitor.propose_cmd import FUND_RULES_APPLIED

    assert make_fund_guard(rt)("terminal", {"command": command}) is FUND_RULES_APPLIED


@pytest.mark.parametrize("change, reason", [
    (("--amount 40 ", "--amount 41 "), "41 USDG is above the fund ceiling 40 USDG"),
    (("--amount 40 ", "--amount 1000 "), "1000 USDG is above the fund ceiling 40 USDG"),
    (("--amount 40 ", "--amount=40.5 "), "40.5 USDG is above the fund ceiling 40 USDG"),
    (("--max-drawdown-bps 1000 ", "--max-drawdown-bps 1500 "), "max drawdown 1500 bps is above the fund ceiling 1000 bps"),
    (("--max-drawdown-bps 1000 ", ""), "no --max-drawdown-bps: the CLI default 2000 bps is above the fund ceiling 1000 bps"),
    (("--amount 40 ", ""), "must name its capital with --amount"),
    (("--amount 40 ", "--amount forty "), "cannot read --amount 'forty'"),
])
def test_a_fund_vault_propose_above_a_ceiling_is_blocked(as_caller, change, reason):
    """Smoke 10a: `--amount 1000` on the fund vault was allowed (size read only from --size-usd and
    `vault info --json` rejected by CLI 0.103.0, so the check failed open)."""
    rt = guard_rt()
    command = shipped_command(rt).replace(*change)
    out, fetched = run_hook(rt, command)
    assert out is not None and out["action"] == "block", command
    assert reason in out["message"] and "alpha-fund" in out["message"]
    assert fetched == []


def test_vault_case_and_attached_flags_still_match_the_fund(as_caller):
    rt = guard_rt()
    vault = rt.service.load("alpha-fund").fund.vault
    command = shipped_command(rt).replace(f"--vault {vault}", f"--vault={vault.upper().replace('0X', '0x')}")
    out, _ = run_hook(rt, command.replace("--amount 40 ", "--amount 41 "))
    assert out["action"] == "block" and "41 USDG" in out["message"]
    # Chained after something else in one terminal call: still read.
    out, _ = run_hook(rt, "cd /tmp && " + shipped_command(rt).replace("--amount 40 ", "--amount 41 "))
    assert out["action"] == "block"


def test_proposal_create_on_a_fund_vault_reads_max_capital(as_caller):
    rt = guard_rt()
    vault = rt.service.load("alpha-fund").fund.vault
    base = (f"sherwood proposal create --vault {vault} --name x --description y --duration 6h "
            "--execute-calls e.json --settle-calls s.json --strategy 0x" + "88" * 20)
    assert run_hook(rt, base + " --max-capital 40 --max-drawdown-bps 1000")[0] is None
    out, _ = run_hook(rt, base + " --max-capital 41 --max-drawdown-bps 1000")
    assert "41 USDG is above the fund ceiling 40 USDG" in out["message"]


def test_generic_call_propose_for_a_fund_vault_gets_the_same_ceilings(as_caller, tmp_path):
    """The Generic Call command the plugin ships names its plan file with --calls and the same
    --amount / --max-drawdown-bps: those are checked like any template. The plan itself was
    validated at draft time; in a chat turn the plan path trips the fund-state block first."""
    from sherwood_monitor.fund.generic_call import propose_generic_call_command

    rt = guard_rt()
    st = rt.service.load("alpha-fund")
    record = ProposalRecord(id="p-1", proposer_agent="alpha", thesis="route the pull", topic="custom route",
                            max_capital=USDG_40, max_drawdown_bps=1000, strategy_duration=21600,
                            generic_call={"executeCalls": [], "settleCalls": [], "settlePatches": [], "sweepTokens": []})
    plan = tmp_path / "plugins" / "sherwood-monitor" / "funds" / "alpha-fund" / "generic-call" / "p-1.json"
    command = propose_generic_call_command(st.fund, record, str(plan))
    assert "generic-call" in command and "--calls" in command
    assert run_hook(rt, command)[0] is None
    out, _ = run_hook(rt, command.replace("--amount 40 ", "--amount 41 "))
    assert out["action"] == "block" and "41 USDG is above the fund ceiling 40 USDG" in out["message"]
    as_caller("telegram", HOST_TG, "-100500")  # a chat turn
    out, _ = run_hook(rt, command)
    assert out["action"] == "block" and "never run from chat" in out["message"]


def test_a_propose_without_vault_on_a_fund_host_is_blocked(as_caller):
    """The CLI would use its configured vault, which on a fund host is the fund's: name it."""
    rt = guard_rt()
    out, _ = run_hook(rt, "sherwood strategy propose portfolio --tokens TSLA --weights 10000 --amount 41")
    assert out["action"] == "block" and "--vault" in out["message"]


def test_other_vaults_keep_the_advisory_check_and_read_amount(as_caller, caplog):
    """A vault that is no fund here: today's advisory rule, now reading --amount; fail open
    (with the reason logged) when the CLI gives no machine-readable state."""
    rt = guard_rt()
    other = "0x" + "99" * 20
    command = f"sherwood strategy propose portfolio --vault {other} --tokens TSLA --weights 10000 --amount 1000"
    out, fetched = run_hook(rt, command, state={"vault_aum_usd": 100, "current_exposure_usd": 0, "allowed_protocols": []})
    assert fetched == [other] and out["action"] == "block" and "position sizing" in out["message"]
    out, _ = run_hook(rt, command, state=None)
    assert out is None
    assert any("advisory and fails open" in r.getMessage() for r in caplog.records)
    # No fund on the host at all: unchanged behaviour, even without --vault.
    hook = make_pre_tool_call_hook(state_fetcher=MagicMock(), fund_guard=None)
    assert asyncio.run(hook(tool_name="bash", args={"command": "echo 'sherwood strategy propose x'"})) is None


def test_the_risk_hook_parses_amount():
    from sherwood_monitor.hooks import _parse_propose_command

    vault = "0x" + "11" * 20
    assert _parse_propose_command(f"sherwood strategy propose portfolio --vault {vault} --amount 40") == (vault, 40.0, "")
    assert _parse_propose_command(f"sherwood proposal create --vault {vault} --max-capital 12.5")[1] == 12.5
    assert _parse_propose_command("sherwood proposal create alpha --size-usd 5000") == ("alpha", 5000.0, "")


# -- the group-chat side of fix 1 (and the cards of fix 3) run through the real plugin ----------------
def boot_group(tmp_path, monkeypatch, *, max_capital="40", max_drawdown_bps=1000):
    """The plugin wired to a fake bot (tests/test_fund_telegram_integration.py), with a chain read that
    says nothing about the proposal: only what the plugin records moves the card."""
    pytest.importorskip("telegram")
    from tests import test_fund_telegram_integration as it

    class SilentChain(it.ChainStub):
        def __call__(self, url, timeout):
            self.calls += 1
            return 200, {"success": True, "data": {"proposals": {"open": [], "recent": []}}}

    ctx = it.boot(tmp_path, monkeypatch, SilentChain())
    assert ctx.call_tool("fund_setup", {
        "fund": "alpha-fund", "vault": it.VAULT, "governor": it.GOVERNOR, "telegram_chat_id": str(it.CHAT),
        "host_telegram_id": str(it.HOST_TG), "host_address": it.HOST_ADDR, "max_capital": max_capital,
        "max_drawdown_bps": max_drawdown_bps, "voting_period": 1800, "agent_fee_bps": 500,
    })["ok"]
    rt, bridge, bot, app = it.connect(ctx)
    return it, ctx, rt, bridge, bot


SMOKE_DRAFT = {
    "topic": "TSLA NVDA pitch", "thesis": "TSLA and NVDA into the next session",
    "legs": [{"summary": "buy TSLA", "symbol": "TSLA", "weight_bps": 5000},
             {"summary": "buy NVDA", "symbol": "NVDA", "weight_bps": 5000}],
    "max_capital": "40", "max_drawdown_bps": 1000, "strategy_duration_seconds": 21600,
}


async def test_object_after_a_recorded_propose_hands_out_the_vote_link(tmp_path, monkeypatch):
    """Smoke 9F2: after `fund_set_proposal_status proposed` the card had no ❌ Object, and a tap
    answered "Voting is closed for this proposal." while the vote was open on chain."""
    it, ctx, rt, bridge, bot = boot_group(tmp_path, monkeypatch)
    assert ctx.call_tool("fund_claim_topic", {"topic": "TSLA NVDA pitch"})["ok"]
    assert ctx.call_tool("fund_draft_proposal", dict(SMOKE_DRAFT))["ok"]
    await it.tap(rt, bot, "shfund:shipit:p-1", it.HOST_TG)
    out = ctx.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "proposed", "onchain_id": 9,
                                                     "tx_hash": "0x" + "ab" * 32})
    assert out["ok"] and out["proposal"]["status"] == "voting"
    await it.settle_tasks(40)
    cards = [c for c in bot.send_message.await_args_list if "<b>Proposal #9</b>" in c.kwargs.get("text", "")]
    assert len(cards) == 1
    text = cards[0].kwargs["text"]
    assert "🗳 VOTING" in text and "Against 0.0%" in text and "ends in 30m" in text
    assert "shfund:vote:9" in {b.get("callback_data") for b in it.buttons(cards[0])}
    n_before = bot.send_message.await_count
    q = await it.tap(rt, bot, "shfund:vote:9", it.ALICE)
    answered = q.answer.await_args.kwargs.get("text") if q.answer.await_args else None
    assert answered != "Voting is closed for this proposal."
    vote_url = it.buttons(bot.send_message.await_args_list[n_before])[0]["url"]
    assert f"/tg/vote?proposal={it.GOVERNOR.lower()}:9&tg={it.ALICE}" in vote_url
