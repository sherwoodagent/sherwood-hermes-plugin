"""pre_tool_call guard for fund drafts.

Runs before `fund_draft_proposal` (the only fund tool that maps to calldata)
and blocks the call when the draft would break a coordination rule or exceed
the fund's max capital / drawdown. Unlike the CLI risk check, this fails
CLOSED: fund state is local and authoritative, so "cannot check" means the
draft is not safe to build.
"""
from __future__ import annotations

from typing import Any, Callable

from . import rules
from .runtime import FundRuntime
from .tools import GUARD_ERRORS, parse_draft_args

GUARDED_TOOLS = frozenset({"fund_draft_proposal"})

FundGuard = Callable[[str, dict[str, Any]], "str | None"]


def make_fund_guard(rt: FundRuntime) -> FundGuard:
    """Return `guard(tool_name, args) -> block reason | None`. Synchronous and fast."""

    def guard(tool_name: str, args: dict[str, Any]) -> str | None:
        if tool_name not in GUARDED_TOOLS:
            return None
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
        except GUARD_ERRORS as exc:
            return f"fund draft blocked: cannot verify it against fund state ({exc})"
        if verdict.ok:
            return None
        return f"fund draft blocked ({verdict.code}): {verdict.reason}"

    return guard
