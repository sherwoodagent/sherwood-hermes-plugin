"""Per-event-type handlers: decide what to inject into the agent's next turn."""
from __future__ import annotations

import logging

from .config import Config
from .event_buffer import EventBuffer
from .models import ChainEvent, SessionMessage

_log = logging.getLogger(__name__)

# On-chain events we inject as context; any other type is logged and dropped.
CHAIN_INJECT = {
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
    # (mechanical dry-run execution; context only)
    # NAME COLLISION: ICallSandbox declares a DIFFERENT SandboxRun with shape
    # (address,uint256,uint256), topic0 0x2638b671... — routing here is by
    # name only, so "SandboxRun" means exclusively the ISyndicateVault event
    # above. The producer (`sherwood session check`, cli/src/lib/events.ts)
    # emits only the vault-side event under this type string; if the
    # ICallSandbox event is ever wired, it must arrive under a distinct type
    # (e.g. "CallSandboxRun") and get its own entry here.
    "SandboxRun",
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


async def handle_chain_event(
    subdomain: str,
    ev: ChainEvent,
    buffer: EventBuffer,
    cfg: Config,
) -> None:
    """Route a single on-chain event."""
    if ev.type not in CHAIN_INJECT:
        _log.warning("unhandled chain event type: %s", ev.type)
        return

    buffer.push(_format_chain_injection(subdomain, ev))


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
