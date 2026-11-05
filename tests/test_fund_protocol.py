"""Fund-core additions for the 4663 fork facts and the Telegram wiring.

Fork run 2026-11-05 (scripts/fork/RESULTS-2026-11-05.md): a 1 h cooldown after
a veto or a settle, a guardian review between voting and execute, agent fee at
most 15%, WETH funds cannot propose, the bond comes back at once on a veto and
stays 14 days after a settle. Plus the observer list, pitch claims, ship-it,
chain reads and the leaderboard the Telegram bridge relies on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sherwood_monitor.fund import rules
from sherwood_monitor.fund.cards import bond_note, render_fund_card, render_receipt
from sherwood_monitor.fund.models import Agent, Leg
from sherwood_monitor.fund.rules import RuleViolation
from sherwood_monitor.fund.service import FundEvent, FundService, pitch_topic
from sherwood_monitor.fund.store import LocalJsonBackend
from tests.fund_helpers import MEMBER_ADDR, TARGET, TX, Clock, fund


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(tmp_path: Path, clock):
    s = FundService(LocalJsonBackend(tmp_path), clock=clock)
    s.create_fund(fund(), Agent(name="alpha", identity_id="101", address="0x" + "33" * 20))
    return s


@pytest.fixture
def events(svc):
    seen: list[FundEvent] = []
    svc.subscribe(seen.append)
    return seen


def legs():
    return [Leg(summary="buy NVDA", symbol="NVDA", weight_bps=6000, kind="swap", target=TARGET, data="0x1234", cap="300000000"),
            Leg(summary="buy AAPL", symbol="AAPL", weight_bps=4000, kind="swap", target=TARGET, data="0x1234", cap="200000000")]


def draft(svc, topic="NVDA AAPL pitch", agent="alpha"):
    svc.claim_topic("alpha-fund", agent, topic)
    return svc.draft_proposal(
        "alpha-fund", agent, topic=topic, thesis="AI capex", legs=legs(),
        max_capital=500_000_000, max_drawdown_bps=2000, strategy_duration=86400,
    ).proposal


def to_executed(svc, pid="p-1", onchain=7):
    svc.set_proposal_status("alpha-fund", "alpha", pid, "proposed", onchain_id=onchain)
    svc.set_proposal_status("alpha-fund", "alpha", pid, "voting")
    svc.set_proposal_status("alpha-fund", "alpha", pid, "review")
    svc.set_proposal_status("alpha-fund", "alpha", pid, "approved")
    svc.set_proposal_status("alpha-fund", "alpha", pid, "executed")


# -- (a) cooldown --------------------------------------------------------------------------
def test_veto_starts_the_cooldown_and_blocks_drafting_until_it_ends(svc, clock):
    draft(svc)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=1)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "vetoed")
    st = svc.load("alpha-fund")
    assert st.fund.cooldown_until == clock() + 3600
    with pytest.raises(RuleViolation) as exc:
        svc.claim_topic("alpha-fund", "alpha", "TSLA")
    assert exc.value.code == "cooldown" and "CooldownNotElapsed" in exc.value.reason
    card = render_fund_card(st, now=clock())
    assert "Next proposal possible at" in card.text
    clock.advance(3600)
    assert svc.claim_topic("alpha-fund", "alpha", "TSLA").topic == "TSLA"


def test_settle_starts_the_cooldown_too(svc, clock):
    draft(svc)
    to_executed(svc)
    svc.record_settle("alpha-fund", "alpha", "p-1", pnl=41_000_000, pnl_bps=820, tx_hash=TX)
    assert svc.load("alpha-fund").fund.cooldown_until == clock() + 3600
    verdict = rules.check_draft(svc.load("alpha-fund"), "alpha", "x", max_capital=1, max_drawdown_bps=0, legs=legs(), now=clock())
    assert verdict.code == "cooldown"


# -- (b) guardian review between voting and execute ------------------------------------
def test_review_and_approved_sit_between_voting_and_executed(svc):
    draft(svc)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=2)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "voting")
    r = svc.set_proposal_status("alpha-fund", "alpha", "p-1", "review")
    assert r.review_ends_at is not None and r.is_open
    with pytest.raises(RuleViolation):
        svc.set_proposal_status("alpha-fund", "alpha", "p-1", "executed")  # needs the guardian Approve
    assert svc.set_proposal_status("alpha-fund", "alpha", "p-1", "approved").status == "approved"
    assert svc.set_proposal_status("alpha-fund", "alpha", "p-1", "executed").status == "executed"


def test_chain_card_sets_against_windows_and_guardian_coverage(svc, events):
    draft(svc)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=2)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "voting")
    # Shape from sherwood docs/tx-builder.md (PR #739): GET funds/<vault>/tg-card data.
    card = {"success": True, "data": {
        "governor": {"cooldownPeriod": "3600", "vetoThresholdBps": "2000"},
        "proposeGate": {"openProposals": 1, "cooldownUntil": "1800009000", "ready": False},
        "proposals": {"open": [{"id": "2", "state": "GuardianReview", "againstBps": 500,
                                 "voteEnd": "1800001800", "reviewEnd": "1800003600",
                                 "review": {"opened": True, "resolved": False, "blocked": False,
                                            "coverage": {"kind": "full", "deployBps": 10000}}}],
                      "recent": [{"id": "99", "againstBps": 9}]}}}
    assert svc.apply_chain_card("alpha-fund", card) == 1
    st = svc.load("alpha-fund")
    p = st.proposal("2")
    assert (p.against_bps, p.voting_ends_at, p.review_ends_at, p.guardian_coverage) == (500, 1800001800.0, 1800003600.0, "full")
    assert p.status == "review" and st.fund.cooldown_until == 1800009000.0  # chain state + proposeGate win
    assert [e.kind for e in events[-2:]] == ["tally", "status"] and events[-1].data == {"source": "chain"}
    n = len(events)
    svc.apply_chain_card("alpha-fund", card)  # nothing changed: no event
    assert len(events) == n
    card["data"]["proposals"]["open"][0]["state"] = "Rejected"
    svc.apply_chain_card("alpha-fund", card)
    assert svc.load("alpha-fund").proposal("2").status == "vetoed"


# -- (c) agent fee -------------------------------------------------------------------------
def test_agent_fee_above_15_percent_is_refused(tmp_path, clock):
    s = FundService(LocalJsonBackend(tmp_path / "x"), clock=clock)
    with pytest.raises(RuleViolation) as exc:
        s.create_fund(fund(agent_fee_bps=2000), Agent(name="alpha", identity_id="1", address="0x" + "33" * 20))
    assert exc.value.code == "agent_fee_too_high" and "EXCESSIVE_PERFORMANCE_FEE" in exc.value.reason
    assert s.create_fund(fund(agent_fee_bps=1500), Agent(name="alpha", identity_id="1", address="0x" + "33" * 20))
    assert rules.check_agent_fee(500).ok and not rules.check_agent_fee(1501).ok


def test_draft_refused_when_the_recorded_fee_is_too_high(svc, clock):
    svc.claim_topic("alpha-fund", "alpha", "NVDA")
    svc.backend.update("alpha-fund", lambda st: setattr(st.fund, "agent_fee_bps", 2000))
    v = rules.check_draft(svc.load("alpha-fund"), "alpha", "NVDA", max_capital=1, max_drawdown_bps=0, legs=legs(), now=clock())
    assert v.code == "agent_fee_too_high"


# -- (d) WETH funds cannot propose -------------------------------------------------------
def test_weth_fund_cannot_claim_or_draft(tmp_path, clock):
    s = FundService(LocalJsonBackend(tmp_path / "w"), clock=clock)
    s.create_fund(fund(asset="WETH"), Agent(name="alpha", identity_id="1", address="0x" + "33" * 20))
    with pytest.raises(RuleViolation) as exc:
        s.claim_topic("alpha-fund", "alpha", "NVDA")
    assert exc.value.code == "asset_not_usd_pegged" and "AssetNotUsdPegged" in exc.value.reason
    assert "proposals cannot" in render_fund_card(s.load("alpha-fund"), now=clock()).text


# -- (e) bond after veto / settle --------------------------------------------------------
def test_receipt_says_the_bond_is_locked_for_the_challenge_window(svc):
    draft(svc)
    to_executed(svc)
    receipt = svc.record_settle("alpha-fund", "alpha", "p-1", pnl=41_000_000, pnl_bps=820, tx_hash=TX)
    text = render_receipt(svc.load("alpha-fund"), receipt)
    assert "locked in escrow for the 14-day challenge window" in text
    assert "returned to the agent at once" in bond_note("vetoed")


# -- Telegram wiring surface ---------------------------------------------------------------
def test_pitch_topic():
    assert pitch_topic("@bot long $nvda and $AAPL into earnings") == "NVDA AAPL pitch"
    assert pitch_topic("@bot rotate into semis now please") == "rotate into semis now please"


def test_request_draft_claims_the_topic_and_the_draft_inherits_idea_by(svc, events):
    claim = svc.request_draft("alpha-fund", "alpha", idea_by="77", pitch="long $NVDA $AAPL", chat_id="-100500", source_message_id=41)
    assert (claim.topic, claim.idea_by, claim.source_message_id) == ("NVDA AAPL pitch", "77", 41)
    assert events[-1].kind == "draft_requested"
    record = draft_after_claim(svc, claim.topic)
    assert record.idea_by == "77" and record.pitch_message_id == 41
    assert events[-1].kind == "drafted" and events[-1].proposal.id == record.id
    prepare = events[-1].data["prepare"]  # symbol + weight legs map to prepare/propose-portfolio
    assert prepare["route"] == "propose-portfolio" and prepare["ready"]
    assert prepare["body"]["legs"] == [{"weightBps": 6000, "symbol": "NVDA"}, {"weightBps": 4000, "symbol": "AAPL"}]
    assert prepare["body"]["maxCapital"] == "500000000" and prepare["body"]["proposer"] == "0x" + "33" * 20
    # The pitch claim reserves the topic like any claim.
    with pytest.raises(RuleViolation):
        svc.request_draft("alpha-fund", "alpha", idea_by="78", pitch="short $TSLA")


def draft_after_claim(svc, topic):
    return svc.draft_proposal(
        "alpha-fund", "alpha", topic=topic, thesis="AI capex", legs=legs(),
        max_capital=500_000_000, max_drawdown_bps=2000, strategy_duration=86400,
    ).proposal


def test_ship_draft_is_host_only_and_needs_a_built_draft(svc, events):
    record = draft(svc)
    with pytest.raises(RuleViolation):
        svc.ship_draft("alpha-fund", record.id, caller_telegram_id="12345")
    shipped = svc.ship_draft("alpha-fund", record.id, caller_telegram_id="900")
    assert shipped.shipped_at is not None and shipped.shipped_by == "900"
    assert events[-1].kind == "shipped"
    svc.set_proposal_status("alpha-fund", "alpha", record.id, "proposed", onchain_id=3)
    with pytest.raises(RuleViolation):
        svc.ship_draft("alpha-fund", record.id, caller_telegram_id="900")


def test_vote_clear_and_events(svc, events):
    svc.request_membership("alpha-fund", "77", address=MEMBER_ADDR)
    svc.approve_member("alpha-fund", "alpha", "77")
    assert [e.kind for e in events[-2:]] == ["member_requested", "member_approved"]
    draft(svc)
    svc.set_proposal_status("alpha-fund", "alpha", "p-1", "proposed", onchain_id=4)
    assert events[-1].kind == "status" and events[-1].old_status == "draft"
    assert svc.record_vote("alpha-fund", "p-1", voter="77", side="back").backers == ["77"]
    assert svc.record_vote("alpha-fund", "p-1", voter="77", side="clear").backers == []
    assert events[-1].kind == "vote" and events[-1].data["side"] == "clear"


def test_a_failing_observer_never_fails_the_write(svc):
    def boom(_event):
        raise RuntimeError("observer down")

    svc.subscribe(boom)
    assert svc.request_membership("alpha-fund", "77").telegram_id == "77"


def test_leaderboard_ranks_members_by_settled_idea_pnl(svc, clock):
    svc.request_draft("alpha-fund", "alpha", idea_by="77", pitch="$NVDA")
    draft_after_claim(svc, "NVDA pitch")
    to_executed(svc)
    svc.record_settle("alpha-fund", "alpha", "p-1", pnl=41_000_000, pnl_bps=820, tx_hash=TX)
    assert svc.leaderboard("alpha-fund") == [{"tg_id": "77", "pnl_bps": 820, "ideas": 1, "wins": 1}]
