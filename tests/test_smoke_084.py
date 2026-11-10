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
from sherwood_monitor.fund.models import Leg, ProposalRecord
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
    base = {"max_capital": USDG_40, "max_drawdown_bps": 1000, "voting_period": 1800}
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
    base = {"id": "p-1", "proposer_agent": "alpha", "thesis": "TSLA and NVDA into the next session",
                "topic": "TSLA NVDA pitch", "legs": portfolio_legs(), "max_capital": USDG_40, "max_drawdown_bps": 1000,
                "strategy_duration": 21600}
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
    rt, bridge, bot, _app = it.connect(ctx)
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
    it, ctx, rt, _bridge, bot = boot_group(tmp_path, monkeypatch)
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
    # 3a: no bond quote under agent signing -> no "bond 0 WOOD" line on the proposal card.
    assert "bond 0 WOOD" not in text and "skin in the game" not in text


# == 3. card wording ======================================================================
def _view(**kw):
    from sherwood_monitor.telegram.types import Leg as LegView
    from sherwood_monitor.telegram.types import ProposalView

    base = {"id": "p-1", "governor": "0x" + "22" * 20, "proposer": "alpha", "thesis": "TSLA and NVDA",
                "legs": (LegView("TSLA", 5000), LegView("NVDA", 5000)), "max_capital": 40.0, "max_drawdown_bps": 1000,
                "capital_symbol": "USDG"}
    base.update(kw)
    return ProposalView(**base)


def test_no_bond_line_without_a_bond_quote():
    """Smoke 5e / 9a: "bond 0 WOOD · agent's skin in the game" on the draft and proposal cards."""
    pytest.importorskip("telegram")
    from sherwood_monitor.telegram.cards import render_draft_card, render_proposal_card

    for render in (render_draft_card, render_proposal_card):
        text, _ = render(_view(bond_wood=0.0))
        assert "WOOD" not in text and "skin in the game" not in text, render.__name__
        text, _ = render(_view(bond_wood=12.4))
        assert "bond 12.4 WOOD · agent's skin in the game" in text, render.__name__


PORTFOLIO_CMD = (
    "sherwood strategy propose portfolio --vault 0x2fD6056d9326e07Eec9Ba4499078C328E4F38b4A --tokens TSLA,NVDA "
    "--weights 5000,5000 --amount 40 --duration 6h --max-drawdown-bps 1000 '--name=hermes-fund: TSLA NVDA pitch' "
    "'--description=TSLA and NVDA into the next session'"
)
GENERIC_CMD = (
    "sherwood strategy propose generic-call --vault 0x2fD6056d9326e07Eec9Ba4499078C328E4F38b4A --calls "
    "/Users/operator/.hermes/plugins/sherwood-monitor/funds/hermes-fund/generic-call/p-1.json --amount 40 "
    "--duration 6h --max-drawdown-bps 1000 '--name=hermes-fund: custom route' '--description=route the pull'"
)


def test_shipped_card_drops_the_ship_it_hint():
    """Smoke 5f: the shipped card kept "Host: 🚀 Ship it sends this to a vote." above "🚀 Shipped by"."""
    pytest.importorskip("telegram")
    from sherwood_monitor.telegram.cards import (
        render_draft_card,
        render_shipped_command,
    )

    draft_html, _ = render_draft_card(_view())
    assert "Ship it sends this to a vote" in draft_html
    for base in (draft_html, draft_html.replace("<i>Host:", "<em>Host:").replace("vote.</i>", "vote.</em>")):
        text = render_shipped_command(base, PORTFOLIO_CMD, shipped_by="@host", proposer="alpha")
        assert "Ship it sends this to a vote" not in text
        assert "🚀 Shipped by @host" in text and "--amount 40" in text


def test_shipped_generic_call_card_shows_only_the_plan_file_name():
    """Smoke 7c: the group saw the operator's absolute plan path (and home directory)."""
    pytest.importorskip("telegram")
    from sherwood_monitor.telegram.cards import group_command, render_shipped_command

    text = render_shipped_command("📝 DRAFT", GENERIC_CMD, shipped_by="@host", proposer="alpha")
    assert "/Users/operator" not in text and ".hermes" not in text
    assert "--calls p-1.json --amount 40" in text
    assert "fund_pending_commands has the full command" in text
    assert group_command(GENERIC_CMD.replace("--calls /", "--calls=/"))[0].count("--calls=p-1.json") == 1
    assert group_command(PORTFOLIO_CMD) == (PORTFOLIO_CMD, False)  # nothing to shorten: verbatim


def test_handles_not_raw_ids_where_the_record_has_one(svc):
    """Smoke 9h/9j/5b: "Idea by member 451850101", "1. member 451850101: ..." and "the group backed
    member 451850101's pitch" for a member whose handle is on record."""
    from sherwood_monitor.fund.cards import render_receipt
    from sherwood_monitor.fund.commands import make_fund_commands
    from sherwood_monitor.fund.models import Member

    def add_member(state):
        state.members.append(Member(telegram_id="451850101", approved=True, username="imthatcarlos"))

    svc.backend.update("alpha-fund", add_member)
    svc.claim_topic("alpha-fund", "alpha", "TSLA NVDA pitch")
    svc.draft_proposal("alpha-fund", "alpha", topic="TSLA NVDA pitch", thesis="t", legs=portfolio_legs(),
                       max_capital=40_000_000, max_drawdown_bps=1000, strategy_duration=21600, idea_by="451850101")
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=9001)
    for status in ("review", "approved", "executed"):
        svc.set_proposal_status("alpha-fund", "alpha", "p-1", status)
    receipt = svc.record_settle("alpha-fund", "alpha", "p-1", pnl=1_200_000, pnl_bps=300, tx_hash="0x" + "cd" * 32)
    post = render_receipt(svc.load("alpha-fund"), receipt)
    assert "Idea by @imthatcarlos" in post and "451850101" not in post
    rt = FundRuntime(svc, FundSettings(agent_name="alpha", owner_telegram_id="900"), None)
    board = make_fund_commands(rt)["leaderboard"]("alpha-fund")
    assert "1. @imthatcarlos: +3.0%" in board and "451850101" not in board
    # No handle on record: the id is all there is.
    receipt.idea_by = "777"
    assert "Idea by member 777" in render_receipt(svc.load("alpha-fund"), receipt)


async def test_draft_request_context_names_the_member_by_handle(tmp_path, monkeypatch):
    it, _ctx, _rt, bridge, _bot = boot_group(tmp_path, monkeypatch)
    svc = bridge.service
    svc.request_membership("alpha-fund", str(it.ALICE), username="alice")
    svc.request_draft("alpha-fund", "alpha", idea_by=str(it.ALICE), pitch="long $NVDA", chat_id=str(it.CHAT))
    (block,) = bridge.take_chat_context(str(it.CHAT), str(it.ALICE))
    assert "the group backed @alice's pitch" in block and f"member {it.ALICE}" not in block


def test_member_card_has_no_share_line_when_the_share_is_unknown():
    """Smoke 3a: "0.0% of shares" on the member card when the share was simply unknown."""
    pytest.importorskip("telegram")
    from sherwood_monitor.fund.models import Member
    from sherwood_monitor.fund.telegram import _member_view
    from sherwood_monitor.telegram.cards import render_member_card
    from sherwood_monitor.telegram.types import FundView

    fv = FundView(name="hermes-fund", vault="0x" + "11" * 20, governor="0x" + "22" * 20)
    view = _member_view(Member(telegram_id="451850101", approved=True, username="imthatcarlos", joined_at=1794100000))
    text, _ = render_member_card(view, fund=fv)
    assert "of shares" not in text and "0.0%" not in text
    assert text.splitlines()[1].startswith("joined ")


# == 4. weights that do not make 100% are refused at draft time ===========================
NINETY = [{"summary": "buy TSLA", "symbol": "TSLA", "weight_bps": 5000}, {"summary": "buy NVDA", "symbol": "NVDA", "weight_bps": 4000}]


def test_weights_off_10000_are_refused_when_drafted_not_at_ship(svc):
    """Smoke 8c/8c2: a 9000-bps draft was recorded (holding the agent's slot) and only refused
    at 🚀 Ship it. Now the draft is refused, with the reason Ship it gives, and nothing is recorded."""
    from sherwood_monitor.fund.rules import NOT_PORTFOLIO_REASON, RuleViolation
    from sherwood_monitor.fund.tools import parse_legs

    svc.claim_topic("alpha-fund", "alpha", "TSLA NVDA pitch")
    before = svc.load("alpha-fund")
    with pytest.raises(RuleViolation) as exc:
        svc.draft_proposal("alpha-fund", "alpha", topic="TSLA NVDA pitch", thesis="t", legs=parse_legs(NINETY, 6),
                           max_capital=40_000_000, max_drawdown_bps=1000, strategy_duration=21600)
    assert exc.value.code == "not_portfolio" and NOT_PORTFOLIO_REASON in exc.value.reason
    assert "these sum to 9000" in exc.value.reason
    after = svc.load("alpha-fund")
    assert after.proposals == [] and after.next_proposal_seq == before.next_proposal_seq
    assert [c.topic for c in after.claims] == ["TSLA NVDA pitch"]  # the claim stays: fix the weights, draft again


def test_the_draft_guard_refuses_the_same_draft(as_caller):
    rt = guard_rt()
    rt.service.claim_topic("alpha-fund", "alpha", "TSLA NVDA pitch")
    guard = make_fund_guard(rt)
    args = {"topic": "TSLA NVDA pitch", "thesis": "t", "legs": NINETY, "max_capital": "40", "max_drawdown_bps": 1000,
            "strategy_duration_seconds": 21600}
    reason = guard("fund_draft_proposal", args)
    assert isinstance(reason, str) and reason.startswith("fund draft blocked (not_portfolio)")
    args["legs"] = [dict(NINETY[0]), dict(NINETY[1], weight_bps=5000)]
    assert guard("fund_draft_proposal", args) is None


def test_weights_must_make_100_percent_under_the_host_signer_too(tmp_path):
    from sherwood_monitor.fund.rules import RuleViolation
    from sherwood_monitor.fund.tools import parse_legs

    s = FundService(LocalJsonBackend(tmp_path), clock=Clock())  # proposal_signer: host
    s.create_fund(smoke_fund(), host_agent())
    s.claim_topic("alpha-fund", "alpha", "TSLA NVDA pitch")
    with pytest.raises(RuleViolation) as exc:
        s.draft_proposal("alpha-fund", "alpha", topic="TSLA NVDA pitch", thesis="t", legs=parse_legs(NINETY, 6),
                         max_capital=40_000_000, max_drawdown_bps=1000, strategy_duration=21600)
    assert exc.value.code == "not_portfolio"
    # Raw-calldata legs (no weights) are still the host signer's to co-sign.
    target = "0x" + "77" * 20
    raw = [Leg(summary="swap", kind="swap", target=target, data="0x1234", cap="40000000"), Leg(summary="back", phase="settle")]
    assert s.draft_proposal("alpha-fund", "alpha", topic="TSLA NVDA pitch", thesis="t", legs=raw,
                            max_capital=40_000_000, max_drawdown_bps=1000, strategy_duration=21600).proposal.id == "p-1"


# == 5. symbols the chain cannot price are refused at draft time ===========================
# Trimmed from a live GET https://api.sherwood.sh/api/v1/sources/prices (2026-11-10): NFLX is absent.
PRICES = {"success": True, "data": {"feed": "prices", "cadence_s": 900, "records": [
    {"symbol": "TSLA", "address": "0x322F0929c4625eD5bAd873c95208D54E1c003b2d", "price_usd": 375.95, "portfolio_price_ok": True},
    {"symbol": "NVDA", "address": "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC", "price_usd": 172.78, "portfolio_price_ok": True},
    {"symbol": "AAPL", "address": "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9", "price_usd": 348.32, "portfolio_price_ok": False},
    {"symbol": "ETH", "address": None, "price_usd": 2086.12, "portfolio_price_ok": True},
    {"symbol": "USDG", "address": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", "price_usd": 1.0, "portfolio_price_ok": True},
]}}


def _draft_tool(rt):
    import json as _json

    from sherwood_monitor.fund.tools import make_fund_tool_handlers

    tools = make_fund_tool_handlers(rt)
    return lambda args: _json.loads(tools["fund_draft_proposal"](args))


def test_a_symbol_the_prices_source_does_not_cover_is_refused(as_caller, monkeypatch):
    """Smoke 8d/8d2: a draft with NFLX (no token on 4663 in CLI 0.103.0) was accepted and shipped."""
    from sherwood_monitor.fund import symbols

    seen = []
    monkeypatch.setattr(symbols, "_get_json", lambda url, timeout: seen.append(url) or PRICES)
    rt = guard_rt()
    rt.service.claim_topic("alpha-fund", "alpha", "TSLA NVDA pitch")
    draft = _draft_tool(rt)
    args = {"topic": "TSLA NVDA pitch", "thesis": "t", "max_capital": "40", "max_drawdown_bps": 1000,
            "strategy_duration_seconds": 21600,
            "legs": [{"summary": "buy NFLX", "symbol": "NFLX", "weight_bps": 5000},
                     {"summary": "buy TSLA", "symbol": "TSLA", "weight_bps": 5000}]}
    out = draft(args)
    assert out["ok"] is False and out["code"] == "unknown_symbol"
    assert out["error"].startswith("NFLX is not in the Sherwood prices source for chain 4663")
    assert "TSLA" in out["error"] and "ETH" not in out["error"]  # ETH has no token address: not a leg
    assert rt.service.load("alpha-fund").proposals == []
    assert seen == [rt.service.tx_builder_url + "/sources/prices"]
    # Covered symbols (a stale weekend price included: portfolio_price_ok is freshness, not coverage).
    args["legs"] = [{"summary": "buy AAPL", "symbol": "aapl", "weight_bps": 5000},
                    {"summary": "buy TSLA", "symbol": "TSLA", "weight_bps": 5000}]
    out = draft(args)
    assert out["ok"] is True and "symbol_check" not in out
    assert len(seen) == 1  # cached


def test_an_unreachable_prices_source_fails_open_with_a_note(as_caller):
    rt = guard_rt()  # conftest: the source is unreachable in tests
    rt.service.claim_topic("alpha-fund", "alpha", "TSLA NVDA pitch")
    out = _draft_tool(rt)({"topic": "TSLA NVDA pitch", "thesis": "t", "max_capital": "40", "max_drawdown_bps": 1000,
                           "strategy_duration_seconds": 21600,
                           "legs": [{"summary": "buy NFLX", "symbol": "NFLX", "weight_bps": 10000}]})
    assert out["ok"] is True
    assert out["symbol_check"].startswith("not checked: the Sherwood prices source")
    assert "NFLX were not verified" in out["symbol_check"]


# == 6. a chain read never clears a cooldown the plugin just started ======================
def test_a_stale_chain_read_does_not_clear_the_settle_cooldown(svc, clock):
    """Smoke 9h-9l: right after fund_record_settle the next /fund read (cooldownUntil "0", cached from
    before the settle) cleared the local cooldown and fund_claim_topic went through, although the
    governor still enforces CooldownNotElapsed for cooldownPeriod."""
    from sherwood_monitor.fund.rules import RuleViolation

    draft(svc)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=9001)
    for status in ("review", "approved", "executed"):
        svc.set_proposal_status("alpha-fund", "alpha", "p-1", status)
    svc.record_settle("alpha-fund", "alpha", "p-1", pnl=1_200_000, pnl_bps=300)
    until = svc.load("alpha-fund").fund.cooldown_until
    assert until == clock() + 3600
    stale = {"data": {"proposeGate": {"openProposals": "0", "cooldownUntil": "0", "ready": True}}}
    svc.apply_chain_card("alpha-fund", stale)
    assert svc.load("alpha-fund").fund.cooldown_until == until
    with pytest.raises(RuleViolation) as exc:
        svc.claim_topic("alpha-fund", "alpha", "AAPL")
    assert exc.value.code == "cooldown"
    # A later chain cooldown still wins.
    later = {"data": {"proposeGate": {"cooldownUntil": str(int(until) + 600)}}}
    svc.apply_chain_card("alpha-fund", later)
    assert svc.load("alpha-fund").fund.cooldown_until == int(until) + 600
