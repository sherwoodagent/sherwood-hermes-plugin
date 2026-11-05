"""Host + guest flows end to end, two services sharing one state directory."""
from __future__ import annotations

from pathlib import Path

import pytest

from sherwood_monitor.fund.cards import render_agents, render_fund_card
from sherwood_monitor.fund.models import Agent, Leg
from sherwood_monitor.fund.rules import RuleViolation
from sherwood_monitor.fund.service import FundService, FundServiceError
from sherwood_monitor.fund.store import LocalJsonBackend
from tests.fund_helpers import (
    GUEST2_ADDR,
    GUEST_ADDR,
    MEMBER_ADDR,
    TARGET,
    TX,
    Clock,
    fund,
    host_agent,
)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def services(tmp_path: Path, clock):
    """Two processes' worth of services (host + guest) over the same files."""
    host = FundService(LocalJsonBackend(tmp_path), clock=clock, tx_builder_url="https://api.example/api/v1")
    guest = FundService(LocalJsonBackend(tmp_path), clock=clock)
    host.create_fund(fund(), Agent(name="alpha", identity_id="101", address="0x" + "33" * 20))
    return host, guest


def join_and_register(host, guest, name="beta", address=GUEST_ADDR):
    guest.request_agent("alpha-fund", Agent(name=name, identity_id="202", address=address))
    agent, body = host.approve_agent("alpha-fund", "alpha", name)
    assert not agent.registered and body["agentAddress"] == address and body["agentId"] == "202"
    agent, _ = host.approve_agent("alpha-fund", "alpha", name, tx_hash=TX)
    assert agent.registered and agent.register_tx == TX


def test_create_fund_registers_host_agent(services):
    host, _ = services
    st = host.load("alpha-fund")
    a = st.agent("alpha")
    assert a.role == "host" and a.registered and st.fund.mode == "host"


def test_member_request_and_host_approval(services):
    host, _ = services
    host.request_membership("alpha-fund", "77", address=MEMBER_ADDR, username="ana")
    m, body = host.approve_member("alpha-fund", "alpha", "77", caller_telegram_id="900")
    assert m.approved and m.member_no == 1
    assert body == {"chainId": 4663, "vault": fund().vault, "depositor": MEMBER_ADDR}
    host.request_membership("alpha-fund", "78")
    m2, _ = host.approve_member("alpha-fund", "alpha", "78", address=TARGET)
    assert m2.member_no == 2 and m2.address == TARGET
    with pytest.raises(RuleViolation, match="already approved"):
        host.approve_member("alpha-fund", "alpha", "77")


def test_non_host_caller_cannot_approve(services):
    host, _ = services
    host.request_membership("alpha-fund", "77", address=MEMBER_ADDR)
    with pytest.raises(RuleViolation) as exc:
        host.approve_member("alpha-fund", "alpha", "77", caller_telegram_id="77")
    assert exc.value.code == "not_host"


def test_guest_agent_cannot_approve(services):
    host, guest = services
    join_and_register(host, guest)
    host.request_membership("alpha-fund", "77", address=MEMBER_ADDR)
    with pytest.raises(RuleViolation) as exc:
        guest.approve_member("alpha-fund", "beta", "77")
    assert exc.value.code == "not_host"


def test_join_requires_identity_and_unique_wallet(services):
    host, guest = services
    with pytest.raises(RuleViolation):
        guest.request_agent("alpha-fund", Agent(name="beta", identity_id="", address=GUEST_ADDR))
    guest.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    with pytest.raises(RuleViolation, match="already belongs"):
        guest.request_agent("alpha-fund", Agent(name="gamma", identity_id="303", address=GUEST_ADDR))


def test_coordination_two_agents_one_topic(services, clock):
    host, guest = services
    join_and_register(host, guest)
    host.claim_topic("alpha-fund", "alpha", "TSLA momentum")
    with pytest.raises(RuleViolation) as exc:
        guest.claim_topic("alpha-fund", "beta", "tsla Momentum")
    assert exc.value.code == "topic_claimed"
    clock.advance(600)  # alpha's claim lapses
    assert guest.claim_topic("alpha-fund", "beta", "TSLA momentum").agent == "beta"


def test_unregistered_guest_cannot_claim(services):
    host, guest = services
    guest.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    with pytest.raises(RuleViolation) as exc:
        guest.claim_topic("alpha-fund", "beta", "TSLA")
    assert exc.value.code == "agent_not_registered"


def draft(svc, agent="beta", topic="TSLA momentum", **kw):
    args = dict(
        topic=topic,
        thesis="TSLA runs into deliveries",
        legs=[Leg(summary="buy TSLA", target=TARGET, data="0xabcd", cap="400000000"),
              Leg(summary="sell TSLA", phase="settle", target=TARGET, data="0xdcba")],
        max_capital=500_000_000,
        max_drawdown_bps=1000,
        strategy_duration=3600,
        bond_quote_wood=10**21,
        wood_balance=9 * 10**20,
        idea_by="77",
        strategy=TARGET,
        metadata_uri="ipfs://bafyproposal",
    )
    args.update(kw)
    return svc.draft_proposal("alpha-fund", agent, **args)


def test_full_lifecycle_draft_vote_settle(services, clock):
    host, guest = services
    join_and_register(host, guest)
    host.request_membership("alpha-fund", "77", address=MEMBER_ADDR)
    host.approve_member("alpha-fund", "alpha", "77")

    guest.claim_topic("alpha-fund", "beta", "TSLA momentum")
    result = draft(guest)
    p = result.proposal
    assert p.id == "p-1" and p.status == "draft" and p.max_capital == "500000000"
    assert result.prepare["ready"] is True and result.prepare["missing"] == []
    body = result.prepare["body"]
    assert body["executeCalls"] == [{"target": TARGET, "data": "0xabcd", "value": "0"}]
    assert body["settlementCallCaps"] == ["0"] and body["envelope"]["maxCapital"] == "500000000"
    assert result.prepare["url"] == "https://api.sherwood.sh/api/v1/prepare/propose"
    # Bond: quote 1000 WOOD, balance 900 -> approve 1050, swap 150 first.
    assert result.bond["approve_wood"] == str(1050 * 10**18)
    assert result.bond["swap_gap_wood"] == str(150 * 10**18) and result.bond["swap_first"] is True
    # The claim is consumed by the draft.
    assert host.load("alpha-fund").claims == []

    rec = guest.set_proposal_status("alpha-fund", "beta", "p-1", "proposed", onchain_id=12, tx_hash=TX)
    assert rec.voting_ends_at == clock() + 1800 and rec.onchain_id == 12

    host.record_vote("alpha-fund", "12", voter="77", side="object")
    rec = host.record_vote("alpha-fund", "p-1", voter="77", side="back")
    assert rec.backers == ["77"] and rec.objectors == []
    rec = host.record_vote("alpha-fund", "p-1", voter="alpha", side="object", voter_kind="agent", note="too big")
    assert rec.reviews[0].agent == "alpha" and rec.reviews[0].verdict == "object"
    with pytest.raises(RuleViolation, match="not its own"):
        guest.record_vote("alpha-fund", "p-1", voter="beta", side="back", voter_kind="agent")

    host.set_proposal_status("alpha-fund", "alpha", "p-1", "passed")  # host may move it too
    guest.set_proposal_status("alpha-fund", "beta", "p-1", "executed")
    receipt = guest.record_settle("alpha-fund", "beta", "p-1", pnl=40_000_000, pnl_bps=800, tx_hash=TX)
    st = host.load("alpha-fund")
    assert st.proposal("p-1").status == "settled" and st.receipts[0].idea_by == "77"
    assert receipt.pnl == "40000000"
    assert "Last settle: #12 +40 USDG (+800 bps) by beta" in render_fund_card(st, now=clock()).text


def test_draft_payload_lists_what_is_missing(services):
    host, _ = services
    host.claim_topic("alpha-fund", "alpha", "NVDA")
    res = draft(host, agent="alpha", topic="NVDA", strategy="", metadata_uri="",
                legs=[Leg(summary="buy NVDA", cap="100")], bond_quote_wood=None, wood_balance=None)
    assert res.prepare["ready"] is False
    missing = " ".join(res.prepare["missing"])
    assert "strategy" in missing and "metadataURI" in missing and "--calldata-only" in missing
    assert "missing" in res.bond and res.bond["bond_value_asset_units"] == "5000000"


def test_draft_over_fund_ceiling_writes_nothing(services):
    host, _ = services
    host.claim_topic("alpha-fund", "alpha", "NVDA")
    before = host.load("alpha-fund")
    with pytest.raises(RuleViolation) as exc:
        draft(host, agent="alpha", topic="NVDA", max_capital=2_000_000_000)
    assert exc.value.code == "exceeds_max_capital"
    after = host.load("alpha-fund")
    assert after.revision == before.revision and after.proposals == []


def test_max_two_open_per_fund_across_agents(services):
    host, guest = services
    join_and_register(host, guest)
    join_and_register(host, guest, "gamma", GUEST2_ADDR)
    host.claim_topic("alpha-fund", "alpha", "A")
    draft(host, agent="alpha", topic="A")
    guest.claim_topic("alpha-fund", "beta", "B")
    draft(guest, agent="beta", topic="B")
    with pytest.raises(RuleViolation) as exc:
        guest.claim_topic("alpha-fund", "gamma", "C")
    assert exc.value.code == "fund_full"
    guest.set_proposal_status("alpha-fund", "beta", "p-2", "cancelled")
    assert guest.claim_topic("alpha-fund", "gamma", "C").agent == "gamma"


def test_remove_agent_one_action(services):
    host, guest = services
    join_and_register(host, guest)
    guest.claim_topic("alpha-fund", "beta", "B")
    draft(guest, topic="B")
    join_and_register(host, guest, "gamma", GUEST2_ADDR)
    guest.claim_topic("alpha-fund", "gamma", "G")
    removed = host.remove_agent("alpha-fund", "alpha", "beta")
    st = host.load("alpha-fund")
    assert removed.removed and st.proposal("p-1").status == "cancelled"
    assert [c.agent for c in st.claims] == ["gamma"]
    with pytest.raises(RuleViolation):
        guest.claim_topic("alpha-fund", "beta", "B")
    with pytest.raises(RuleViolation, match="cannot be removed"):
        host.remove_agent("alpha-fund", "alpha", "alpha")
    # A removed agent may ask to join again; it starts unapproved.
    again = guest.request_agent("alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    assert not again.registered and not again.removed
    assert "waiting: host runs /fund-approve beta" in render_agents(host.load("alpha-fund"), now=0)


def test_settle_only_from_executed(services):
    host, _ = services
    host.claim_topic("alpha-fund", "alpha", "A")
    draft(host, agent="alpha", topic="A")
    with pytest.raises(RuleViolation, match="cannot go from draft to settled"):
        host.record_settle("alpha-fund", "alpha", "p-1", pnl=1)


def test_resolve_fund(services, tmp_path):
    host, _ = services
    assert host.resolve_fund(None) == "alpha-fund"
    assert host.resolve_fund(None, chat_id="-100500") == "alpha-fund"
    host.create_fund(fund(name="beta-fund", telegram_chat_id="-200"), host_agent())
    assert host.resolve_fund(None, chat_id="-200") == "beta-fund"
    with pytest.raises(FundServiceError, match="which fund"):
        host.resolve_fund(None)
    assert host.resolve_fund(None, default="alpha-fund") == "alpha-fund"
