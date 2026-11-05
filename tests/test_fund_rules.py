"""Coordination rules: every boundary the shared fund state depends on."""
from __future__ import annotations

import pytest

from sherwood_monitor.fund import rules
from sherwood_monitor.fund.models import CLAIM_TTL_SECONDS, BondPolicy, Claim, Leg
from sherwood_monitor.fund.units import UINT256_MAX, UnitsError, format_units, is_unlimited, to_base_units
from tests.fund_helpers import T0, TARGET, legs, open_proposal, state


def claim(agent: str, topic: str, expires: float = T0 + CLAIM_TTL_SECONDS) -> Claim:
    return Claim(agent=agent, topic=topic, expires_at=expires, created_at=T0)


# -- topics + claims -----------------------------------------------------------------
def test_normalize_topic():
    assert rules.normalize_topic("  TSLA   Momentum!! ") == "tsla momentum"
    assert rules.normalize_topic("$NVDA earnings") == "$nvda earnings"
    assert rules.normalize_topic("") == ""


def test_claim_ok_for_registered_agent():
    assert rules.check_claim(state(), "beta", "TSLA momentum", T0).ok


@pytest.mark.parametrize("agent,code", [("nobody", "unknown_agent")])
def test_claim_unknown_agent(agent, code):
    assert rules.check_claim(state(), agent, "x", T0).code == code


def test_claim_needs_registered_agent():
    s = state()
    s.agent("beta").registered = False
    assert rules.check_claim(s, "beta", "x", T0).code == "agent_not_registered"


def test_claim_removed_agent():
    s = state()
    s.agent("beta").removed = True
    assert rules.check_claim(s, "beta", "x", T0).code == "agent_removed"


def test_claim_empty_topic():
    assert rules.check_claim(state(), "beta", " !! ", T0).code == "bad_topic"


def test_topic_held_by_other_agent_is_refused_until_expiry():
    s = state()
    s.claims.append(claim("gamma", "TSLA Momentum", expires=T0 + 600))
    assert rules.check_claim(s, "beta", "tsla momentum", T0 + 599).code == "topic_claimed"
    # A claim is dead at exactly expires_at.
    assert rules.check_claim(s, "beta", "tsla momentum", T0 + 600).ok


def test_same_agent_same_topic_refresh_allowed():
    s = state()
    s.claims.append(claim("beta", "TSLA momentum"))
    assert rules.check_claim(s, "beta", "tsla  momentum", T0).ok


def test_one_claim_per_agent():
    s = state()
    s.claims.append(claim("beta", "TSLA momentum"))
    assert rules.check_claim(s, "beta", "NVDA", T0).code == "agent_has_claim"


def test_agent_with_open_proposal_cannot_claim():
    s = state()
    s.proposals.append(open_proposal("p-1", "beta", status="draft"))
    assert rules.check_claim(s, "beta", "NVDA", T0).code == "agent_has_open_proposal"


def test_fund_full_counts_open_proposals_and_other_claims():
    s = state()
    s.proposals.append(open_proposal("p-1", "alpha"))
    s.claims.append(claim("gamma", "NVDA"))
    assert rules.check_claim(s, "beta", "TSLA", T0).code == "fund_full"
    # Once gamma's claim expires there is room again.
    assert rules.check_claim(s, "beta", "TSLA", T0 + CLAIM_TTL_SECONDS).ok


def test_terminal_proposals_free_slots():
    s = state()
    for status in ("settled", "vetoed", "cancelled"):
        s.proposals.append(open_proposal(f"p-{status}", "alpha", status=status))
    assert rules.check_claim(s, "beta", "TSLA", T0).ok


# -- drafts ------------------------------------------------------------------------------
def draft(s, agent="beta", topic="TSLA momentum", **kw):
    args = dict(max_capital=500_000_000, max_drawdown_bps=1000, legs=legs(), now=T0)
    args.update(kw)
    return rules.check_draft(s, agent, topic, **args)


def with_claim(agent="beta", topic="TSLA momentum"):
    s = state()
    s.claims.append(claim(agent, topic))
    return s


def test_draft_ok():
    assert draft(with_claim()).ok


def test_draft_without_claim():
    assert draft(state()).code == "no_claim"


def test_draft_with_expired_claim():
    assert draft(with_claim(), now=T0 + CLAIM_TTL_SECONDS).code == "no_claim"


def test_draft_claim_for_other_topic():
    assert draft(with_claim(topic="NVDA"), topic="TSLA").code == "claim_mismatch"


def test_draft_one_open_per_agent_counts_drafts():
    s = with_claim()
    s.proposals.append(open_proposal("p-1", "beta", status="draft"))
    assert draft(s).code == "agent_has_open_proposal"


def test_draft_max_two_open_per_fund():
    s = with_claim()
    s.proposals += [open_proposal("p-1", "alpha", "voting"), open_proposal("p-2", "gamma", "draft")]
    assert draft(s).code == "fund_full"


def test_draft_unregistered_agent_blocked():
    s = with_claim()
    s.agent("beta").registered = False
    assert draft(s).code == "agent_not_registered"


@pytest.mark.parametrize("cap,code", [(0, "bad_capital"), (1_000_000_001, "exceeds_max_capital")])
def test_draft_capital_bounds(cap, code):
    assert draft(with_claim(), max_capital=cap).code == code


def test_draft_capital_at_ceiling_ok():
    assert draft(with_claim(), max_capital=1_000_000_000).ok


@pytest.mark.parametrize("bps,code", [(-1, "bad_drawdown"), (10_001, "bad_drawdown"), (2001, "exceeds_max_drawdown")])
def test_draft_drawdown_bounds(bps, code):
    assert draft(with_claim(), max_drawdown_bps=bps).code == code


def test_draft_no_fund_ceiling_means_no_plugin_cap():
    s = with_claim()
    s.fund.max_capital = None
    s.fund.max_drawdown_bps = None
    assert draft(s, max_capital=10**30, max_drawdown_bps=10_000, legs=[Leg(summary="x")]).ok


def test_draft_needs_legs():
    assert draft(with_claim(), legs=[]).code == "no_legs"


def test_unlimited_approve_amount_blocked():
    bad = [Leg(summary="approve TSLA", kind="approve", amount="max")]
    assert draft(with_claim(), legs=bad).code == "unlimited_approve"
    bad = [Leg(summary="approve", kind="approve", amount=str(UINT256_MAX))]
    assert draft(with_claim(), legs=bad).code == "unlimited_approve"


def test_unlimited_approve_calldata_blocked_even_when_kind_unset():
    data = "0x095ea7b3" + "00" * 12 + "77" * 20 + "ff" * 32
    assert draft(with_claim(), legs=[Leg(summary="x", target=TARGET, data=data)]).code == "unlimited_approve"


def test_bounded_approve_calldata_ok():
    data = "0x095ea7b3" + "00" * 12 + "77" * 20 + format(1_000_000, "064x")
    assert draft(with_claim(), legs=[Leg(summary="x", kind="approve", target=TARGET, data=data)]).ok


def test_leg_bad_target_or_data():
    assert draft(with_claim(), legs=[Leg(summary="x", target="0x12", data="0x")]).code == "bad_leg"
    assert draft(with_claim(), legs=[Leg(summary="x", target=TARGET, data="zz")]).code == "bad_leg"
    assert draft(with_claim(), legs=[Leg(summary="x", phase="later")]).code == "bad_leg"


def test_leg_caps_cannot_exceed_max_capital():
    two = [Leg(summary="a", cap="300000000"), Leg(summary="b", cap="300000000")]
    assert draft(with_claim(), legs=two, max_capital=500_000_000).code == "caps_exceed_capital"
    assert draft(with_claim(), legs=two, max_capital=600_000_000).ok


# -- reviews + votes --------------------------------------------------------------------
def test_review_other_agents_proposal_only():
    s = state()
    p = open_proposal("p-1", "beta", "voting")
    s.proposals.append(p)
    assert rules.check_review(s, "gamma", p).ok
    assert rules.check_review(s, "beta", p).code == "own_proposal"
    assert rules.check_review(s, "gamma", None).code == "unknown_proposal"
    p.status = "settled"
    assert rules.check_review(s, "gamma", p).code == "not_reviewable"


def test_member_vote_rules():
    s = state()
    p = open_proposal("p-1", "beta", "voting")
    p.voting_ends_at = T0 + 1800
    approved, waiting = s.member("1"), s.member("2")
    assert rules.check_member_vote(s, approved, p, T0).ok
    assert rules.check_member_vote(s, waiting, p, T0).code == "not_member"
    assert rules.check_member_vote(s, None, p, T0).code == "not_member"
    assert rules.check_member_vote(s, approved, p, T0 + 1800).code == "voting_closed"
    p.status = "draft"
    assert rules.check_member_vote(s, approved, p, T0).code == "voting_closed"


def test_member_lookup_by_address_case_insensitive():
    s = state()
    assert s.member(s.members[0].address.upper().replace("0X", "0x")) is s.members[0]


@pytest.mark.parametrize(
    "against,supply,vetoed",
    [(1999, 10_000, False), (2000, 10_000, True), (0, 0, False), (1, 3, True), (0, 3, False)],
)
def test_optimistic_veto_threshold(against, supply, vetoed):
    assert rules.is_vetoed(against, supply, 2000) is vetoed


def test_veto_threshold_shares_rounds_up():
    assert rules.veto_threshold_shares(10_000, 2000) == 2000
    assert rules.veto_threshold_shares(10_001, 2000) == 2001


# -- lifecycle + host actions ---------------------------------------------------------
@pytest.mark.parametrize(
    "old,new,ok",
    [("draft", "proposed", True), ("draft", "cancelled", True), ("proposed", "voting", True),
     ("voting", "vetoed", True), ("passed", "executed", True), ("executed", "settled", True),
     ("draft", "settled", False), ("settled", "proposed", False), ("vetoed", "passed", False)],
)
def test_transitions(old, new, ok):
    assert rules.check_transition(old, new).ok is ok


def test_host_only_actions():
    s = state()
    assert rules.check_remove_agent(s, "beta", s.agent("gamma")).code == "not_host"
    assert rules.check_remove_agent(s, "alpha", s.agent("alpha")).code == "cannot_remove_host"
    assert rules.check_remove_agent(s, "alpha", s.agent("gamma")).ok
    assert rules.check_remove_agent(s, "alpha", None).code == "unknown_agent"
    assert rules.check_approve_agent(s, "alpha", s.agent("beta")).ok
    s.agent("beta").identity_id = ""
    assert rules.check_approve_agent(s, "alpha", s.agent("beta")).code == "missing_identity"


def test_approve_member_rules():
    s = state()
    assert rules.check_approve_member(s, "alpha", s.member("1")).code == "already_approved"
    assert rules.check_approve_member(s, "alpha", s.member("2")).code == "missing_address"
    s.member("2").address = TARGET
    assert rules.check_approve_member(s, "alpha", s.member("2")).ok
    assert rules.check_approve_member(s, "beta", s.member("2")).code == "not_host"


def test_caller_is_host():
    s = state()
    assert rules.check_caller_is_host(s, None).ok
    assert rules.check_caller_is_host(s, "900").ok
    assert rules.check_caller_is_host(s, "1").code == "not_host"


# -- bond -----------------------------------------------------------------------------------
def test_bond_is_one_percent_of_coverage_rounded_up():
    assert rules.required_bond_value(500_000_000) == 5_000_000
    assert rules.required_bond_value(101) == 2


def test_approve_is_quote_plus_five_percent_ceil():
    assert rules.approve_amount_for(1000) == 1050
    assert rules.approve_amount_for(1001) == 1052  # 1051.05 -> ceil


def test_bond_plan_swaps_gap_first():
    plan = rules.plan_bond(1000, 900)
    assert (plan.approve_amount, plan.gap, plan.swap_first) == (1050, 150, True)
    assert not rules.plan_bond(1000, 1050).swap_first


def test_requote_on_drift_over_five_percent():
    assert not rules.needs_requote(10_000, 10_500)
    assert rules.needs_requote(10_000, 10_501)
    assert rules.needs_requote(10_000, 9_499)
    assert rules.needs_requote(0, 1)


def test_bond_approve_exact_never_unlimited_never_wider():
    assert rules.check_bond_approve(1050, 1000).ok
    assert rules.check_bond_approve(1051, 1000).code == "approve_too_wide"
    assert rules.check_bond_approve(999, 1000).code == "approve_too_small"
    assert rules.check_bond_approve("max", 1000).code == "unlimited_approve"
    assert rules.check_bond_approve(UINT256_MAX, 1000).code == "unlimited_approve"
    assert rules.check_bond_approve(1100, 1000, BondPolicy(approve_buffer_bps=1000)).ok


def test_require_raises_rule_violation():
    with pytest.raises(rules.RuleViolation) as exc:
        rules.require(rules.deny("x", "nope"))
    assert exc.value.code == "x"


# -- units -------------------------------------------------------------------------------------
def test_units_round_trip_and_guards():
    assert to_base_units("1.5", 6) == 1_500_000
    assert to_base_units("1000000.123456789012345678", 18) == 1_000_000_123_456_789_012_345_678
    assert format_units(1_500_000, 6) == "1.5"
    assert format_units(-2_500_000, 6) == "-2.5"
    assert to_base_units("-2.5", 6, allow_negative=True) == -2_500_000
    with pytest.raises(UnitsError):
        to_base_units("1.0000001", 6)
    with pytest.raises(UnitsError):
        to_base_units(1.5, 6)  # type: ignore[arg-type]
    with pytest.raises(UnitsError):
        to_base_units("-1", 6)
    assert is_unlimited("0x" + "f" * 64) and not is_unlimited("1000") and not is_unlimited("abc")
