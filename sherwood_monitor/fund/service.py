"""Fund operations: rules + store, one atomic update per action.

This is the surface other lanes call (the Telegram lane for buttons and
reactions, tools and slash commands here). Every write is a single
`backend.update()`, so a rule is checked against the same state it mutates,
under the file lock. Rule failures raise `RuleViolation` (code + reason);
nothing is written when one is raised.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import rules
from .models import (
    Agent,
    Claim,
    Fund,
    FundState,
    Leg,
    Member,
    ProposalRecord,
    Review,
    SettleReceipt,
)
from .rules import RuleViolation, require
from .store import FundNotFound, FundStateBackend, FundStoreError, validate_fund_name
from .txbuilder import DEFAULT_TX_BUILDER_URL, TxBuilderError, build_propose_body


class FundServiceError(Exception):
    pass


@dataclass
class DraftResult:
    proposal: ProposalRecord
    prepare: dict[str, Any]  # {"route", "url", "ready", "missing", "body"}
    bond: dict[str, Any] = field(default_factory=dict)


class FundService:
    def __init__(
        self,
        backend: FundStateBackend,
        *,
        clock: Callable[[], float] = time.time,
        tx_builder_url: str = DEFAULT_TX_BUILDER_URL,
    ) -> None:
        self.backend = backend
        self.clock = clock
        self.tx_builder_url = (tx_builder_url or DEFAULT_TX_BUILDER_URL).rstrip("/")

    # -- reads -------------------------------------------------------------------------------
    def load(self, fund: str) -> FundState:
        return self.backend.load(fund)

    def resolve_fund(self, fund: str | None = None, *, chat_id: str | None = None, default: str = "") -> str:
        """Pick the fund a command or tool means.

        Explicit name > the fund bound to this Telegram chat > the configured
        default > the only fund on this machine.
        """
        if fund and str(fund).strip():
            return validate_fund_name(str(fund))
        names: list[str] | None = None
        if chat_id:
            names = self.backend.list_funds()
            for name in names:
                try:
                    if self.backend.load(name).fund.telegram_chat_id == str(chat_id):
                        return name
                except FundStoreError:
                    continue
        if default:
            return validate_fund_name(default)
        names = names if names is not None else self.backend.list_funds()
        if len(names) == 1:
            return names[0]
        if not names:
            raise FundServiceError("no fund here yet: create one (host) or /join-fund <fund> (guest)")
        raise FundServiceError(f"which fund? pass one of: {', '.join(names)}")

    def check_draft(self, fund: str, agent: str, **kwargs: Any) -> rules.Verdict:
        """Read-only draft check (the pre_tool_call guard uses this)."""
        state = self.backend.load(fund)
        return rules.check_draft(state, agent, now=self.clock(), **kwargs)

    # -- host setup ------------------------------------------------------------------------
    def create_fund(self, fund: Fund, host_agent: Agent) -> FundState:
        now = self.clock()
        fund.name = validate_fund_name(fund.name)
        fund.created_at = fund.created_at or now
        host_agent.role = "host"
        host_agent.approved = True
        host_agent.registered = True  # the vault owner registers its own agent at creation
        host_agent.requested_at = host_agent.requested_at or now
        host_agent.approved_at = host_agent.approved_at or now
        host_agent.registered_at = host_agent.registered_at or now
        return self.backend.create(FundState(fund=fund, agents=[host_agent]))

    # -- members ----------------------------------------------------------------------------
    def request_membership(self, fund: str, telegram_id: str, *, address: str = "", username: str = "") -> Member:
        now = self.clock()

        def mutate(state: FundState) -> Member:
            member = state.member(str(telegram_id)) or (state.member(address) if address else None)
            if member is None:
                member = Member(telegram_id=str(telegram_id), joined_at=now)
                state.members.append(member)
            if address and not member.address:
                if not rules.is_address(address):
                    raise RuleViolation(rules.deny("missing_address", "that is not a 0x wallet address"))
                member.address = address
            if username:
                member.username = username
            return member

        return self.backend.update(fund, mutate)

    def approve_member(
        self,
        fund: str,
        actor: str,
        who: str,
        *,
        address: str = "",
        tx_hash: str = "",
        caller_telegram_id: str | None = None,
    ) -> tuple[Member, dict[str, Any]]:
        """Host approves a depositor. Returns the member and the approve-depositor body to sign."""
        now = self.clock()

        def mutate(state: FundState) -> tuple[Member, dict[str, Any]]:
            require(rules.check_caller_is_host(state, caller_telegram_id))
            member = state.member(who) or (state.member(address) if address else None)
            if member is None and rules.is_address(who):
                member = Member(telegram_id="", address=who, joined_at=now)
                state.members.append(member)
            elif member is None and address:
                member = Member(telegram_id=str(who), address=address, joined_at=now)
                state.members.append(member)
            if member is not None and address and not member.address:
                member.address = address
            require(rules.check_approve_member(state, actor, member))
            assert member is not None
            member.approved = True
            member.approved_at = now
            member.approve_tx = tx_hash or member.approve_tx
            member.member_no = state.next_member_no
            state.next_member_no += 1
            body = {"chainId": state.fund.chain_id, "vault": state.fund.vault, "depositor": member.address}
            return member, body

        return self.backend.update(fund, mutate)

    # -- agents --------------------------------------------------------------------------
    def request_agent(self, fund: str, agent: Agent) -> Agent:
        """`/join-fund`: a guest agent asks to co-manage. Host approval comes later."""
        now = self.clock()
        if not agent.identity_id or not rules.is_address(agent.address):
            raise RuleViolation(rules.deny("missing_identity", "joining needs an ERC-8004 identity id and a 0x wallet address"))

        def mutate(state: FundState) -> Agent:
            other = state.agent_by_address(agent.address)
            if other is not None and other.name.lower() != agent.name.lower() and not other.removed:
                raise RuleViolation(rules.deny("address_taken", f"wallet {agent.address} already belongs to {other.name}"))
            existing = state.agent(agent.name)
            if existing is not None and not existing.removed:
                if existing.registered:
                    raise RuleViolation(rules.deny("already_registered", f"{existing.name} is already a co-manager"))
                existing.identity_id = agent.identity_id
                existing.address = agent.address
                existing.owner_telegram_id = agent.owner_telegram_id or existing.owner_telegram_id
                return existing
            if existing is not None:
                state.agents.remove(existing)  # removed agents may ask again
            agent.role = "guest"
            agent.approved = agent.registered = agent.removed = False
            agent.requested_at = now
            agent.approved_at = agent.registered_at = agent.removed_at = None
            state.agents.append(agent)
            return agent

        return self.backend.update(fund, mutate)

    def approve_agent(
        self,
        fund: str,
        actor: str,
        name: str,
        *,
        tx_hash: str = "",
        caller_telegram_id: str | None = None,
    ) -> tuple[Agent, dict[str, Any]]:
        """Host approves a guest agent. Without `tx_hash` it returns the register-agent body
        to sign; call again with the tx hash once `registerAgent` lands to mark it registered."""
        now = self.clock()

        def mutate(state: FundState) -> tuple[Agent, dict[str, Any]]:
            require(rules.check_caller_is_host(state, caller_telegram_id))
            target = state.agent(name) or state.agent_by_address(name)
            require(rules.check_approve_agent(state, actor, target))
            assert target is not None
            if target.registered:
                raise RuleViolation(rules.deny("already_registered", f"{target.name} is already registered"))
            target.approved = True
            target.approved_at = target.approved_at or now
            if tx_hash:
                target.registered = True
                target.registered_at = now
                target.register_tx = tx_hash
            body = {
                "chainId": state.fund.chain_id,
                "vault": state.fund.vault,
                "agentAddress": target.address,
                "agentId": target.identity_id,
            }
            return target, body

        return self.backend.update(fund, mutate)

    def remove_agent(self, fund: str, actor: str, name: str, *, caller_telegram_id: str | None = None) -> Agent:
        """One action: mark removed, drop its claims, cancel its unproposed drafts.

        Proposals already on-chain keep running; the host vetoes them like anyone else.
        """
        now = self.clock()

        def mutate(state: FundState) -> Agent:
            require(rules.check_caller_is_host(state, caller_telegram_id))
            target = state.agent(name) or state.agent_by_address(name)
            require(rules.check_remove_agent(state, actor, target))
            assert target is not None
            target.removed = True
            target.removed_at = now
            key = target.name.lower()
            state.claims = [c for c in state.claims if c.agent.lower() != key]
            for p in state.proposals:
                if p.proposer_agent.lower() == key and p.status == "draft":
                    p.status = "cancelled"
                    p.updated_at = now
            return target

        return self.backend.update(fund, mutate)

    # -- coordination --------------------------------------------------------------------
    def claim_topic(self, fund: str, agent: str, topic: str) -> Claim:
        now = self.clock()

        def mutate(state: FundState) -> Claim:
            state.claims = rules.active_claims(state, now)
            require(rules.check_claim(state, agent, topic, now))
            mine = rules.claim_of(state, agent, now)
            if mine is not None:
                mine.expires_at = rules.claim_expires_at(now)  # same topic: refresh
                return mine
            claim = Claim(agent=state.agent(agent).name, topic=topic.strip(), expires_at=rules.claim_expires_at(now), created_at=now)  # type: ignore[union-attr]
            state.claims.append(claim)
            return claim

        return self.backend.update(fund, mutate)

    def release_claim(self, fund: str, agent: str, topic: str | None = None) -> int:
        now = self.clock()

        def mutate(state: FundState) -> int:
            key = agent.lower()
            want = rules.normalize_topic(topic) if topic else None
            keep, dropped = [], 0
            for c in rules.active_claims(state, now):
                if c.agent.lower() == key and (want is None or rules.normalize_topic(c.topic) == want):
                    dropped += 1
                else:
                    keep.append(c)
            state.claims = keep
            return dropped

        return self.backend.update(fund, mutate)

    def draft_proposal(
        self,
        fund: str,
        agent: str,
        *,
        topic: str,
        thesis: str,
        legs: list[Leg],
        max_capital: int,
        max_drawdown_bps: int,
        strategy_duration: int,
        bond_quote_wood: int | None = None,
        wood_balance: int | None = None,
        idea_by: str = "",
        strategy: str = "",
        metadata_uri: str = "",
    ) -> DraftResult:
        """Check every rule, record the draft, consume the claim, return the prepare payload."""
        now = self.clock()
        if not thesis.strip():
            raise RuleViolation(rules.deny("no_thesis", "a proposal needs a thesis"))

        def mutate(state: FundState) -> DraftResult:
            state.claims = rules.active_claims(state, now)
            require(rules.check_draft(
                state, agent, topic, max_capital=max_capital, max_drawdown_bps=max_drawdown_bps, legs=legs, now=now
            ))
            proposer = state.agent(agent)
            assert proposer is not None
            record = ProposalRecord(
                id=f"p-{state.next_proposal_seq}",
                proposer_agent=proposer.name,
                thesis=thesis.strip(),
                legs=legs,
                max_capital=str(max_capital),
                max_drawdown_bps=max_drawdown_bps,
                bond_wood=str(bond_quote_wood or 0),
                governor=state.fund.governor,
                topic=topic.strip(),
                strategy_duration=strategy_duration,
                idea_by=str(idea_by or ""),
                created_at=now,
                updated_at=now,
            )
            state.next_proposal_seq += 1
            state.proposals.append(record)
            state.claims = [c for c in state.claims if c.agent.lower() != proposer.name.lower()]
            bond = _bond_section(state, proposer, max_capital, bond_quote_wood, wood_balance)
            return DraftResult(record, self.propose_payload(state.fund, record, strategy=strategy, metadata_uri=metadata_uri), bond)

        return self.backend.update(fund, mutate)

    def propose_payload(self, fund: Fund, record: ProposalRecord, *, strategy: str = "", metadata_uri: str = "") -> dict[str, Any]:
        """The prepare/propose request this draft maps to, with what is still missing."""
        execute = [leg for leg in record.legs if leg.phase == "execute"]
        settle = [leg for leg in record.legs if leg.phase == "settle"]
        missing: list[str] = []
        if not fund.vault:
            missing.append("vault: the fund has no vault address yet")
        if not strategy:
            missing.append("strategy: a PortfolioStrategy clone address (deploy one through the factory)")
        if not metadata_uri:
            missing.append("metadataURI: pin the proposal metadata JSON and pass its ipfs:// URI")
        if record.strategy_duration <= 0:
            missing.append("strategyDuration: seconds the position stays open")
        for i, leg in enumerate(record.legs):
            if not leg.has_call:
                missing.append(f"leg {i} ({leg.summary!r}) has no calldata: build it with the CLI's --calldata-only")
        body: dict[str, Any] = {
            "chainId": fund.chain_id,
            "vault": fund.vault,
            "strategy": strategy,
            "metadataURI": metadata_uri,
            "strategyDuration": str(record.strategy_duration),
            "envelope": {"maxCapital": record.max_capital, "maxDrawdownBps": record.max_drawdown_bps},
            "executeCalls": [_call(leg) for leg in execute if leg.has_call],
            "executeCallCaps": [leg.cap for leg in execute if leg.has_call],
            "settlementCalls": [_call(leg) for leg in settle if leg.has_call],
            "settlementCallCaps": [leg.cap for leg in settle if leg.has_call],
        }
        if not missing:
            try:
                body = build_propose_body(
                    vault=fund.vault,
                    strategy=strategy,
                    metadata_uri=metadata_uri,
                    strategy_duration=record.strategy_duration,
                    max_capital=record.max_capital,
                    max_drawdown_bps=record.max_drawdown_bps,
                    execute_calls=body["executeCalls"],
                    execute_call_caps=body["executeCallCaps"],
                    settlement_calls=body["settlementCalls"],
                    settlement_call_caps=body["settlementCallCaps"],
                    chain_id=fund.chain_id,
                )
            except TxBuilderError as exc:
                missing.append(exc.message)
        return {
            "route": "propose",
            "url": f"{self.tx_builder_url}/prepare/propose",
            "ready": not missing,
            "missing": missing,
            "body": body,
        }

    def set_proposal_status(
        self,
        fund: str,
        actor: str,
        proposal_id: str,
        status: str,
        *,
        onchain_id: int | None = None,
        tx_hash: str = "",
        card_message_id: str = "",
    ) -> ProposalRecord:
        """Move a proposal along its lifecycle (draft -> proposed -> ... ). Proposer or host only."""
        now = self.clock()

        def mutate(state: FundState) -> ProposalRecord:
            record = state.proposal(proposal_id)
            if record is None:
                raise RuleViolation(rules.deny("unknown_proposal", f"no proposal {proposal_id}"))
            _require_proposer_or_host(state, actor, record)
            require(rules.check_transition(record.status, status))
            record.status = status
            record.updated_at = now
            if onchain_id is not None:
                record.onchain_id = int(onchain_id)
            if status == "proposed":
                record.propose_tx = tx_hash or record.propose_tx
                record.voting_ends_at = now + state.fund.voting_period
            if card_message_id:
                record.card_message_id = str(card_message_id)
            return record

        return self.backend.update(fund, mutate)

    def set_card_message(self, fund: str, proposal_id: str, card_message_id: str) -> ProposalRecord:
        """The Telegram lane records which message is the proposal's card."""
        def mutate(state: FundState) -> ProposalRecord:
            record = state.proposal(proposal_id)
            if record is None:
                raise RuleViolation(rules.deny("unknown_proposal", f"no proposal {proposal_id}"))
            record.card_message_id = str(card_message_id)
            return record

        return self.backend.update(fund, mutate)

    def record_vote(
        self,
        fund: str,
        proposal_id: str,
        *,
        voter: str,
        side: str,
        voter_kind: str = "member",
        note: str = "",
    ) -> ProposalRecord:
        """A member backs/objects (chat-side tally), or an agent reviews another agent's proposal.

        The binding vote is on-chain (only Against counts); this records the
        chat's signal so the card and the agents can see it.
        """
        now = self.clock()
        side = side.strip().lower()
        if side not in ("back", "object"):
            raise RuleViolation(rules.deny("bad_side", "side must be back or object"))

        def mutate(state: FundState) -> ProposalRecord:
            record = state.proposal(proposal_id)
            if voter_kind == "agent":
                require(rules.check_review(state, voter, record))
                assert record is not None
                reviewer = state.agent(voter)
                name = reviewer.name if reviewer else voter
                record.reviews = [r for r in record.reviews if r.agent.lower() != name.lower()]
                record.reviews.append(Review(agent=name, verdict=side, note=note, at=now))
            else:
                member = state.member(voter)
                require(rules.check_member_vote(state, member, record, now))
                assert record is not None and member is not None
                tid = member.telegram_id or member.address
                record.backers = [x for x in record.backers if x != tid]
                record.objectors = [x for x in record.objectors if x != tid]
                (record.backers if side == "back" else record.objectors).append(tid)
            record.updated_at = now
            return record

        return self.backend.update(fund, mutate)

    def record_settle(
        self, fund: str, actor: str, proposal_id: str, *, pnl: int, pnl_bps: int = 0, tx_hash: str = ""
    ) -> SettleReceipt:
        now = self.clock()

        def mutate(state: FundState) -> SettleReceipt:
            record = state.proposal(proposal_id)
            if record is None:
                raise RuleViolation(rules.deny("unknown_proposal", f"no proposal {proposal_id}"))
            _require_proposer_or_host(state, actor, record)
            require(rules.check_transition(record.status, "settled"))
            record.status = "settled"
            record.updated_at = now
            receipt = SettleReceipt(
                proposal_id=record.id,
                agent=record.proposer_agent,
                asset=state.fund.asset,
                pnl=str(int(pnl)),
                pnl_bps=int(pnl_bps),
                tx_hash=tx_hash,
                onchain_id=record.onchain_id,
                idea_by=record.idea_by,
                thesis=record.thesis,
                settled_at=now,
            )
            state.receipts.append(receipt)
            return receipt

        return self.backend.update(fund, mutate)


def _call(leg: Leg) -> dict[str, str]:
    return {"target": leg.target, "data": leg.data, "value": leg.value or "0"}


def _require_proposer_or_host(state: FundState, actor: str, record: ProposalRecord) -> None:
    if actor.lower() == record.proposer_agent.lower():
        require(rules.check_agent_can_act(state, actor))
        return
    require(rules.check_host_action(state, actor))


def _bond_section(
    state: FundState, agent: Agent, max_capital: int, quote: int | None, balance: int | None
) -> dict[str, Any]:
    policy = agent.bond_policy
    out: dict[str, Any] = {
        "bond_value_asset_units": str(rules.required_bond_value(max_capital, policy)),
        "asset": state.fund.asset,
        "rule": "bond = 1% of coverage; approve quote + 5%, never unlimited; re-quote on >5% drift, never widen",
    }
    if quote is None:
        out["missing"] = "bond_quote_wood: quote the WOOD bond for this coverage, then draft again"
        return out
    plan = rules.plan_bond(quote, balance if balance is not None else 0, policy)
    steps = []
    if balance is None:
        steps.append("read your WOOD balance; if it is below approve_wood, swap the gap first")
    elif plan.swap_first:
        steps.append(f"swap {policy.swap_gap_from} -> WOOD for {plan.gap} WOOD base units (first leg)")
    steps.append(
        f"approve exactly {plan.approve_amount} WOOD base units to the bond escrow of governor "
        f"{state.fund.governor or '<governor>'} (governor.bondEscrow())"
    )
    steps.append("propose (prepare/propose), then fund_set_proposal_status proposed with the on-chain id")
    out.update({
        "quote_wood": str(plan.quote),
        "approve_wood": str(plan.approve_amount),
        "wood_balance": None if balance is None else str(balance),
        "swap_gap_wood": str(plan.gap),
        "swap_first": plan.swap_first,
        "steps": steps,
    })
    return out


__all__ = ["DraftResult", "FundNotFound", "FundService", "FundServiceError", "RuleViolation"]
