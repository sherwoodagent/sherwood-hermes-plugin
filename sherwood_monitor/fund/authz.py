"""Host authority for the LLM tools.

Group members talk to the agent, so anything they type can end up steering
it. Tools that act with the host's authority therefore check WHO started the
turn (``sherwood_monitor.session``):

- a Telegram turn: the sender must be the fund's host (``host_telegram_id``,
  or ``owner_telegram_id`` before the fund exists). An unset id means nobody;
- another chat platform: refused (the host's identity is a Telegram id);
- an operator surface (CLI, TUI, cron, no session): allowed, and the
  ``pre_tool_call`` guard asks the human through Hermes' approval gate
  (``{"action": "approve"}``) before the tool runs.

The guard applies the same check, so a refused call never reaches the
approval prompt.
"""
from __future__ import annotations

from typing import Any

from ..session import Caller
from .models import FundState
from .rules import ALLOW, Verdict, deny

# Tools that act with the host's authority (state the host owns, or money-adjacent bookkeeping).
HOST_TOOLS = frozenset({
    "fund_setup",
    "fund_approve_member",
    "fund_approve_agent",
    "fund_remove_agent",
    "fund_record_settle",
    "fund_set_proposal_status",
})

HOST_UNSET = (
    "the fund has no host Telegram id, so nobody may do this from chat. The operator sets it: "
    "`hermes sherwood fund-bind <fund> --host-telegram-id <id>` (or fund.owner_telegram_id in "
    "~/.hermes/plugins/sherwood-monitor/config.yaml before the fund exists)"
)


def expected_host(state: FundState | None, owner_telegram_id: str = "") -> str:
    if state is not None and state.fund.host_telegram_id:
        return str(state.fund.host_telegram_id)
    return str(owner_telegram_id or "")


def check_caller(state: FundState | None, caller: Caller, owner_telegram_id: str = "") -> Verdict:
    """May the person who started this turn use the host's authority?"""
    if not caller.messaging:
        return ALLOW  # operator surface: the approval gate asks the human
    if caller.platform != "telegram":
        return deny("not_host", f"host actions are not taken from {caller.platform}; use the host's Telegram or the CLI")
    host = expected_host(state, owner_telegram_id)
    if not host:
        return deny("host_unset", HOST_UNSET)
    if caller.user_id != host:
        return deny("not_host", "only the fund host can do that")
    return ALLOW


def check_member_vote_caller(state: FundState | None, caller: Caller, voter: str, owner_telegram_id: str = "") -> Verdict:
    """A member records their own back/object; anyone else's needs the host."""
    if caller.messaging and caller.platform == "telegram" and caller.user_id and caller.user_id == str(voter):
        return ALLOW
    return check_caller(state, caller, owner_telegram_id)


def approve_directive(tool_name: str, fund: str, args: dict[str, Any]) -> dict[str, Any]:
    """Hermes' human-approval shape for a pre_tool_call hook (v0.22.x, fail-closed)."""
    keys = ("member", "agent", "proposal_id", "status", "pnl", "host_telegram_id", "telegram_chat_id", "vault", "voter", "side")
    detail = ", ".join(f"{k}={args[k]}" for k in keys if args.get(k) not in (None, ""))
    return {
        "action": "approve",
        "message": f"Sherwood fund {fund or '?'}: {tool_name}" + (f" ({detail})" if detail else "") + " acts with the host's authority.",
        "rule_key": f"sherwood-fund:{tool_name}",
    }
