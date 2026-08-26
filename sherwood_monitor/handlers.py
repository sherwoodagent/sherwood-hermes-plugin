"""Per-event-type handlers: decide how to inject + whether to post to XMTP."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .config import Config
from .event_buffer import EventBuffer
from .models import ChainEvent, SessionMessage

_log = logging.getLogger(__name__)

PostFn = Callable[[str, str], Awaitable[None]]  # (subdomain, markdown)

# Events we inject as context but don't auto-post
CHAIN_INJECT_ONLY = {
    "VoteCast",
    "AgentRegistered",
    "AgentRemoved",
    "DepositorApproved",
    "DepositorRemoved",
    "RedemptionsLockedEvent",
    "RedemptionsUnlockedEvent",
    "Deposited",
    "Withdrawn",
    # SandboxRun(uint256 indexed pid, address indexed sandbox, uint256 funding)
    # topic0 0x6fbfaa03c3db533cf914b5a5b997d8244b90daa0e259cf25627e7901d536dc16
    # (mechanical dry-run execution; context only, the payload-stored event is
    # the one worth surfacing to humans)
    "SandboxRun",
}

# Events that inject AND auto-post a summary to XMTP
CHAIN_INJECT_AND_POST = {
    "ProposalCreated",
    "ProposalExecuted",
    "ProposalSettled",
    "ProposalCancelled",
    # SandboxPayloadStored(uint256 indexed proposalId, uint256 funding, uint256 callCount, uint256 tokenCount)
    # topic0 0x393ef493444e6e45462f29fd987511c4cd17f31055d75871c2fe6cee68c82ffd
    "SandboxPayloadStored",
    # ReviewOpened(uint256 indexed proposalId, uint128 totalStakeAtOpen)
    # topic0 0x4d948f9d06779a41bd21bacd6cba0b797cf95b1f539bb54987679283d6792be5
    "ReviewOpened",
    # ReviewResolved(uint256 indexed proposalId, bool blocked, uint256 slashedAmount)
    # topic0 0xd889790c2115ffccca59bed8322903510682f6a6bdf7fe5752bfde7ada8b4f36
    "ReviewResolved",
    # GuardianSlashed(bytes32 indexed reviewKey, address indexed approver, uint256 ownSlash, uint256 delegatedSlash)
    # topic0 0x02f6fd955b5d82d2a69f041915e84a751e27676108460d0c3b711da57520db68
    "GuardianSlashed",
}


def _format_chain_injection(subdomain: str, ev: ChainEvent, priority: str = "normal") -> str:
    args_lines = "\n".join(f"  {k}: {v}" for k, v in ev.args.items())
    return (
        f'<sherwood-event fund="{subdomain}" source="chain" '
        f'type="{ev.type}" priority="{priority}" block="{ev.block}" tx="{ev.tx}">\n'
        f"<args>\n{args_lines}\n</args>\n"
        f"</sherwood-event>"
    )


def _format_proposal_created_summary(ev: ChainEvent) -> str:
    name = ev.args.get("metadataName", "(unnamed)")
    desc = ev.args.get("metadataDescription", "")
    proposer = ev.args.get("proposer", "?")
    fee_bps = ev.args.get("performanceFeeBps", "?")
    duration = ev.args.get("strategyDuration", "?")
    try:
        duration_days = f"{int(duration) // 86400}d" if duration != "?" else "?"
    except ValueError:
        duration_days = "?"
    return (
        f"**Proposal #{ev.args.get('proposalId', '?')} — {name}**\n"
        f"{desc}\n"
        f"Proposer: `{proposer}` | Fee: {fee_bps} bps | Duration: {duration_days}"
    )


def _format_proposal_executed_summary(ev: ChainEvent) -> str:
    capital = ev.args.get("capitalSnapshot", "?")
    try:
        capital_usd = f"${int(capital) / 1_000_000:,.2f}"
    except ValueError:
        capital_usd = capital
    return (
        f"**Proposal #{ev.args.get('proposalId', '?')} executed** — "
        f"capital deployed: {capital_usd} (USDC)"
    )


def _format_proposal_settled_summary(ev: ChainEvent) -> str:
    pnl_raw = ev.args.get("pnl", "0")
    try:
        pnl_usd = f"${int(pnl_raw) / 1_000_000:+,.2f}"
    except ValueError:
        pnl_usd = pnl_raw
    duration = ev.args.get("duration", "?")
    try:
        duration_days = f"{int(duration) // 86400}d"
    except ValueError:
        duration_days = "?"
    return (
        f"**Proposal #{ev.args.get('proposalId', '?')} settled** — "
        f"pnl: {pnl_usd}, duration: {duration_days}"
    )


def _format_proposal_cancelled_summary(ev: ChainEvent) -> str:
    return (
        f"**Proposal #{ev.args.get('proposalId', '?')} cancelled** "
        f"by `{ev.args.get('cancelledBy', '?')}`"
    )


def _format_sandbox_payload_stored_summary(ev: ChainEvent) -> str:
    funding = ev.args.get("funding", "?")
    try:
        funding_usd = f"${int(funding) / 1_000_000:,.2f}"
    except ValueError:
        funding_usd = funding
    return (
        f"**Proposal #{ev.args.get('proposalId', '?')} — sandbox payload stored** — "
        f"funding: {funding_usd} (USDC), calls: {ev.args.get('callCount', '?')}, "
        f"tokens: {ev.args.get('tokenCount', '?')}. Guardians: payload is ready for review."
    )


def _fmt_wood(raw: str) -> str:
    try:
        return f"{int(raw) / 1e18:,.2f} WOOD"
    except ValueError:
        return raw


def _format_review_opened_summary(ev: ChainEvent) -> str:
    return (
        f"**Guardian review opened — proposal #{ev.args.get('proposalId', '?')}** — "
        f"total stake at open: {_fmt_wood(ev.args.get('totalStakeAtOpen', '?'))}"
    )


def _format_review_resolved_summary(ev: ChainEvent) -> str:
    blocked = str(ev.args.get("blocked", "?")).lower() in {"true", "1"}
    verdict = "BLOCKED" if blocked else "cleared"
    return (
        f"**Guardian review resolved — proposal #{ev.args.get('proposalId', '?')}** — "
        f"verdict: {verdict}, slashed: {_fmt_wood(ev.args.get('slashedAmount', '?'))}"
    )


def _format_guardian_slashed_summary(ev: ChainEvent) -> str:
    return (
        f"**Guardian slashed** — approver `{ev.args.get('approver', '?')}` "
        f"lost {_fmt_wood(ev.args.get('ownSlash', '?'))} "
        f"(review `{ev.args.get('reviewKey', '?')}`)"
    )


_CHAIN_SUMMARY_FORMATTERS: dict[str, Callable[[ChainEvent], str]] = {
    "ProposalCreated": _format_proposal_created_summary,
    "ProposalExecuted": _format_proposal_executed_summary,
    "ProposalSettled": _format_proposal_settled_summary,
    "ProposalCancelled": _format_proposal_cancelled_summary,
    "SandboxPayloadStored": _format_sandbox_payload_stored_summary,
    "ReviewOpened": _format_review_opened_summary,
    "ReviewResolved": _format_review_resolved_summary,
    "GuardianSlashed": _format_guardian_slashed_summary,
}


async def handle_chain_event(
    subdomain: str,
    ev: ChainEvent,
    buffer: EventBuffer,
    cfg: Config,
    post_fn: PostFn,
) -> None:
    """Route a single on-chain event."""
    if ev.type not in CHAIN_INJECT_ONLY and ev.type not in CHAIN_INJECT_AND_POST:
        _log.warning("unhandled chain event type: %s", ev.type)
        return

    buffer.push(_format_chain_injection(subdomain, ev))

    if ev.type in CHAIN_INJECT_AND_POST and cfg.xmtp_summaries:
        formatter = _CHAIN_SUMMARY_FORMATTERS.get(ev.type)
        if formatter is not None:
            summary = formatter(ev)
            await post_fn(subdomain, summary)


# XMTP message types that always get injected with specific priority
_XMTP_PRIORITY: dict[str, str] = {
    "RISK_ALERT": "high",
    "APPROVAL_REQUEST": "human-escalate",
    "STRATEGY_PROPOSAL": "normal",
    "TRADE_SIGNAL": "normal",
    "POSITION_UPDATE": "low",
    "LP_REPORT": "low",
    "TRADE_EXECUTED": "low",
    "MEMBER_JOIN": "low",
    "RAGEQUIT_NOTICE": "normal",
    "AGENT_REGISTERED": "low",
    "X402_RESEARCH": "normal",
}

# Types never injected regardless of config
_XMTP_NEVER_INJECT = {"REACTION"}


def _format_xmtp_injection(subdomain: str, msg: SessionMessage, priority: str) -> str:
    return (
        f'<sherwood-event fund="{subdomain}" source="xmtp" '
        f'type="{msg.type}" priority="{priority}" from="{msg.from_}" '
        f'sentAt="{msg.sent_at}">\n'
        f"{msg.text}\n"
        f"</sherwood-event>"
    )


async def handle_xmtp_message(
    subdomain: str,
    msg: SessionMessage,
    buffer: EventBuffer,
    cfg: Config,
    post_fn: PostFn,
) -> None:
    """Route a single XMTP message."""
    if msg.type in _XMTP_NEVER_INJECT:
        return

    # Plain MESSAGE: respect inject_mentions_only
    if msg.type == "MESSAGE":
        if cfg.inject_mentions_only and "@" not in msg.text:
            return
        buffer.push(_format_xmtp_injection(subdomain, msg, "normal"))
        return

    priority = _XMTP_PRIORITY.get(msg.type)
    if priority is None:
        _log.info("unhandled xmtp message type: %s", msg.type)
        return

    buffer.push(_format_xmtp_injection(subdomain, msg, priority))
