"""0.8.0 open-protocol mode: open deposits, agent-signed proposals and registrations.

The Sherwood factory on Robinhood Chain 4663 opened on 2026-11-06
(`ownerOnlyProposals` false, `depositsRestricted` false, `creationFee` 0). With
`open_deposits` a member who taps Join gets the deposit link with the join link
and nothing waits on the host. With `proposal_signer: agent` the host's "ship
it" and agent approvals hand the host agent the exact signed Sherwood CLI
command; the plugin never signs. Next to each new behaviour, a check that the
restricted / host-signer posture is unchanged.
"""
from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sherwood_monitor.fund.models import Agent, Fund, FundState, Leg, ProposalRecord
from sherwood_monitor.fund.rules import RuleViolation
from sherwood_monitor.fund.service import FundEvent, FundService
from sherwood_monitor.fund.signed_cli import (
    CLI_NOTE,
    MIN_CLI_FOR_COMMANDS,
    cli_duration,
    propose_portfolio_command,
    register_agent_command,
)
from sherwood_monitor.fund.store import LocalJsonBackend
from tests.fund_helpers import GUEST_ADDR, HOST_ADDR, MEMBER_ADDR, TX, VAULT, Clock, fund, legs as raw_legs
from tests.test_fund_plugin import HOST_CFG, boot

STATE_DIR = ".hermes/plugins/sherwood-monitor/funds"
TG_HOST = SimpleNamespace(user_id="900", chat_id="-100500")
# `sherwood strategy propose --help` (0.88.2 here, 0.103.0 on sherwood main): the portfolio flags we emit.
PROPOSE_FLAGS = {"--vault", "--tokens", "--weights", "--amount", "--duration", "--max-drawdown-bps", "--name", "--description"}
VAULT_ADD_FLAGS = {"--vault", "--wallet", "--agent-id"}  # `sherwood vault add --help`
CLI_DURATION_RE = re.compile(r"^(\d+)(d|h|m|s)?$")  # cli/src/lib/governor.ts parseDuration


def portfolio_legs() -> list[Leg]:
    return [Leg(summary="buy NVDA", symbol="NVDA", weight_bps=6000), Leg(summary="buy AAPL", symbol="aapl", weight_bps=4000)]


def portfolio_record(**kw) -> ProposalRecord:
    base = dict(id="p-1", proposer_agent="alpha", thesis="AI capex keeps beating", topic="NVDA AAPL pitch",
                legs=portfolio_legs(), max_capital="100000000", max_drawdown_bps=1500, strategy_duration=86400)
    base.update(kw)
    return ProposalRecord(**base)


EXPECTED_PROPOSE = (
    f"sherwood strategy propose portfolio --vault {VAULT} --tokens NVDA,AAPL --weights 6000,4000 --amount 100 "
    "--duration 1d --max-drawdown-bps 1500 --name 'alpha-fund: NVDA AAPL pitch' --description 'AI capex keeps beating'"
)


# -- the command builder ----------------------------------------------------------------------
def test_propose_command_is_the_exact_cli_call():
    cmd = propose_portfolio_command(fund(), portfolio_record())
    assert cmd == EXPECTED_PROPOSE
    args = shlex.split(cmd)
    assert args[:4] == ["sherwood", "strategy", "propose", "portfolio"]
    assert {a for a in args if a.startswith("--")} == PROPOSE_FLAGS
    assert "--chain" not in args  # mainnet is the CLI's default and only network from 0.93.0
    # --amount is human units of the fund asset (USDG, 6 dp), never base units.
    half = propose_portfolio_command(fund(), portfolio_record(max_capital="12500000"))
    assert shlex.split(half)[shlex.split(half).index("--amount") + 1] == "12.5"


def test_thesis_stays_inert_in_the_shell():
    """Free text goes through shlex.quote: a real POSIX shell sees exactly the arguments, nothing expands."""
    thesis = 'He said "go" $(echo INJECTED) `echo TICKED`; it\'s $HOME\nsecond line'
    cmd = propose_portfolio_command(fund(), portfolio_record(thesis=thesis, topic=""))
    args = shlex.split(cmd)
    assert args[-2:] == ["--description", " ".join(thesis.split())]
    echoed = subprocess.run(
        ["/bin/sh", "-c", "printf '%s\\n' " + cmd.removeprefix("sherwood ")],
        capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home"},
    ).stdout.splitlines()
    assert echoed == args[1:]
    assert "INJECTED" not in [line.strip() for line in echoed] and "/nonexistent-home" not in cmd


@pytest.mark.parametrize("seconds,text", [(86400, "1d"), (172800, "2d"), (21600, "6h"), (3600, "1h"), (120, "2m"), (90, "90s")])
def test_cli_duration(seconds, text):
    assert cli_duration(seconds) == text and CLI_DURATION_RE.match(text)


def test_cli_duration_refuses_zero():
    with pytest.raises(ValueError):
        cli_duration(0)


@pytest.mark.parametrize("record,f,code", [
    (portfolio_record(legs=raw_legs()), fund(), "not_portfolio"),
    (portfolio_record(legs=[Leg(summary="NVDA", symbol="NVDA", weight_bps=5000)]), fund(), "not_portfolio"),
    (portfolio_record(legs=[Leg(summary="x", symbol="NV DA", weight_bps=10000)]), fund(), "bad_symbol"),
    (portfolio_record(strategy_duration=0), fund(), "draft_not_ready"),
    (portfolio_record(), fund(vault=""), "no_vault"),
])
def test_propose_command_refuses_what_it_cannot_express(record, f, code):
    with pytest.raises(RuleViolation) as exc:
        propose_portfolio_command(f, record)
    assert exc.value.code == code


def test_register_command_is_the_exact_cli_call():
    agent = Agent(name="beta", identity_id="202", address=GUEST_ADDR)
    cmd = register_agent_command(fund(), agent)
    assert cmd == f"sherwood vault add --vault {VAULT} --wallet {GUEST_ADDR} --agent-id 202"
    assert {a for a in shlex.split(cmd) if a.startswith("--")} == VAULT_ADD_FLAGS
    with pytest.raises(RuleViolation):
        register_agent_command(fund(), Agent(name="beta", identity_id="", address=GUEST_ADDR))
    assert MIN_CLI_FOR_COMMANDS in CLI_NOTE and "never from the group chat" in CLI_NOTE


# -- fund state: the record field and the chain read -------------------------------------------
def test_older_state_reads_as_restricted_and_the_flag_round_trips():
    assert Fund.from_dict({"name": "alpha-fund"}).open_deposits is False
    assert Fund.from_dict({"name": "alpha-fund", "open_deposits": "true"}).open_deposits is True
    st = FundState(fund=fund(open_deposits=True), proposals=[portfolio_record(propose_command="sherwood x")])
    back = FundState.from_dict(st.to_dict())
    assert back.fund.open_deposits is True and back.proposals[0].propose_command == "sherwood x"


@pytest.fixture
def clock():
    return Clock()


def make_service(tmp_path: Path, clock, **fund_kw) -> FundService:
    s = FundService(LocalJsonBackend(tmp_path), clock=clock)
    s.create_fund(fund(**fund_kw), Agent(name="alpha", identity_id="101", address=HOST_ADDR))
    return s


def test_the_chain_read_of_depositsopen_beats_the_record(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    # tg-card `fund` is the vault's info plus its launch rules (sherwood app/src/lib/launch-rules.ts).
    svc.apply_chain_card("alpha-fund", {"data": {"fund": {"openDeposits": True, "depositsRestricted": False, "depositsOpen": True}}})
    assert svc.load("alpha-fund").fund.open_deposits is True
    svc.apply_chain_card("alpha-fund", {"data": {"fund": {"openDeposits": True, "depositsRestricted": True, "depositsOpen": False}}})
    assert svc.load("alpha-fund").fund.open_deposits is False
    for card in ({"data": {"fund": None}}, {"data": {"fund": {"depositsOpen": "yes"}}}, {"data": {}}):
        svc.apply_chain_card("alpha-fund", card)  # a section that failed soft, or no bool: the record stays
        assert svc.load("alpha-fund").fund.open_deposits is False


def test_open_deposits_admit_a_member_when_they_join(tmp_path, clock):
    svc = make_service(tmp_path, clock, open_deposits=True)
    seen: list[FundEvent] = []
    svc.subscribe(seen.append)
    member = svc.request_membership("alpha-fund", "77", username="alice")
    assert member.approved and member.member_no == 1 and member.approved_at == clock()
    assert [e.kind for e in seen] == ["member_requested", "member_approved"]
    assert seen[-1].data == {"open_deposits": True}
    svc.request_membership("alpha-fund", "77")  # a second tap admits nobody twice
    assert seen[-1].kind == "member_requested" and svc.load("alpha-fund").member("77").member_no == 1


def test_restricted_deposits_still_wait_for_the_host(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    seen: list[FundEvent] = []
    svc.subscribe(seen.append)
    member = svc.request_membership("alpha-fund", "77")
    assert not member.approved and member.member_no is None
    assert [e.kind for e in seen] == ["member_requested"]


# -- ship it ----------------------------------------------------------------------------------
def draft(svc: FundService, legs: list[Leg] | None = None) -> ProposalRecord:
    svc.claim_topic("alpha-fund", "alpha", "NVDA AAPL pitch")
    return svc.draft_proposal(
        "alpha-fund", "alpha", topic="NVDA AAPL pitch", thesis="AI capex keeps beating", legs=legs or portfolio_legs(),
        max_capital=100_000_000, max_drawdown_bps=1500, strategy_duration=86400,
    ).proposal


def test_agent_mode_ship_puts_the_command_on_the_record_and_the_event(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    seen: list[FundEvent] = []
    svc.subscribe(seen.append)
    record = draft(svc)
    shipped = svc.ship_draft("alpha-fund", record.id, caller_telegram_id="900", agent_signs=True)
    assert shipped.propose_command == EXPECTED_PROPOSE
    assert svc.load("alpha-fund").proposal(record.id).propose_command == EXPECTED_PROPOSE  # persisted
    assert seen[-1].kind == "shipped" and seen[-1].data == {"propose_command": EXPECTED_PROPOSE}
    assert seen[-1].proposal.propose_command == EXPECTED_PROPOSE


def test_the_service_default_signer_is_the_setting(tmp_path, clock):
    svc = FundService(LocalJsonBackend(tmp_path), clock=clock, proposal_signer="agent")
    svc.create_fund(fund(), Agent(name="alpha", identity_id="101", address=HOST_ADDR))
    record = draft(svc)
    assert svc.ship_draft("alpha-fund", record.id, caller_telegram_id="900").propose_command == EXPECTED_PROPOSE


def test_host_mode_ship_builds_no_command(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    seen: list[FundEvent] = []
    svc.subscribe(seen.append)
    record = draft(svc)
    shipped = svc.ship_draft("alpha-fund", record.id, caller_telegram_id="900")
    assert shipped.propose_command == "" and seen[-1].data == {}


def test_agent_mode_refuses_a_raw_calldata_draft_and_writes_nothing(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    record = draft(svc, legs=raw_legs(cap="50000000"))
    with pytest.raises(RuleViolation) as exc:
        svc.ship_draft("alpha-fund", record.id, caller_telegram_id="900", agent_signs=True)
    assert exc.value.code == "not_portfolio"
    assert svc.load("alpha-fund").proposal(record.id).shipped_at is None
    # The same draft ships under the host signer, as before.
    assert svc.ship_draft("alpha-fund", record.id, caller_telegram_id="900").shipped_at is not None


# -- tools and slash commands -------------------------------------------------------------------
SETUP = {
    "fund": "alpha-fund", "vault": VAULT, "governor": "0x" + "22" * 20, "telegram_chat_id": "-100500",
    "host_telegram_id": "900", "max_capital": "1000", "max_drawdown_bps": 2000, "voting_period": 1800,
    "agent_fee_bps": 500,
}


@pytest.fixture
def agent_host(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, {**HOST_CFG, "proposal_signer": "agent"})
    assert ctx.call_tool("fund_setup", SETUP)["created"]
    return ctx


@pytest.fixture
def host(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, HOST_CFG)
    assert ctx.call_tool("fund_setup", SETUP)["created"]
    return ctx


def test_setup_open_deposits_reaches_the_create_fund_body(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, HOST_CFG)
    opened = ctx.call_tool("fund_setup", {"fund": "alpha-fund", "open_deposits": True})
    assert opened["prepare"]["body"]["openDeposits"] is True and opened["state"]["open_deposits"] is True
    closed = ctx.call_tool("fund_setup", {"fund": "beta-fund"})  # the default stays restricted
    assert closed["prepare"]["body"]["openDeposits"] is False and closed["state"]["open_deposits"] is False
    flipped = ctx.call_tool("fund_setup", {"fund": "alpha-fund", "open_deposits": "false"})
    assert flipped["ok"] and flipped["prepare"]["body"]["openDeposits"] is False
    bad = ctx.call_tool("fund_setup", {"fund": "alpha-fund", "open_deposits": "maybe"})
    assert bad["ok"] is False and "open_deposits" in bad["error"]


def test_approving_a_member_on_an_open_fund_has_nothing_to_approve(host, monkeypatch):
    from sherwood_monitor.fund import applink

    assert host.call_tool("fund_setup", {"fund": "alpha-fund", "open_deposits": True})["ok"]
    monkeypatch.setenv("SHERWOOD_TG_API_TOKEN", "tok3n")
    monkeypatch.setattr(applink, "_http_get_json", lambda *a, **k: pytest.fail("no app lookup on an open fund"))
    out = host.call_tool("fund_approve_member", {"member": "77"})
    assert out["ok"] and out["open_deposits"] is True and out["approved"] is False and "prepare" not in out
    assert out["note"].startswith("deposits are open on this fund, nothing to approve")
    said = host.run_command("fund-approve", f"77 {MEMBER_ADDR}", source=TG_HOST)
    assert said.startswith("Deposits are open on this fund, nothing to approve") and "POST" not in said
    stranger = host.run_command("fund-approve", "77", source=SimpleNamespace(user_id="77", chat_id="-100500"))
    assert stranger.startswith("Not done: only the fund host")  # the host check still runs first
    status = host.call_tool("fund_status")
    assert status["deposits_open"] is True and "deposits are open to anyone" in status["card"]


def test_restricted_fund_card_and_status_are_unchanged(host):
    status = host.call_tool("fund_status")
    assert status["deposits_open"] is False
    assert "Members: 0 approved" in status["card"] and "deposits are open" not in status["card"]


def test_agent_mode_registration_hands_the_host_agent_the_cli_command(agent_host, tmp_path):
    FundService(LocalJsonBackend(tmp_path / STATE_DIR)).request_agent(
        "alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    expected = f"sherwood vault add --vault {VAULT} --wallet {GUEST_ADDR} --agent-id 202"
    a = agent_host.call_tool("fund_approve_agent", {"agent": "beta"})
    assert a["ok"] and "prepare" not in a and a["command"] == expected
    assert a["note"] == CLI_NOTE and a["then"] == "fund_approve_agent agent=beta tx_hash=<registerAgent tx hash>"
    pending = agent_host.call_tool("fund_pending_commands")
    assert pending["registrations"] == [{"agent": "beta", "command": expected, "then": a["then"]}]
    done = agent_host.call_tool("fund_approve_agent", {"agent": "beta", "tx_hash": TX})
    assert done["agent"]["registered"] and done["agent"]["register_tx"] == TX and "command" not in done
    assert agent_host.call_tool("fund_pending_commands")["registrations"] == []


def test_agent_mode_fund_approve_command_prints_the_cli_command(agent_host, tmp_path):
    FundService(LocalJsonBackend(tmp_path / STATE_DIR)).request_agent(
        "alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    msg = agent_host.run_command("fund-approve", "beta", source=TG_HOST)
    assert f"sherwood vault add --vault {VAULT} --wallet {GUEST_ADDR} --agent-id 202" in msg
    assert msg.startswith("Approved beta. The host agent registers it with the signed Sherwood CLI")
    assert msg.endswith("Then: /fund-approve beta <tx_hash>")
    assert "POST" not in msg and "host wallet" not in msg
    done = agent_host.run_command("fund-approve", f"beta {TX}", source=TG_HOST)
    assert "registered on the vault" in done


def test_host_mode_registration_is_unchanged(host, tmp_path):
    FundService(LocalJsonBackend(tmp_path / STATE_DIR)).request_agent(
        "alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    a = host.call_tool("fund_approve_agent", {"agent": "beta"})
    assert a["prepare"]["route"] == "register-agent" and "command" not in a
    assert "Sign registerAgent from the host wallet" in host.run_command("fund-approve", "beta", source=TG_HOST)
    pending = host.call_tool("fund_pending_commands")
    assert pending["proposals"] == [] and pending["registrations"] == []
    assert pending["note"].startswith("proposal_signer is host")


def draft_tool_args() -> dict:
    return {
        "topic": "NVDA AAPL pitch", "thesis": "AI capex keeps beating",
        "legs": [{"summary": "buy NVDA", "symbol": "NVDA", "weight_bps": 6000},
                 {"summary": "buy AAPL", "symbol": "AAPL", "weight_bps": 4000}],
        "max_capital": "100", "max_drawdown_bps": 1500, "strategy_duration_seconds": 86400,
    }


def test_pending_commands_then_recording_the_propose(agent_host, tmp_path):
    hook = agent_host.hooks["pre_tool_call"][0]
    assert asyncio.run(hook(tool_name="fund_pending_commands", args={})) is None  # read-only: no approval
    assert agent_host.call_tool("fund_claim_topic", {"topic": "NVDA AAPL pitch"})["ok"]
    assert agent_host.call_tool("fund_draft_proposal", draft_tool_args())["ok"]
    assert agent_host.call_tool("fund_pending_commands")["proposals"] == []  # not shipped yet
    # The host's 🚀 Ship it (the Telegram bridge passes agent_signs from the setting).
    FundService(LocalJsonBackend(tmp_path / STATE_DIR)).ship_draft("alpha-fund", "p-1", caller_telegram_id="900", agent_signs=True)
    pending = agent_host.call_tool("fund_pending_commands")
    (entry,) = pending["proposals"]
    assert entry["command"] == EXPECTED_PROPOSE and entry["proposal_id"] == "p-1"
    assert entry["then"] == "fund_set_proposal_status proposal_id=p-1 status=proposed onchain_id=<on-chain id> tx_hash=<propose tx hash>"
    assert pending["note"] == CLI_NOTE and pending["proposal_signer"] == "agent"
    # The agent ran it outside the chat; it records what landed.
    rec = agent_host.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "proposed", "onchain_id": 12, "tx_hash": TX})
    assert rec["ok"] and rec["proposal"]["onchain_id"] == 12 and rec["proposal"]["propose_tx"] == TX
    assert agent_host.call_tool("fund_pending_commands")["proposals"] == []


def test_pending_commands_are_per_agent(agent_host, tmp_path, monkeypatch):
    svc = FundService(LocalJsonBackend(tmp_path / STATE_DIR))
    svc.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    svc.approve_agent("alpha-fund", "alpha", "beta", tx_hash=TX)
    svc.claim_topic("alpha-fund", "beta", "TSLA")
    svc.draft_proposal("alpha-fund", "beta", topic="TSLA", thesis="deliveries", legs=[Leg(summary="TSLA", symbol="TSLA", weight_bps=10000)],
                       max_capital=50_000_000, max_drawdown_bps=1000, strategy_duration=3600)
    svc.ship_draft("alpha-fund", "p-1", caller_telegram_id="900", agent_signs=True)
    assert agent_host.call_tool("fund_pending_commands")["proposals"] == []  # beta's to run, not alpha's
    guest = boot(tmp_path, monkeypatch, {"agent_name": "beta", "agent_identity_id": "202", "agent_address": GUEST_ADDR,
                                         "default_fund": "alpha-fund", "proposal_signer": "agent"})
    (entry,) = guest.call_tool("fund_pending_commands")["proposals"]
    assert entry["command"].startswith(f"sherwood strategy propose portfolio --vault {VAULT} --tokens TSLA --weights 10000 --amount 50 --duration 1h")
    assert guest.call_tool("fund_pending_commands")["registrations"] == []  # only the host registers agents
