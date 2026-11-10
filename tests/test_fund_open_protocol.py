"""0.8.0 open-protocol mode: open deposits, agent-signed proposals and registrations.

The Sherwood factory on Robinhood Chain 4663 opened on 2026-11-06
(`ownerOnlyProposals` false, `depositsRestricted` false, `creationFee` 0). With
`open_deposits` a member who taps Join gets the deposit link with the join link
and nothing waits on the host. With `proposal_signer: agent` the host's "ship
it" and agent approvals hand the agent the exact Sherwood CLI command, which
the CLI's wallet signs; the plugin never signs. Next to each new behaviour, a
check that the restricted / host-signer posture is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sherwood_monitor.fund import signed_cli, txbuilder
from sherwood_monitor.fund.models import Agent, Fund, FundState, Leg, ProposalRecord, as_bool
from sherwood_monitor.fund.rules import RuleViolation
from sherwood_monitor.fund.service import FundEvent, FundService
from sherwood_monitor.fund.signed_cli import (
    CLI_NOTE,
    MIN_CLI_FOR_COMMANDS,
    cli_duration,
    cli_version_warning,
    propose_portfolio_command,
    register_agent_command,
)
from sherwood_monitor.fund.store import LocalJsonBackend
from tests.fund_helpers import GUEST2_ADDR, GUEST_ADDR, HOST_ADDR, MEMBER_ADDR, TX, VAULT, Clock, fund, legs as raw_legs
from tests.test_fund_plugin import HOST_CFG, boot

STATE_DIR = ".hermes/plugins/sherwood-monitor/funds"
TG_HOST = SimpleNamespace(user_id="900", chat_id="-100500")
# `sherwood strategy propose --help` (0.88.2 and 0.103.0): the portfolio flags we emit.
SEPARATE_FLAGS = {"--vault", "--tokens", "--weights", "--amount", "--duration", "--max-drawdown-bps"}
ATTACHED_FLAGS = ("--name=", "--description=")
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
    "--duration 1d --max-drawdown-bps 1500 '--name=alpha-fund: NVDA AAPL pitch' '--description=AI capex keeps beating'"
)


def assert_no_value_reads_as_a_flag(cmd: str) -> list[str]:
    """Every argument after the template is a known flag, an attached --name=/--description=, or a value
    that follows a known flag and does not start with '-' (commander would read it as a flag)."""
    args = shlex.split(cmd)
    assert args[:4] == ["sherwood", "strategy", "propose", "portfolio"]
    rest = args[4:]
    i = 0
    seen = set()
    while i < len(rest):
        tok = rest[i]
        if tok.startswith(ATTACHED_FLAGS):
            seen.add(tok.split("=", 1)[0])
            i += 1
            continue
        assert tok in SEPARATE_FLAGS, tok
        value = rest[i + 1]
        assert not value.startswith("-"), (tok, value)
        seen.add(tok)
        i += 2
    assert seen == SEPARATE_FLAGS | {"--name", "--description"}
    return args


# -- the command builder ----------------------------------------------------------------------
def test_propose_command_is_the_exact_cli_call():
    cmd = propose_portfolio_command(fund(), portfolio_record())
    assert cmd == EXPECTED_PROPOSE
    args = assert_no_value_reads_as_a_flag(cmd)
    assert not any(a.startswith("--chain") for a in args)  # mainnet is the CLI's default and only network
    # --amount is human units of the fund asset (USDG, 6 dp), never base units.
    half = shlex.split(propose_portfolio_command(fund(), portfolio_record(max_capital="12500000")))
    assert half[half.index("--amount") + 1] == "12.5"


def test_thesis_stays_inert_in_the_shell():
    """Free text goes through shlex.quote: a real POSIX shell sees exactly the arguments, nothing expands."""
    thesis = 'He said "go" $(echo INJECTED) `echo TICKED`; it\'s $HOME\nsecond line'
    cmd = propose_portfolio_command(fund(), portfolio_record(thesis=thesis, topic=""))
    args = shlex.split(cmd)
    flat = " ".join(thesis.split())
    assert args[-1] == "--description=" + flat
    echoed = subprocess.run(
        ["/bin/sh", "-c", "printf '%s\\n' " + cmd.removeprefix("sherwood ")],
        capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home"},
    ).stdout.splitlines()
    assert echoed == args[1:]
    # Nothing expanded: $HOME stayed literal (the shell's HOME never shows up) and no substitution ran.
    assert echoed[-1] == "--description=" + flat and "$HOME" in echoed[-1] and "$(echo INJECTED)" in echoed[-1]
    assert not any("/nonexistent-home" in line or line.strip() in ("INJECTED", "TICKED") for line in echoed)


@pytest.mark.parametrize("thesis", ["--chain=robinhood-fork long NVDA", "-V", "--calldata-only", "-h"])
def test_a_thesis_that_looks_like_a_flag_stays_a_value(thesis):
    """commander scans every argument before the subcommand: a separate value starting with '-' would be
    read as the root program's flag (`-V` prints the version and exits). The attached form keeps it a value."""
    for topic in ("", "NVDA AAPL pitch"):
        cmd = propose_portfolio_command(fund(), portfolio_record(thesis=thesis, topic=topic))
        args = assert_no_value_reads_as_a_flag(cmd)
        assert args[-1] == "--description=" + thesis
        assert thesis not in args  # never a token of its own


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
    (portfolio_record(legs=[Leg(summary="x", symbol="NVDA", weight_bps=5000), Leg(summary="y", symbol="nvda", weight_bps=5000)]),
     fund(), "duplicate_symbol"),
    (portfolio_record(legs=[Leg(summary=f"t{i}", symbol=f"T{i}", weight_bps=10000 // 25) for i in range(25)]), fund(), "too_many_legs"),
    (portfolio_record(strategy_duration=0), fund(), "draft_not_ready"),
    (portfolio_record(max_drawdown_bps=None), fund(), "draft_not_ready"),
    (portfolio_record(max_drawdown_bps=-5), fund(), "draft_not_ready"),
    (portfolio_record(max_drawdown_bps=10_001), fund(), "draft_not_ready"),
    (portfolio_record(), fund(vault=""), "no_vault"),
])
def test_propose_command_refuses_what_it_cannot_express(record, f, code):
    with pytest.raises(RuleViolation) as exc:
        propose_portfolio_command(f, record)
    assert exc.value.code == code


def test_twenty_legs_is_the_limit():
    twenty = [Leg(summary=f"t{i}", symbol=f"T{i}", weight_bps=500) for i in range(20)]
    assert "--tokens T0,T1," in propose_portfolio_command(fund(), portfolio_record(legs=twenty))


def test_register_command_is_the_exact_cli_call():
    agent = Agent(name="beta", identity_id="202", address=GUEST_ADDR)
    cmd = register_agent_command(fund(), agent)
    assert cmd == f"sherwood vault add --vault {VAULT} --wallet {GUEST_ADDR} --agent-id 202"
    assert {a for a in shlex.split(cmd) if a.startswith("--")} == VAULT_ADD_FLAGS
    with pytest.raises(RuleViolation):
        register_agent_command(fund(), Agent(name="beta", identity_id="", address=GUEST_ADDR))
    assert MIN_CLI_FOR_COMMANDS == "0.103.0" and MIN_CLI_FOR_COMMANDS in CLI_NOTE
    assert "never from the group chat" in CLI_NOTE.lower() and "never on boot" in CLI_NOTE


# -- the CLI version check (read-only `sherwood --version`, through preflight) -----------------------
def fake_version(monkeypatch, version: str | None = None, *, missing: bool = False):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if missing:
            raise FileNotFoundError(argv[0])
        return SimpleNamespace(returncode=0, stdout=f"{version}\n", stderr="")

    monkeypatch.setattr("sherwood_monitor.preflight.subprocess.run", run)
    return calls


def test_cli_version_warning(monkeypatch):
    calls = fake_version(monkeypatch, "0.103.0")
    assert cli_version_warning("sherwood") is None and calls == [["sherwood", "--version"]]
    fake_version(monkeypatch, "0.88.2")
    old = cli_version_warning("sherwood")
    assert "0.88.2" in old and "0.103.0" in old and "upgrade" in old
    fake_version(monkeypatch, missing=True)
    assert "could not read" in cli_version_warning("/opt/sherwood")

    def boom(*a, **k):
        raise RuntimeError("weird")

    monkeypatch.setattr("sherwood_monitor.preflight.check_cli_installed", boom)
    assert "could not check" in cli_version_warning("sherwood")  # never raises


# -- fund state: the record field and the chain read -------------------------------------------
def test_older_state_reads_as_restricted_and_the_flag_round_trips():
    assert Fund.from_dict({"name": "alpha-fund"}).open_deposits is False
    assert Fund.from_dict({"name": "alpha-fund", "open_deposits": None}).open_deposits is False  # a null loads
    assert Fund.from_dict({"name": "alpha-fund", "open_deposits": "true"}).open_deposits is True
    assert as_bool(None) is False
    st = FundState(fund=fund(open_deposits=True), proposals=[portfolio_record(propose_command="sherwood x", handed_out_at=5.0)])
    back = FundState.from_dict(st.to_dict())
    assert back.fund.open_deposits is True and back.proposals[0].propose_command == "sherwood x"
    assert back.proposals[0].handed_out_at == 5.0


@pytest.fixture
def clock():
    return Clock()


def make_service(tmp_path: Path, clock, **fund_kw) -> FundService:
    s = FundService(LocalJsonBackend(tmp_path), clock=clock)
    s.create_fund(fund(**fund_kw), Agent(name="alpha", identity_id="101", address=HOST_ADDR))
    return s


def rules_card(open_: bool) -> dict:
    """tg-card `fund`: the vault info plus its launch rules, as sherwood app/src/lib/launch-rules.ts
    sends them: always bools; an unread rule fails closed to `depositsOpen: false` with no warning."""
    return {"data": {"fund": {"openDeposits": True, "depositsRestricted": not open_, "depositsOpen": open_,
                              "ownerOnlyProposals": False}}}


def test_a_closed_read_closes_an_open_fund_only_after_two_reads_in_a_row(tmp_path, clock, caplog):
    svc = make_service(tmp_path, clock, open_deposits=True)
    caplog.set_level(logging.WARNING, logger="sherwood_monitor.fund.service")
    svc.apply_chain_card("alpha-fund", rules_card(False))  # could be a failed rule read: not trusted yet
    assert svc.load("alpha-fund").fund.open_deposits is True
    assert "read 1 of 2" in caplog.text
    svc.apply_chain_card("alpha-fund", rules_card(True))  # an open read in between resets the count
    svc.apply_chain_card("alpha-fund", rules_card(False))
    assert svc.load("alpha-fund").fund.open_deposits is True
    caplog.clear()
    svc.apply_chain_card("alpha-fund", rules_card(False))
    assert svc.load("alpha-fund").fund.open_deposits is False
    assert "2 chain reads in a row say deposits are restricted" in caplog.text


def test_an_open_read_opens_a_restricted_fund_at_once(tmp_path, clock, caplog):
    svc = make_service(tmp_path, clock)
    caplog.set_level(logging.WARNING, logger="sherwood_monitor.fund.service")
    svc.apply_chain_card("alpha-fund", rules_card(False))
    assert svc.load("alpha-fund").fund.open_deposits is False and caplog.text == ""  # no flip, no noise
    svc.apply_chain_card("alpha-fund", rules_card(True))
    assert svc.load("alpha-fund").fund.open_deposits is True  # a failed read never opens a fund, so true is trusted
    assert "the chain says deposits are open" in caplog.text


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


# -- guest agents: the approved wallet is the one registered ---------------------------------------
def test_a_guest_that_changes_wallet_or_identity_after_approval_is_pending_again(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    svc.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    svc.approve_agent("alpha-fund", "alpha", "beta")
    again = svc.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    assert again.approved  # the same pair asking again keeps the approval
    swapped = svc.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST2_ADDR))
    assert not swapped.approved and swapped.approved_at is None and swapped.address == GUEST2_ADDR
    svc.approve_agent("alpha-fund", "alpha", "beta")
    renumbered = svc.request_agent("alpha-fund", Agent(name="beta", identity_id="999", address=GUEST2_ADDR))
    assert not renumbered.approved


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
    svc.mark_handed_out("alpha-fund", [record.id])
    assert svc.load("alpha-fund").proposal(record.id).handed_out_at == clock()
    reshipped = svc.ship_draft("alpha-fund", record.id, caller_telegram_id="900", agent_signs=True)
    assert reshipped.handed_out_at is None  # a fresh ship is listed again at once


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


class FundCardStub:
    """GET funds/<vault>/tg-card at the transport: an idle vault unless a test says otherwise."""

    def __init__(self) -> None:
        self.status = 200
        self.open: list[dict] = []
        self.recent: list[dict] = []
        self.warnings: list[str] = []
        self.gate_failed = False  # the API's shape for a failed openProposalCount/cooldown read
        self.calls = 0

    def __call__(self, url: str, timeout: float):
        self.calls += 1
        assert f"/funds/{VAULT}/tg-card?chain=4663" in url
        if self.status != 200:
            return self.status, {}
        gate = None if self.gate_failed else {
            "openProposals": len(self.open), "cooldownUntil": "0", "ready": not self.open}
        return 200, {"success": True, "data": {
            "proposeGate": gate,
            "proposals": {"count": len(self.recent), "open": self.open, "recent": self.recent},
            "warnings": self.warnings,
        }}


@pytest.fixture
def card(monkeypatch):
    stub = FundCardStub()
    monkeypatch.setattr(txbuilder, "_urllib_get_json", stub)
    return stub


@pytest.fixture
def cli(monkeypatch):
    return fake_version(monkeypatch, "0.103.0")


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


def test_agent_mode_registration_hands_the_host_agent_the_cli_command(agent_host, tmp_path, cli):
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


def test_a_guest_wallet_swap_drops_the_pending_vault_add(agent_host, tmp_path, cli):
    svc = FundService(LocalJsonBackend(tmp_path / STATE_DIR))
    svc.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    assert agent_host.call_tool("fund_approve_agent", {"agent": "beta"})["ok"]
    assert len(agent_host.call_tool("fund_pending_commands")["registrations"]) == 1
    svc.request_agent("alpha-fund", Agent(name="beta", identity_id="999", address=GUEST2_ADDR))  # a second /join-fund
    assert agent_host.call_tool("fund_pending_commands")["registrations"] == []
    again = agent_host.call_tool("fund_approve_agent", {"agent": "beta"})  # the host approves the new pair explicitly
    assert again["command"] == f"sherwood vault add --vault {VAULT} --wallet {GUEST2_ADDR} --agent-id 999"


def test_agent_mode_fund_approve_command_prints_the_cli_command(agent_host, tmp_path):
    FundService(LocalJsonBackend(tmp_path / STATE_DIR)).request_agent(
        "alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    msg = agent_host.run_command("fund-approve", "beta", source=TG_HOST)
    assert f"sherwood vault add --vault {VAULT} --wallet {GUEST_ADDR} --agent-id 202" in msg
    assert msg.startswith("Approved beta. The host agent registers it with the Sherwood CLI")
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


def shipped_draft(ctx, tmp_path) -> FundService:
    assert ctx.call_tool("fund_claim_topic", {"topic": "NVDA AAPL pitch"})["ok"]
    assert ctx.call_tool("fund_draft_proposal", draft_tool_args())["ok"]
    svc = FundService(LocalJsonBackend(tmp_path / STATE_DIR))
    # The host's 🚀 Ship it (the Telegram bridge passes agent_signs from the setting).
    svc.ship_draft("alpha-fund", "p-1", caller_telegram_id="900", agent_signs=True)
    return svc


def test_pending_commands_then_recording_the_propose(agent_host, tmp_path, card, cli):
    hook = agent_host.hooks["pre_tool_call"][0]
    assert asyncio.run(hook(tool_name="fund_pending_commands", args={})) is None  # no approval prompt
    assert agent_host.call_tool("fund_claim_topic", {"topic": "NVDA AAPL pitch"})["ok"]
    assert agent_host.call_tool("fund_draft_proposal", draft_tool_args())["ok"]
    assert agent_host.call_tool("fund_pending_commands")["proposals"] == []  # not shipped yet
    assert card.calls == 0  # nothing to hand out, no chain read
    FundService(LocalJsonBackend(tmp_path / STATE_DIR)).ship_draft("alpha-fund", "p-1", caller_telegram_id="900", agent_signs=True)
    pending = agent_host.call_tool("fund_pending_commands")
    (entry,) = pending["proposals"]
    assert entry["command"] == EXPECTED_PROPOSE and entry["proposal_id"] == "p-1"
    assert entry["then"] == "fund_set_proposal_status proposal_id=p-1 status=proposed onchain_id=<on-chain id> tx_hash=<propose tx hash>"
    assert pending["note"] == CLI_NOTE and pending["proposal_signer"] == "agent" and "cli_warning" not in pending
    assert card.calls == 1 and cli == [["sherwood", "--version"]]
    # The agent ran it outside the chat; it records what landed.
    rec = agent_host.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "proposed", "onchain_id": 12, "tx_hash": TX})
    assert rec["ok"] and rec["proposal"]["onchain_id"] == 12 and rec["proposal"]["propose_tx"] == TX
    assert agent_host.call_tool("fund_pending_commands")["proposals"] == []


def test_a_handed_out_command_is_held_back_for_30_minutes(agent_host, tmp_path, card, cli):
    svc = shipped_draft(agent_host, tmp_path)
    first = agent_host.call_tool("fund_pending_commands")
    assert [e["proposal_id"] for e in first["proposals"]] == ["p-1"]
    stamped = svc.load("alpha-fund").proposal("p-1").handed_out_at
    assert stamped is not None and abs(stamped - time.time()) < 60
    # A deny-mode cron could not record it: the next run must not propose it again.
    second = agent_host.call_tool("fund_pending_commands")
    assert second["proposals"] == []
    (held,) = second["held_back"]
    assert held["proposal_id"] == "p-1" and "handed out less than 30 minutes ago" in held["reason"]
    again = agent_host.call_tool("fund_pending_commands", {"include_recent": True})
    assert [e["proposal_id"] for e in again["proposals"]] == ["p-1"]
    svc.backend.update("alpha-fund", lambda st: setattr(st.proposal("p-1"), "handed_out_at", time.time() - 1801))
    assert [e["proposal_id"] for e in agent_host.call_tool("fund_pending_commands")["proposals"]] == ["p-1"]


def test_propose_commands_are_held_while_the_vault_has_an_open_proposal(agent_host, tmp_path, card, cli):
    svc = shipped_draft(agent_host, tmp_path)
    card.open = [{"id": "7", "state": "Pending", "proposer": "0x" + "99" * 20}]
    out = agent_host.call_tool("fund_pending_commands")
    assert out["proposals"] == [] and "VaultHasOpenProposal" in out["chain_hold"] and "#7" in out["chain_hold"]
    assert out["held_back"][0]["proposal_id"] == "p-1"
    assert svc.load("alpha-fund").proposal("p-1").handed_out_at is None  # held, not handed out
    card.open = []
    assert [e["proposal_id"] for e in agent_host.call_tool("fund_pending_commands")["proposals"]] == ["p-1"]


def test_fresh_vault_an_unrecorded_proposal_from_this_wallet_holds_the_propose(agent_host, tmp_path, card, cli):
    """An earlier run proposed and could not record it; the vault is idle again (settled or vetoed)."""
    shipped_draft(agent_host, tmp_path)
    card.recent = [{"id": "1", "state": "Settled", "proposer": HOST_ADDR}]
    out = agent_host.call_tool("fund_pending_commands")
    assert out["proposals"] == [] and "#1 from your wallet is on chain and newer than anything recorded" in out["chain_hold"]
    assert "If it is draft p-1, record it" in out["chain_hold"]
    assert "predates this fund record and can be ignored" in out["chain_hold"]
    agent_host.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "proposed", "onchain_id": 1, "tx_hash": TX})
    assert agent_host.call_tool("fund_pending_commands")["held_back"] == []


def test_include_recent_bypasses_only_the_unrecorded_hold(agent_host, tmp_path, card, cli):
    shipped_draft(agent_host, tmp_path)
    card.recent = [{"id": "1", "state": "Settled", "proposer": HOST_ADDR}]
    assert [e["proposal_id"] for e in agent_host.call_tool("fund_pending_commands", {"include_recent": True})["proposals"]] == ["p-1"]
    card.open = [{"id": "2", "state": "Pending", "proposer": "0x" + "99" * 20}]
    out = agent_host.call_tool("fund_pending_commands", {"include_recent": True})
    assert out["proposals"] == [] and "VaultHasOpenProposal" in out["chain_hold"]  # never bypassed


HISTORY = [{"id": str(i), "state": "Settled", "proposer": HOST_ADDR} for i in range(8, 0, -1)]  # #1-#8 by the owner


def clear_handed_out(svc: FundService) -> None:
    """As if the 30-minute hold had passed since the last listing."""
    svc.backend.update("alpha-fund", lambda st: setattr(st.proposal("p-1"), "handed_out_at", None))


def test_history_vault_old_proposals_below_the_highest_recorded_id_never_hold(agent_host, tmp_path, card, cli):
    """A vault with history (e.g. #1-#8 by its owner): ids at or below the newest one recorded here are old."""
    svc = FundService(LocalJsonBackend(tmp_path / STATE_DIR))
    svc.backend.update("alpha-fund", lambda st: st.proposals.append(
        ProposalRecord(id="p-0", proposer_agent="alpha", thesis="old", status="settled", onchain_id=8)))
    svc.backend.update("alpha-fund", lambda st: setattr(st, "next_proposal_seq", 1))
    shipped_draft(agent_host, tmp_path)
    card.recent = list(HISTORY)
    out = agent_host.call_tool("fund_pending_commands")
    assert [e["proposal_id"] for e in out["proposals"]] == ["p-1"] and "chain_hold" not in out
    card.recent = [{"id": "9", "state": "Settled", "proposer": HOST_ADDR}, *HISTORY]  # a newer one is suspect
    clear_handed_out(svc)
    held = agent_host.call_tool("fund_pending_commands")
    assert held["proposals"] == [] and "#9 from your wallet" in held["chain_hold"] and "#8" not in held["chain_hold"]


def test_history_vault_proposals_created_before_the_ship_never_hold(agent_host, tmp_path, card, cli):
    svc = shipped_draft(agent_host, tmp_path)
    shipped_at = svc.load("alpha-fund").proposal("p-1").shipped_at
    card.recent = [{**row, "createdAt": str(int(shipped_at) - 3600)} for row in HISTORY]
    out = agent_host.call_tool("fund_pending_commands")
    assert [e["proposal_id"] for e in out["proposals"]] == ["p-1"] and "chain_hold" not in out
    card.recent = [{"id": "9", "state": "Settled", "proposer": HOST_ADDR, "createdAt": str(int(shipped_at) + 60)}, *card.recent]
    clear_handed_out(svc)
    held = agent_host.call_tool("fund_pending_commands")
    assert held["proposals"] == [] and "#9 from your wallet" in held["chain_hold"] and "#8" not in held["chain_hold"]


def test_history_vault_without_created_times_holds_once_then_include_recent_lists_it(agent_host, tmp_path, card, cli):
    """Nothing recorded here and no created time on the card: ids alone cannot tell, so the hold says the
    proposals may predate this fund record and include_recent=true lists the command."""
    shipped_draft(agent_host, tmp_path)
    card.recent = list(HISTORY)
    out = agent_host.call_tool("fund_pending_commands")
    assert out["proposals"] == [] and "#1, #2, #3, #4, #5, #6, #7, #8 from your wallet" in out["chain_hold"]
    assert "predates this fund record and can be ignored: list again with include_recent=true" in out["chain_hold"]
    assert [e["proposal_id"] for e in agent_host.call_tool("fund_pending_commands", {"include_recent": True})["proposals"]] == ["p-1"]


@pytest.mark.parametrize("warning", [
    "Could not read the vault's proposals.",
    "Could not read some of the vault's proposals.",
])
def test_a_failed_proposals_section_holds_propose_commands(agent_host, tmp_path, card, cli, warning):
    """The API reports a failed section as a warning, with empty lists, never as an HTTP error."""
    shipped_draft(agent_host, tmp_path)
    card.warnings = [warning]
    out = agent_host.call_tool("fund_pending_commands", {"include_recent": True})
    assert out["proposals"] == [] and warning in out["chain_hold"]


def test_a_null_propose_gate_holds_propose_commands(agent_host, tmp_path, card, cli):
    shipped_draft(agent_host, tmp_path)
    card.gate_failed = True
    card.warnings = ["Could not read the governor's open-proposal count / cooldown."]
    out = agent_host.call_tool("fund_pending_commands", {"include_recent": True})
    assert out["proposals"] == [] and "open-proposal count" in out["chain_hold"]
    card.warnings = []  # a null gate with no warning still holds
    out = agent_host.call_tool("fund_pending_commands", {"include_recent": True})
    assert out["proposals"] == [] and "no proposeGate" in out["chain_hold"]


def test_unrelated_card_warnings_do_not_hold(agent_host, tmp_path, card, cli):
    shipped_draft(agent_host, tmp_path)
    card.warnings = ["Could not read ProposalSettled logs: settlement results and realized P&L are unavailable.",
                     "Could not read the guardian review of proposal 3."]
    assert [e["proposal_id"] for e in agent_host.call_tool("fund_pending_commands")["proposals"]] == ["p-1"]


def test_an_unreadable_fund_card_holds_propose_commands(agent_host, tmp_path, card, cli):
    shipped_draft(agent_host, tmp_path)
    card.status = 503
    out = agent_host.call_tool("fund_pending_commands")
    assert out["proposals"] == [] and out["chain_hold"].startswith("could not read the vault's proposals")


def test_a_command_shipped_over_a_day_ago_is_expired(agent_host, tmp_path, card, cli):
    svc = shipped_draft(agent_host, tmp_path)
    svc.backend.update("alpha-fund", lambda st: setattr(st.proposal("p-1"), "shipped_at", time.time() - 86401))
    out = agent_host.call_tool("fund_pending_commands", {"include_recent": True})
    assert out["proposals"] == [] and card.calls == 0
    (expired,) = out["expired"]
    assert expired["proposal_id"] == "p-1" and "ship it again" in expired["reason"] and "command" not in expired
    svc.ship_draft("alpha-fund", "p-1", caller_telegram_id="900", agent_signs=True)
    assert [e["proposal_id"] for e in agent_host.call_tool("fund_pending_commands")["proposals"]] == ["p-1"]


def test_an_old_cli_is_flagged_but_the_listing_still_answers(agent_host, tmp_path, card, monkeypatch):
    shipped_draft(agent_host, tmp_path)
    fake_version(monkeypatch, "0.88.2")
    out = agent_host.call_tool("fund_pending_commands")
    assert out["ok"] and len(out["proposals"]) == 1 and "0.88.2" in out["cli_warning"]
    fake_version(monkeypatch, missing=True)
    assert "could not read" in agent_host.call_tool("fund_pending_commands", {"include_recent": True})["cli_warning"]


def test_pending_commands_are_per_agent(agent_host, tmp_path, monkeypatch, card, cli):
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
    fake_version(monkeypatch, "0.103.0")
    (entry,) = guest.call_tool("fund_pending_commands")["proposals"]
    assert entry["command"].startswith(f"sherwood strategy propose portfolio --vault {VAULT} --tokens TSLA --weights 10000 --amount 50 --duration 1h")
    assert guest.call_tool("fund_pending_commands")["registrations"] == []  # only the host registers agents


def test_signed_cli_never_runs_anything(monkeypatch):
    """Building commands spawns nothing: only the version check reads `sherwood --version`."""
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("no subprocess while building commands"))
    propose_portfolio_command(fund(), portfolio_record())
    register_agent_command(fund(), Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    assert signed_cli.HANDOUT_HOLD_SECONDS == 1800 and signed_cli.SHIP_TTL_SECONDS == 86400


# -- the terminal risk hook reads the vault, not the template --------------------------------------
def test_the_risk_hook_reads_the_vault_not_the_template():
    from sherwood_monitor.hooks import _parse_propose_command

    assert _parse_propose_command(EXPECTED_PROPOSE)[0] == VAULT
    assert _parse_propose_command(f"sherwood strategy propose portfolio --vault={VAULT} --amount 1")[0] == VAULT
    # Unchanged: a positional fund, and no --vault.
    assert _parse_propose_command("sherwood proposal create alpha --size-usd 5000") == ("alpha", 5000.0, "")
    assert _parse_propose_command("sherwood strategy propose alpha --size-usd 5000")[0] == "alpha"


async def test_the_state_fetcher_names_an_address_with_vault(monkeypatch):
    from sherwood_monitor import state_fetcher

    seen = []

    async def fake_exec(*argv, **kw):
        seen.append(argv)
        raise FileNotFoundError(argv[0])

    async def supports_json(sherwood_bin):  # a future CLI whose `vault info --help` lists --json
        return True

    monkeypatch.setattr(state_fetcher.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(state_fetcher, "vault_info_supports_json", supports_json)
    assert await state_fetcher.fetch_vault_info("sherwood", VAULT) is None
    assert await state_fetcher.fetch_vault_info("sherwood", "alpha") is None
    assert seen == [("sherwood", "vault", "info", "--vault", VAULT, "--json"), ("sherwood", "vault", "info", "alpha", "--json")]


def test_listing_from_a_chat_turn_does_not_hold_back_the_operators_run(agent_host, tmp_path, card, cli, monkeypatch):
    from sherwood_monitor import session

    svc = shipped_draft(agent_host, tmp_path)
    env = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_USER_ID": "77", "HERMES_SESSION_CHAT_ID": "-100500"}
    monkeypatch.setattr(session, "_session_env", lambda name: env.get(name, ""))
    chat = agent_host.call_tool("fund_pending_commands")
    assert len(chat["proposals"]) == 1 and chat["handed_out"] is False
    assert svc.load("alpha-fund").proposal("p-1").handed_out_at is None
    monkeypatch.setattr(session, "_session_env", lambda name: "")  # the operator's one-shot
    run = agent_host.call_tool("fund_pending_commands")
    assert len(run["proposals"]) == 1 and run["handed_out"] is True
