"""Plain-text renderings of fund state: the status card, agent list, receipts.

The slash commands return these strings. `FundCard.buttons` follow the
Telegram button contract (docs/telegram.md): a Telegram button cannot run a
slash command, so a button is one of

- `url`: a public link, e.g. the spectator page `${APP_BASE_URL}/tg/fund/<vault>`;
- `link`: a PERSONAL signed `/tg/<link>?fund=<vault>&tg=..&sig=..` link
  (deposit, withdraw, join). The group card carries a `shfund:<link>:<vault>`
  callback and the bot hands the tapping user their own URL button;
- `action`: a host-only callback (`shfund:<action>:<arg>`).

Money only moves in the browser, signed by the member's or host's wallet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..telegram.links import fund_page_url
from . import rules
from .models import BOND_CHALLENGE_WINDOW_DAYS, WOOD_DECIMALS, FundState, ProposalRecord, SettleReceipt
from .units import format_units

PERSONAL_LINKS = ("deposit", "withdraw", "join")
STATUS_LABELS = {"review": "guardian review", "approved": "guardian approved"}


@dataclass(frozen=True)
class CardButton:
    label: str
    url: str = ""  # public URL button
    link: str = ""  # personal signed /tg link kind: deposit | withdraw | join
    action: str = ""  # host-only callback action
    arg: str = ""  # callback argument (the vault for links)

    @property
    def callback_data(self) -> str:
        kind = self.link or self.action
        return f"shfund:{kind}:{self.arg}" if kind else ""


@dataclass(frozen=True)
class FundCard:
    text: str
    buttons: list[CardButton] = field(default_factory=list)

    def as_text(self) -> str:
        if not self.buttons:
            return self.text
        parts = []
        for b in self.buttons:
            if b.url:
                parts.append(f"[{b.label}] {b.url}")
            elif b.link:
                parts.append(f"[{b.label}] personal link: tap it on the fund card in the group")
        return self.text + ("\n\nButtons: " + " · ".join(parts) if parts else "")


def hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts))


def bond_note(status: str) -> str:
    """Where the proposer's WOOD bond is after the proposal ends (fork run, finding 8)."""
    if status == "vetoed":
        return "Bond: returned to the agent at once (reclaim-bond right after the veto)."
    if status == "settled":
        return (
            f"Bond: locked in escrow for the {BOND_CHALLENGE_WINDOW_DAYS}-day challenge window "
            f"(reclaimable after executedAt + duration + {BOND_CHALLENGE_WINDOW_DAYS} days)."
        )
    return ""


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
    if p.status == "review" and p.review_ends_at:
        when = f", {_minutes(p.review_ends_at - now)} left" if p.review_ends_at > now else ", review ended"
        if p.guardian_coverage:
            when += f", coverage {p.guardian_coverage}"
    tally = f" · {len(p.backers)} back / {len(p.objectors)} object" if (p.backers or p.objectors) else ""
    against = f" · Against {p.against_bps / 100:g}%" if p.against_bps else ""
    reviews = f" · {len(p.reviews)} agent review(s)" if p.reviews else ""
    status = STATUS_LABELS.get(p.status, p.status)
    return f"{tag} {p.topic or p.thesis[:40]} by {p.proposer_agent} · {status}{when} · max {amount(state, p.max_capital)}{against}{tally}{reviews}"


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
    if f.cooldown_until and f.cooldown_until > now:
        lines.append(f"Next proposal possible at {hhmm(f.cooldown_until)} UTC (governor cooldown after the last veto/settle)")
    if f.agent_fee_bps is not None:
        lines.append(f"Agent fee {f.agent_fee_bps / 100:g}% (max {rules.MAX_AGENT_FEE_BPS / 100:g}% so guardians can approve)")
    if not rules.check_proposable_asset(state).ok:
        lines.append(f"{f.asset} fund: deposits work, proposals cannot (PortfolioStrategy needs USDG).")
    if state.receipts:
        lines.append("Last settle: " + receipt_line(state, state.receipts[-1]))
    lines.append(f"Bring your Hermes: its owner runs /join-fund {f.name}; the host approves it here.")
    buttons: list[CardButton] = []
    if rules.is_address(f.vault):
        vault = f.vault.lower()
        buttons.append(CardButton("📊 View", url=fund_page_url(vault, app_base_url or None)))
        buttons.append(CardButton("💰 Deposit", link="deposit", arg=vault))
        buttons.append(CardButton("↩️ Withdraw", link="withdraw", arg=vault))
        buttons.append(CardButton("🎟 Join", link="join", arg=vault))
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
    lines.append(bond_note("settled"))
    if state.fund.cooldown_until:
        lines.append(f"Next proposal possible at {hhmm(state.fund.cooldown_until)} UTC (governor cooldown).")
    return "\n".join(lines)


def wood(units: int | str) -> str:
    return f"{format_units(units, WOOD_DECIMALS, max_places=4)} WOOD"
