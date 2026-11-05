"""Coordination rules for a group-chat fund. Pure functions, no I/O.

Telegram bots never see each other's messages, so agents coordinate only
through the shared fund state. These checks are the whole protocol:

- one open proposal per agent; at most two open per fund (drafts count);
- claim a topic before drafting it ("drafting X"), claims expire after 10 min;
- any registered agent may review another agent's proposal, never its own;
- every agent bonds its own WOOD: bond = 1% of coverage, approve quote + 5%,
  never unlimited, top up a WOOD shortfall first, re-quote on >5% drift;
- the host removes an agent with one action; the host cannot be removed;
- optimistic veto: only Against counts; vetoed once Against >= 20% of supply;
- the governor's cooldown: no new proposal for 1 h after a veto or a settle;
- PortfolioStrategy proposes only on a USD-pegged asset (USDG), and the guardian
  review refuses an agent fee near the 20% cap, so the fund keeps it <= 15%.

Every check takes `now` explicitly and returns a `Verdict`; `require()` turns a
failed verdict into a `RuleViolation` for callers that prefer exceptions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .models import (
    CLAIM_TTL_SECONDS,
    MAX_AGENT_FEE_BPS,
    MAX_OPEN_PER_AGENT,
    MAX_OPEN_PER_FUND,
    OPEN_STATUSES,
    PROPOSABLE_ASSETS,
    Agent,
    BondPolicy,
    Claim,
    FundState,
    Leg,
    Member,
    ProposalRecord,
)
from .units import is_unlimited

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]*$")
BPS = 10_000


@dataclass(frozen=True)
class Verdict:
    ok: bool
    code: str = ""
    reason: str = ""


ALLOW = Verdict(True)


def deny(code: str, reason: str) -> Verdict:
    return Verdict(False, code, reason)


class RuleViolation(Exception):
    def __init__(self, verdict: Verdict) -> None:
        super().__init__(verdict.reason)
        self.code = verdict.code
        self.reason = verdict.reason


def require(verdict: Verdict) -> None:
    if not verdict.ok:
        raise RuleViolation(verdict)


def first_failure(verdicts: Iterable[Verdict]) -> Verdict:
    for v in verdicts:
        if not v.ok:
            return v
    return ALLOW


def is_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value or ""))


# -- topics and claims -----------------------------------------------------------
def normalize_topic(topic: str) -> str:
    """"  TSLA  Momentum!! " -> "tsla momentum". Claims compare normalized topics."""
    text = re.sub(r"[^a-z0-9$#./+\- ]+", " ", (topic or "").lower())
    return re.sub(r"\s+", " ", text).strip()[:80]


def claim_expires_at(now: float, ttl: int = CLAIM_TTL_SECONDS) -> float:
    return now + ttl


def active_claims(state: FundState, now: float) -> list[Claim]:
    """Claims still alive at `now` (a claim is dead at exactly `expires_at`)."""
    return [c for c in state.claims if c.expires_at > now]


def claim_of(state: FundState, agent: str, now: float) -> Claim | None:
    key = agent.lower()
    for c in active_claims(state, now):
        if c.agent.lower() == key:
            return c
    return None


def claim_for_topic(state: FundState, topic: str, now: float) -> Claim | None:
    key = normalize_topic(topic)
    for c in active_claims(state, now):
        if normalize_topic(c.topic) == key:
            return c
    return None


def open_proposals_of(state: FundState, agent: str) -> list[ProposalRecord]:
    key = agent.lower()
    return [p for p in state.proposals if p.status in OPEN_STATUSES and p.proposer_agent.lower() == key]


# -- who may act ---------------------------------------------------------------------
def check_agent_can_act(state: FundState, agent_name: str, *, need_registered: bool = True) -> Verdict:
    agent = state.agent(agent_name)
    if agent is None:
        return deny("unknown_agent", f"agent {agent_name!r} is not part of fund {state.fund.name!r}")
    if agent.removed:
        return deny("agent_removed", f"agent {agent.name!r} was removed by the host")
    if need_registered and not agent.registered:
        return deny(
            "agent_not_registered",
            f"agent {agent.name!r} is not registered on the vault yet; the host must approve it "
            "(sign registerAgent) first",
        )
    return ALLOW


def check_host_action(state: FundState, actor: str) -> Verdict:
    agent = state.agent(actor)
    if agent is None or agent.removed or agent.role != "host":
        return deny("not_host", f"only the host agent of {state.fund.name!r} can do that")
    return ALLOW


HOST_ID_UNSET = (
    "this fund has no host Telegram id, so nobody may do that from chat. The operator sets it: "
    "`hermes sherwood fund-bind <fund> --host-telegram-id <id>`"
)


def check_caller_is_host(state: FundState, caller_telegram_id: str | None) -> Verdict:
    """When the chat tells us who typed a host command, it must be the host.

    `None` (CLI, cron, or a platform that does not say) passes: the command
    then runs with the local operator's authority. The on-chain step still
    needs the host wallet's signature either way. A caller with no host id on
    record is refused (fail closed): unset means nobody.
    """
    if caller_telegram_id is None:
        return ALLOW
    if not state.fund.host_telegram_id:
        return deny("host_unset", HOST_ID_UNSET)
    if str(caller_telegram_id) != str(state.fund.host_telegram_id):
        return deny("not_host", "only the fund host can approve or remove members and agents")
    return ALLOW


# -- fund-level gates (fork run 2026-11-05) -----------------------------------------------
def check_cooldown(state: FundState, now: float) -> Verdict:
    """The governor reverts CooldownNotElapsed for 1 h after a veto or a settle."""
    until = state.fund.cooldown_until
    if until is not None and now < until:
        return deny(
            "cooldown",
            f"the governor's cooldown after the last veto/settle runs until {_hhmm(until)} UTC; "
            "no new proposal before then (CooldownNotElapsed)",
        )
    return ALLOW


def check_proposable_asset(state: FundState) -> Verdict:
    asset = (state.fund.asset or "").upper()
    if asset not in PROPOSABLE_ASSETS:
        return deny(
            "asset_not_usd_pegged",
            f"{asset} funds cannot propose: PortfolioStrategy needs a USD-pegged asset "
            "(AssetNotUsdPegged; the bond quote reverts FeedNotConfigured). Use a USDG fund.",
        )
    return ALLOW


def check_agent_fee(fee_bps: int | None) -> Verdict:
    if fee_bps is None:
        return ALLOW
    if not isinstance(fee_bps, int) or fee_bps < 0:
        return deny("bad_agent_fee", "agent fee must be a non-negative integer number of bps")
    if fee_bps > MAX_AGENT_FEE_BPS:
        return deny(
            "agent_fee_too_high",
            f"agent fee {fee_bps} bps is above {MAX_AGENT_FEE_BPS} bps: the guardian review blocks "
            "Approve near the 20% cap (EXCESSIVE_PERFORMANCE_FEE), so execute would never pass",
        )
    return ALLOW


def check_fee_recorded(state: FundState) -> Verdict:
    """Drafting needs the vault's agent fee on record, and within the cap (never assumed)."""
    if state.fund.agent_fee_bps is None:
        return deny(
            "agent_fee_unknown",
            "the vault's agent fee is not on record; set it (<= 1500 bps, 500 recommended) with "
            "prepare/set-agent-fee and record it with fund_setup agent_fee_bps, or let the tg-card read fill it",
        )
    return check_agent_fee(state.fund.agent_fee_bps)


def _hhmm(ts: float) -> str:
    import time as _time

    return _time.strftime("%H:%M", _time.gmtime(ts))


# -- claims ------------------------------------------------------------------------------
def check_claim(
    state: FundState,
    agent: str,
    topic: str,
    now: float,
    *,
    max_open: int = MAX_OPEN_PER_FUND,
    max_open_per_agent: int = MAX_OPEN_PER_AGENT,
) -> Verdict:
    v = first_failure([
        check_agent_can_act(state, agent), check_proposable_asset(state), check_fee_recorded(state), check_cooldown(state, now),
    ])
    if not v.ok:
        return v
    key = normalize_topic(topic)
    if not key:
        return deny("bad_topic", "say what you are drafting (a topic like 'TSLA momentum')")
    if len(open_proposals_of(state, agent)) >= max_open_per_agent:
        return deny(
            "agent_has_open_proposal",
            f"{agent} already has an open proposal; settle, veto or cancel it before claiming another",
        )
    holder = claim_for_topic(state, key, now)
    if holder is not None and holder.agent.lower() != agent.lower():
        return deny("topic_claimed", f"{holder.agent} is already drafting {holder.topic!r}")
    mine = claim_of(state, agent, now)
    if mine is not None and normalize_topic(mine.topic) != key:
        return deny(
            "agent_has_claim",
            f"{agent} is already drafting {mine.topic!r}; release that claim first",
        )
    if mine is None:
        others = [c for c in active_claims(state, now) if c.agent.lower() != agent.lower()]
        used = len(state.open_proposals()) + len(others)
        if used >= max_open:
            return deny(
                "fund_full",
                f"fund {state.fund.name!r} already has {used} open proposals or claims (max {max_open})",
            )
    return ALLOW


# -- drafts --------------------------------------------------------------------------------
def check_legs(legs: list[Leg], max_capital: int) -> Verdict:
    if not legs:
        return deny("no_legs", "a proposal needs at least one leg")
    execute_caps = 0
    settle_caps = 0
    for i, leg in enumerate(legs):
        if leg.phase not in ("execute", "settle"):
            return deny("bad_leg", f"leg {i}: phase must be execute or settle")
        if leg.kind == "approve" and leg.amount and is_unlimited(leg.amount):
            return deny("unlimited_approve", f"leg {i}: unlimited approvals are never allowed")
        if leg.target or leg.data:
            if not is_address(leg.target):
                return deny("bad_leg", f"leg {i}: target must be a 0x address")
            if not _HEX_RE.match(leg.data or ""):
                return deny("bad_leg", f"leg {i}: data must be 0x hex calldata")
            if is_unlimited(_approve_amount_from_calldata(leg.data)):
                return deny("unlimited_approve", f"leg {i}: unlimited approvals are never allowed")
        try:
            cap = int(leg.cap or "0")
        except ValueError:
            return deny("bad_leg", f"leg {i}: cap must be an integer of asset base units")
        if cap < 0:
            return deny("bad_leg", f"leg {i}: cap must not be negative")
        if leg.phase == "execute":
            execute_caps += cap
        else:
            settle_caps += cap
    if execute_caps > max_capital:
        return deny("caps_exceed_capital", f"execute leg caps sum to {execute_caps}, above max capital {max_capital}")
    if settle_caps > max_capital:
        return deny("caps_exceed_capital", f"settle leg caps sum to {settle_caps}, above max capital {max_capital}")
    return ALLOW


def _approve_amount_from_calldata(data: str) -> int:
    """ERC-20 approve(address,uint256) amount, or 0 when the calldata is not one."""
    raw = (data or "").lower()
    if not raw.startswith("0x095ea7b3") or len(raw) < 10 + 128:
        return 0
    return int(raw[10 + 64 : 10 + 128], 16)


def check_envelope(state: FundState, max_capital: int, max_drawdown_bps: int) -> Verdict:
    fund = state.fund
    if max_capital <= 0:
        return deny("bad_capital", "max capital must be above zero")
    if fund.max_capital is not None and max_capital > int(fund.max_capital):
        return deny(
            "exceeds_max_capital",
            f"max capital {max_capital} is above the fund ceiling {fund.max_capital} (asset base units)",
        )
    if not isinstance(max_drawdown_bps, int) or not 0 <= max_drawdown_bps <= BPS:
        return deny("bad_drawdown", "max drawdown must be an integer number of bps in [0, 10000]")
    if fund.max_drawdown_bps is not None and max_drawdown_bps > fund.max_drawdown_bps:
        return deny(
            "exceeds_max_drawdown",
            f"max drawdown {max_drawdown_bps} bps is above the fund ceiling {fund.max_drawdown_bps} bps",
        )
    return ALLOW


def check_draft(
    state: FundState,
    agent: str,
    topic: str,
    *,
    max_capital: int,
    max_drawdown_bps: int,
    legs: list[Leg],
    now: float,
    max_open: int = MAX_OPEN_PER_FUND,
    max_open_per_agent: int = MAX_OPEN_PER_AGENT,
) -> Verdict:
    """Everything that must hold before a draft is recorded or calldata is built."""
    v = first_failure([
        check_agent_can_act(state, agent),
        check_proposable_asset(state),
        check_fee_recorded(state),
        check_cooldown(state, now),
    ])
    if not v.ok:
        return v
    if len(open_proposals_of(state, agent)) >= max_open_per_agent:
        return deny("agent_has_open_proposal", f"{agent} already has an open proposal")
    if len(state.open_proposals()) >= max_open:
        return deny("fund_full", f"fund {state.fund.name!r} already has {max_open} open proposals")
    claim = claim_of(state, agent, now)
    if claim is None:
        return deny("no_claim", f"claim the topic first (fund_claim_topic) so other agents know {agent} is drafting it")
    if normalize_topic(claim.topic) != normalize_topic(topic):
        return deny("claim_mismatch", f"{agent} claimed {claim.topic!r}, not {topic!r}")
    return first_failure(
        [check_envelope(state, max_capital, max_drawdown_bps), check_legs(legs, max_capital)]
    )


# -- votes and reviews -----------------------------------------------------------------
VOTABLE_STATUSES = frozenset({"proposed", "voting"})
REVIEWABLE_STATUSES = frozenset({"draft", "proposed", "voting"})


def check_review(state: FundState, agent: str, proposal: ProposalRecord | None) -> Verdict:
    v = check_agent_can_act(state, agent)
    if not v.ok:
        return v
    if proposal is None:
        return deny("unknown_proposal", "no such proposal")
    if proposal.proposer_agent.lower() == agent.lower():
        return deny("own_proposal", "an agent reviews other agents' proposals, not its own")
    if proposal.status not in REVIEWABLE_STATUSES:
        return deny("not_reviewable", f"proposal {proposal.id} is {proposal.status}")
    return ALLOW


def check_member_vote(
    state: FundState, member: Member | None, proposal: ProposalRecord | None, now: float
) -> Verdict:
    if member is None or not member.approved:
        return deny("not_member", "only approved members can back or object")
    if proposal is None:
        return deny("unknown_proposal", "no such proposal")
    if proposal.status not in VOTABLE_STATUSES:
        return deny("voting_closed", f"proposal {proposal.id} is {proposal.status}, not open for votes")
    if proposal.voting_ends_at is not None and now >= proposal.voting_ends_at:
        return deny("voting_closed", f"voting on proposal {proposal.id} has ended")
    return ALLOW


def veto_threshold_shares(total_supply: int, threshold_bps: int) -> int:
    """Smallest Against share count that vetoes (ceil(supply * bps / 10000))."""
    return -(-total_supply * threshold_bps // BPS)


def is_vetoed(against_shares: int, total_supply: int, threshold_bps: int) -> bool:
    """Optimistic veto: only Against counts. Passes unless Against >= threshold."""
    if total_supply <= 0:
        return False
    return against_shares * BPS >= total_supply * threshold_bps


# -- lifecycle ---------------------------------------------------------------------------
# voting -> review (guardian review window) -> approved (guardian Approve with
# enough coverage) -> executed. A guardian block in review reads as vetoed.
TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"proposed", "cancelled"}),
    "proposed": frozenset({"voting", "passed", "review", "vetoed", "cancelled"}),
    "voting": frozenset({"passed", "review", "vetoed", "cancelled"}),
    "passed": frozenset({"review", "approved", "executed", "cancelled"}),
    "review": frozenset({"approved", "vetoed", "cancelled"}),
    "approved": frozenset({"executed", "cancelled"}),
    "executed": frozenset({"settled"}),
}


def check_transition(old: str, new: str) -> Verdict:
    if new not in TRANSITIONS.get(old, frozenset()):
        return deny("bad_transition", f"a proposal cannot go from {old} to {new}")
    return ALLOW


# -- host actions ---------------------------------------------------------------------
def check_approve_agent(state: FundState, actor: str, target: Agent | None) -> Verdict:
    v = check_host_action(state, actor)
    if not v.ok:
        return v
    if target is None:
        return deny("unknown_agent", "no such agent has asked to join")
    if target.removed:
        return deny("agent_removed", f"{target.name} was removed; it must /join-fund again")
    if not target.identity_id or not is_address(target.address):
        return deny("missing_identity", f"{target.name} needs an ERC-8004 identity id and a wallet address")
    return ALLOW


def check_remove_agent(state: FundState, actor: str, target: Agent | None) -> Verdict:
    v = check_host_action(state, actor)
    if not v.ok:
        return v
    if target is None:
        return deny("unknown_agent", "no such agent")
    if target.removed:
        return deny("agent_removed", f"{target.name} is already removed")
    if target.role == "host":
        return deny("cannot_remove_host", "the host agent cannot be removed")
    return ALLOW


def check_approve_member(state: FundState, actor: str, member: Member | None) -> Verdict:
    v = check_host_action(state, actor)
    if not v.ok:
        return v
    if member is None:
        return deny("unknown_member", "no such member; they must ask to join first (or pass an address)")
    if member.approved:
        return deny("already_approved", f"member {member.telegram_id or member.address} is already approved")
    if not is_address(member.address):
        return deny("missing_address", "approving a depositor needs their wallet address")
    return ALLOW


# -- bond ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class BondPlan:
    quote: int  # WOOD base units the governor asks for
    approve_amount: int  # quote + buffer, never unlimited
    wood_balance: int
    gap: int  # WOOD to buy first (0 when the balance covers the approval)

    @property
    def swap_first(self) -> bool:
        return self.gap > 0


def required_bond_value(max_capital: int, policy: BondPolicy | None = None) -> int:
    """Bond = 1% of coverage, in asset base units (ceil). Convert to WOOD with a live quote."""
    bps = (policy or BondPolicy()).bond_bps
    return -(-max_capital * bps // BPS)


def approve_amount_for(quote: int, policy: BondPolicy | None = None) -> int:
    """Quote + 5% (ceil). The only allowance an agent ever grants for its bond."""
    buffer_bps = (policy or BondPolicy()).approve_buffer_bps
    return -(-quote * (BPS + buffer_bps) // BPS)


def plan_bond(quote: int, wood_balance: int, policy: BondPolicy | None = None) -> BondPlan:
    approve = approve_amount_for(quote, policy)
    return BondPlan(quote=quote, approve_amount=approve, wood_balance=wood_balance, gap=max(0, approve - wood_balance))


def needs_requote(quoted_price: int, current_price: int, policy: BondPolicy | None = None) -> bool:
    """True when the WOOD price moved more than the drift limit since the quote.

    Never widen the approval to absorb drift: re-quote and recompute instead.
    """
    if quoted_price <= 0:
        return True
    drift_bps = (policy or BondPolicy()).requote_drift_bps
    return abs(current_price - quoted_price) * BPS > quoted_price * drift_bps


def check_bond_approve(amount: int | str, quote: int, policy: BondPolicy | None = None) -> Verdict:
    if is_unlimited(amount):
        return deny("unlimited_approve", "the bond approval must be exact: quote + 5%, never unlimited")
    value = int(amount)
    if value < quote:
        return deny("approve_too_small", f"approval {value} is below the bond quote {quote}")
    ceiling = approve_amount_for(quote, policy)
    if value > ceiling:
        return deny("approve_too_wide", f"approval {value} is above quote + buffer ({ceiling}); re-quote, never widen")
    return ALLOW
