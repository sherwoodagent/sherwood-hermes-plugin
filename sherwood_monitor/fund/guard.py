"""pre_tool_call guard for fund drafts.

Runs before `fund_draft_proposal` (the only fund tool that maps to calldata)
and blocks the call when the draft would break a coordination rule or exceed
the fund's max capital / drawdown. Unlike the CLI risk check, this fails
CLOSED: fund state is local and authoritative, so "cannot check" means the
draft is not safe to build.
"""
from __future__ import annotations

from typing import Any, Callable

from pathlib import Path

from ..session import current_caller
from . import authz, rules
from .rules import RuleViolation
from .runtime import FundRuntime
from .store import FundNotFound, default_state_dir, scrub_paths
from .tools import GUARD_ERRORS, parse_draft_args

GUARDED_TOOLS = frozenset({"fund_draft_proposal"}) | authz.HOST_TOOLS | {"fund_record_vote"}

# Best effort, chat turns only: a terminal / file / code tool whose arguments name the operator's
# rebind command, the fund state files or a config.yaml is refused. A determined model can obfuscate
# a command, so the real boundary is the deployment: a fund group's toolset must not include
# terminal, file or code tools (docs/fund-host-join.md, "Deployment boundary").
REBIND_MARKERS = (
    "fund-bind", "fund_bind", "config.yaml", "sherwood-monitor/funds", "sherwood-monitor\\funds",
    "plugins/sherwood-monitor",
)
REBIND_BLOCK = (
    "fund action blocked: commands and files that change fund bindings, fund state or plugin config "
    "are never run from chat. Ask the operator."
)


def _strings(obj: Any, depth: int = 0):
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v, depth + 1)


def rebind_markers(rt: FundRuntime) -> tuple[str, ...]:
    """Static markers plus every spelling of the directory this process keeps fund state in."""
    dirs = {rt.settings.fund_state_dir or str(default_state_dir())}
    root = getattr(rt.service.backend, "_root", None) or getattr(rt.service.backend, "root", None)
    if root:
        dirs.add(str(root))
    extra: list[str] = []
    for d in dirs:
        for p in (d, str(Path(d).expanduser()), str(Path(d).expanduser().resolve())):
            if p and p.rstrip("/"):
                extra.append(p.rstrip("/").lower())
    return tuple(m.lower() for m in REBIND_MARKERS) + tuple(dict.fromkeys(extra))

FundGuard = Callable[[str, dict[str, Any]], "str | dict | None"]


def make_fund_guard(rt: FundRuntime) -> FundGuard:
    """Return `guard(tool_name, args) -> block reason | approval directive | None`. Synchronous and fast.

    - `fund_draft_proposal`: blocked when it would break a rule (fails closed).
    - Host-authority tools (and recording a member vote for someone else): blocked
      when the turn's sender is not the host; otherwise Hermes' approval gate
      (`{"action": "approve"}`) asks the human before the tool runs.
    """

    markers = rebind_markers(rt)

    def rebind_block(tool_name: str, args: dict[str, Any]) -> str | None:
        if tool_name.startswith(("fund_", "sherwood_monitor_")):
            return None
        if not current_caller().messaging:
            return None  # the operator's own CLI/TUI turn
        for text in _strings(args or {}):
            low = text.lower()
            if any(m in low for m in markers):
                return REBIND_BLOCK
        return None

    def host_directive(tool_name: str, args: dict[str, Any]) -> str | dict | None:
        try:
            fund = rt.fund_for((args or {}).get("fund"))
        except GUARD_ERRORS:
            fund = ""
        try:
            state = rt.service.load(fund) if fund else None
        except FundNotFound:
            state = None
        except GUARD_ERRORS as exc:
            return f"fund action blocked: cannot read fund state ({scrub_paths(exc)})"
        caller = current_caller()
        if tool_name == "fund_record_vote":
            kind = str((args or {}).get("voter_kind") or ("member" if (args or {}).get("voter") else "agent"))
            if kind != "member":
                return None  # an agent's own review of another agent's proposal: chat signal only
            voter = str((args or {}).get("voter") or "")
            if caller.telegram_user and caller.user_id == voter:
                return None  # a member recording their own back/object
            verdict = authz.check_member_vote_caller(state, caller, voter, rt.settings.owner_telegram_id)
        else:
            verdict = authz.check_caller(state, caller, rt.settings.owner_telegram_id)
        if not verdict.ok:
            return f"fund action blocked ({verdict.code}): {verdict.reason}"
        return authz.approve_directive(tool_name, fund, args or {})

    def guard(tool_name: str, args: dict[str, Any]) -> str | dict | None:
        blocked = rebind_block(tool_name, args)
        if blocked:
            return blocked
        if tool_name not in GUARDED_TOOLS:
            return None
        if tool_name != "fund_draft_proposal":
            return host_directive(tool_name, args)
        try:
            fund = rt.fund_for((args or {}).get("fund"))
            state = rt.service.load(fund)
            d = parse_draft_args(args or {}, state)
            verdict = rules.check_draft(
                state,
                rt.agent_name(),
                d.topic,
                max_capital=d.max_capital,
                max_drawdown_bps=d.max_drawdown_bps,
                legs=d.legs,
                now=rt.service.clock(),
            )
        except RuleViolation as exc:
            return f"fund draft blocked ({exc.code}): {exc.reason}"
        except GUARD_ERRORS as exc:
            return f"fund draft blocked: cannot verify it against fund state ({exc})"
        if verdict.ok:
            return None
        return f"fund draft blocked ({verdict.code}): {verdict.reason}"

    return guard
