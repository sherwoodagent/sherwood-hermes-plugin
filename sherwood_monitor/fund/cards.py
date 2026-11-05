"""Plain-text renderings of fund state: the status card, agent list, receipts.

The slash commands return these strings. The Telegram lane renders the same
`FundCard.buttons` as an inline keyboard; a button is either a URL into the
Sherwood app or a slash command to run.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import rules
from .models import WOOD_DECIMALS, FundState, ProposalRecord, SettleReceipt
from .units import format_units


@dataclass(frozen=True)
class CardButton:
    label: str
    url: str = ""
    command: str = ""


@dataclass(frozen=True)
class FundCard:
    text: str
    buttons: list[CardButton] = field(default_factory=list)

    def as_text(self) -> str:
        if not self.buttons:
            return self.text
        parts = [f"[{b.label}] {b.url or b.command}" for b in self.buttons]
        return self.text + "\n\nButtons: " + " · ".join(parts)


def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "—")


def _minutes(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m}m" if m < 120 else f"{m // 60}h"


def amount(state: FundState, units: int | str) -> str:
    return f"{format_units(units, state.fund.asset_decimals, max_places=2)} {state.fund.asset}"


def proposal_line(state: FundState, p: ProposalRecord, now: float) -> str:
    tag = f"#{p.onchain_id}" if p.onchain_id is not None else p.id
    when = ""
    if p.status in ("proposed", "voting") and p.voting_ends_at:
        when = f", {_minutes(p.voting_ends_at - now)} left" if p.voting_ends_at > now else ", voting ended"
    tally = f" · {len(p.backers)} back / {len(p.objectors)} object" if (p.backers or p.objectors) else ""
    reviews = f" · {len(p.reviews)} agent review(s)" if p.reviews else ""
    return f"{tag} {p.topic or p.thesis[:40]} by {p.proposer_agent} · {p.status}{when} · max {amount(state, p.max_capital)}{tally}{reviews}"


def render_fund_card(state: FundState, *, now: float, app_base_url: str = "") -> FundCard:
    f = state.fund
    approved = sum(1 for m in state.members if m.approved)
    waiting = sum(1 for m in state.members if not m.approved)
    agents = [a for a in state.agents if not a.removed]
    lines = [
        f"{f.name} · {f.asset} on Robinhood Chain ({f.chain_id})",
        f"Vault {short_addr(f.vault)} · voting {_minutes(f.voting_period)} · veto at {f.veto_threshold_bps / 100:g}% Against",
        f"Members: {approved} approved" + (f", {waiting} waiting for the host" if waiting else ""),
        "Agents: " + (", ".join(_agent_tag(a) for a in agents) if agents else "none"),
    ]
    open_ = state.open_proposals()
    lines.append(f"Open proposals ({len(open_)}/{rules.MAX_OPEN_PER_FUND}):" if open_ else "No open proposals.")
    lines += [f"  {proposal_line(state, p, now)}" for p in open_]
    for c in rules.active_claims(state, now):
        lines.append(f"Drafting: {c.agent} on {c.topic!r} ({_minutes(c.expires_at - now)} left on the claim)")
    if state.receipts:
        lines.append("Last settle: " + receipt_line(state, state.receipts[-1]))
    base = (app_base_url or "").rstrip("/")
    buttons: list[CardButton] = []
    if base:
        buttons.append(CardButton("Deposit", url=f"{base}/fund/{f.name}"))
        buttons.append(CardButton("Proposals", url=f"{base}/fund/{f.name}/proposals"))
    buttons.append(CardButton("Agents", command=f"/fund-agents {f.name}"))
    buttons.append(CardButton("Bring your Hermes", command=f"/join-fund {f.name}"))
    return FundCard("\n".join(lines), buttons)


def _agent_tag(a) -> str:  # type: ignore[no-untyped-def]
    if a.role == "host":
        return f"{a.name} (host)"
    if a.registered:
        return f"{a.name} (co-manager)"
    if a.approved:
        return f"{a.name} (approved, waiting for registerAgent)"
    return f"{a.name} (asked to join)"


def render_agents(state: FundState, *, now: float) -> str:
    if not state.agents:
        return f"{state.fund.name} has no agents yet."
    lines = [f"Agents in {state.fund.name}:"]
    for a in state.agents:
        bits = [a.role, f"identity #{a.identity_id}" if a.identity_id else "no identity", short_addr(a.address)]
        if a.removed:
            bits.append("removed")
        elif a.registered:
            bits.append("registered")
        elif a.approved:
            bits.append("approved, registerAgent pending")
        else:
            bits.append(f"waiting: host runs /fund-approve {a.name}")
        open_ = rules.open_proposals_of(state, a.name)
        if open_:
            bits.append("open: " + ", ".join(p.id for p in open_))
        claim = rules.claim_of(state, a.name, now)
        if claim:
            bits.append(f"drafting {claim.topic!r}")
        lines.append(f"- {a.name}: " + " · ".join(bits))
    return "\n".join(lines)


def receipt_line(state: FundState, r: SettleReceipt) -> str:
    sign = "+" if int(r.pnl) >= 0 else "-"
    pnl = amount(state, abs(int(r.pnl)))
    tag = f"#{r.onchain_id}" if r.onchain_id is not None else r.proposal_id
    return f"{tag} {sign}{pnl} ({r.pnl_bps:+d} bps) by {r.agent}"


def render_receipt(state: FundState, r: SettleReceipt) -> str:
    """The settle message the agent posts in the group."""
    lines = [f"Settled {receipt_line(state, r)}"]
    if r.thesis:
        lines.append(f"Thesis: {r.thesis}")
    if r.idea_by:
        lines.append(f"Idea by member {r.idea_by}")
    if r.tx_hash:
        lines.append(f"Tx: {r.tx_hash}")
    return "\n".join(lines)


def wood(units: int | str) -> str:
    return f"{format_units(units, WOOD_DECIMALS, max_places=4)} WOOD"
