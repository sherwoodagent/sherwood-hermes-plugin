"""pre_tool_call guard for fund drafts and for terminal proposes on a fund vault.

Runs before `fund_draft_proposal` (the only fund tool that maps to calldata)
and blocks the call when the draft would break a coordination rule or exceed
the fund's max capital / drawdown. A terminal `sherwood strategy propose` /
`sherwood proposal create` whose vault belongs to a fund on this host is held
to the same ceilings, read from the local fund record (no chain read); one that
passes is answered with `FUND_RULES_APPLIED`, so the general advisory sizing
rule in hooks.py does not run on top. Unlike the CLI risk check, this fails
CLOSED: fund state is local and authoritative, so "cannot check" means the
draft or propose is not safe to run.
"""
from __future__ import annotations

from typing import Any, Callable

from pathlib import Path

from ..propose_cmd import (
    CLI_DEFAULT_DRAWDOWN_BPS,
    CLI_PACKAGE,
    FUND_RULES_APPLIED,
    TERMINAL_TOOLS,
    ProposeInvocation,
    has_unquoted_hash,
    is_address,
    looks_like_propose,
    mentions_propose,
    parse_propose_invocations,
)
from ..session import current_caller
from . import authz, rules
from .generic_call import require_agent_signer
from .models import FundState
from .rules import RuleViolation
from .runtime import FundRuntime
from .store import FundNotFound, default_state_dir, scrub_paths
from .tools import GUARD_ERRORS, parse_draft_args
from .units import UnitsError, format_units, to_base_units

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

FundGuard = Callable[[str, dict[str, Any]], Any]  # block reason | directive | FUND_RULES_APPLIED | None


def _fund_states(rt: FundRuntime) -> list[FundState]:
    """Every fund on this host that has a vault. Raises when fund state cannot be read."""
    out = []
    for name in rt.service.backend.list_funds():
        st = rt.service.load(name)
        if st.fund.vault:
            out.append(st)
    return out


def _amount_text(units: int, st: FundState) -> str:
    return f"{format_units(units, st.fund.asset_decimals)} {st.fund.asset}"


# The flag whose value the CLI 0.103.0 deploys as the proposal's capital (its envelope maxCapital),
# per template (dist: buildInitDataForTemplate / resolveChainInputsForTemplate). A template not in
# this table is refused on a fund vault: the guard cannot tell what it would deploy.
TEMPLATE_CAPITAL_FLAGS: dict[str, tuple[str, ...]] = {
    "portfolio": ("--amount",),
    "generic-call": ("--amount",),  # the pull amount
    "morpho-supply": ("--amount",),
    "concentrated-liquidity": ("--collateral-amount",),  # --amount is ignored; the borrow rides on top
    "lighter-perp": ("--deposit", "--amount"),  # --amount is the alias
    "launchpad": ("--asset-in",),
}
PROPOSAL_CREATE_CAPITAL_FLAGS: tuple[str, ...] = ("--max-capital",)
# Templates that derive the drawdown themselves when the flag is omitted (launchpad: from --asset-in).
DERIVED_DRAWDOWN_TEMPLATES = frozenset({"launchpad"})


def check_propose_ceilings(inv: ProposeInvocation, st: FundState) -> str | None:
    """A propose for fund `st`'s vault against the fund's own ceilings: a block reason, or None.

    The capital is the flag the CLI deploys for that template (TEMPLATE_CAPITAL_FLAGS;
    `--max-capital` on `proposal create`), in human asset units; every other capital-like
    flag on the command counts too, and the largest value is checked. An unmapped template,
    a missing mapped flag or an unreadable amount is refused. `--max-drawdown-bps` omitted
    means the CLI default (2000 bps), checked like a typed one; if it is given more than
    once, the largest counts.
    """
    f = st.fund
    name = f.name
    if inv.kind == "proposal create":
        required = PROPOSAL_CREATE_CAPITAL_FLAGS
    else:
        mapped = TEMPLATE_CAPITAL_FLAGS.get(inv.template)
        if mapped is None:
            return (
                f"fund propose blocked (unmapped_template): the guard does not know which flag carries the "
                f"capital of template {inv.template or '(none)'!r}, so it cannot hold it to {name}'s ceilings. "
                f"Fund vaults take {', '.join(sorted(TEMPLATE_CAPITAL_FLAGS))} from the terminal"
            )
        required = mapped
    if not any(flag in required for flag, _ in inv.capital):
        return (
            f"fund propose blocked (no_amount): a {inv.template or inv.kind} propose for {name}'s vault must "
            f"name its capital with {' or '.join(required)}, so the fund ceiling can be checked"
        )
    amounts: list[int] = []
    for flag, raw in inv.capital:
        try:
            amounts.append(to_base_units(raw, f.asset_decimals))
        except (UnitsError, ValueError):
            return f"fund propose blocked (bad_amount): cannot read {flag} {raw!r} as an amount of {f.asset} for {name}"
    capital = max(amounts)
    if f.max_capital is not None and capital > int(f.max_capital):
        return (
            f"fund propose blocked (exceeds_max_capital): {_amount_text(capital, st)} is above the fund "
            f"ceiling {_amount_text(int(f.max_capital), st)} for {name}"
        )
    if not inv.drawdowns:
        if inv.template in DERIVED_DRAWDOWN_TEMPLATES:
            return (
                f"fund propose blocked (no_drawdown): {inv.template} derives its drawdown when "
                f"--max-drawdown-bps is omitted; pass it so {name}'s ceiling can be checked"
            )
        drawdown, typed = CLI_DEFAULT_DRAWDOWN_BPS, False
    else:
        values: list[int] = []
        for raw in inv.drawdowns:
            try:
                values.append(int(raw))
            except ValueError:
                return f"fund propose blocked (bad_drawdown): cannot read --max-drawdown-bps {raw!r} as bps for {name}"
        drawdown, typed = max(values), True
    if f.max_drawdown_bps is not None and drawdown > f.max_drawdown_bps:
        what = f"max drawdown {drawdown} bps" if typed else (
            f"no --max-drawdown-bps: the CLI default {drawdown} bps"
        )
        return (
            f"fund propose blocked (exceeds_max_drawdown): {what} is above the fund ceiling "
            f"{f.max_drawdown_bps} bps for {name}"
        )
    return None


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

    def terminal_propose(args: dict[str, Any]) -> Any:
        """A terminal propose on a fund vault: the fund's ceilings, or None (not a fund propose).

        This checks the agent's own command text; it does not run the shell. On a host with a
        vaulted fund it therefore refuses what it cannot read: a command that cannot be split,
        an unquoted `#`, a `--vault` that is not a literal address, no `--vault` (CLI 0.103.0
        requires it), or a fund vault address in a propose-looking command that no readable
        `sherwood ... propose` names as its --vault (`bash -c`, `eval`, `$(...)`, a variable).
        A propose built entirely at run time, with no literal fund address, is not seen.
        """
        command = str((args or {}).get("command") or "")
        if not (looks_like_propose(command) or mentions_propose(command) or CLI_PACKAGE in command):
            return None  # no propose spelled anywhere in it (a parsed propose always is)
        try:
            states = _fund_states(rt)
        except GUARD_ERRORS as exc:
            return f"fund propose blocked: cannot read fund state to check the fund ceilings ({scrub_paths(exc)})"
        if not states:
            return None  # no fund on this host: the general advisory check applies
        by_vault = {st.fund.vault.lower(): st for st in states}
        low = command.lower()
        present = {v for v in by_vault if v in low}
        invocations = parse_propose_invocations(command)
        if invocations is None:
            if present or looks_like_propose(command):
                return "fund propose blocked (unreadable_command): this command cannot be split into shell commands with confidence (unbalanced quotes)"
            return None
        if (present or invocations) and has_unquoted_hash(command):
            return (
                "fund propose blocked (unquoted_hash): a # outside quotes makes bash and this guard read "
                "different flags; quote it (the commands fund_pending_commands hands out already do)"
            )
        names = ", ".join(sorted(st.fund.name for st in states))
        claimed: set[str] = set()
        checked = False
        for inv in invocations:
            if inv.help:
                claimed.update(v.lower() for v in inv.vaults)  # prints usage, proposes nothing
                continue
            if not inv.vault:
                return (
                    f"fund propose blocked (no_vault): this host runs fund {names}; CLI 0.103.0 requires "
                    "--vault on a propose, and the guard needs it to apply the fund ceilings"
                )
            if any(not is_address(v) for v in inv.vaults):
                bad = next(v for v in inv.vaults if not is_address(v))
                return (
                    f"fund propose blocked (vault_not_literal): --vault {bad!r} is not a literal 0x address, so "
                    f"the guard cannot tell whether it is {names}'s vault (the CLI takes only an address)"
                )
            st = by_vault.get(inv.vault.lower())
            if st is None:
                continue  # another vault: the general advisory check applies
            reason = check_propose_ceilings(inv, st)
            if reason:
                return reason
            claimed.add(inv.vault.lower())
            checked = True
        unclaimed = present - claimed
        if unclaimed:
            fund_names = ", ".join(sorted(by_vault[v].fund.name for v in unclaimed))
            return (
                f"fund propose blocked (unreadable_propose): this command names {fund_names}'s vault and "
                "looks like a propose, but no `sherwood strategy propose` / `sherwood proposal create` in it "
                "takes that vault as a literal --vault the guard can read (bash -c, eval, $(...) or a variable). "
                "Run the command as fund_pending_commands lists it"
            )
        proposing = [inv for inv in invocations if not inv.help]
        if checked and all(inv.vault.lower() in by_vault for inv in proposing):
            return FUND_RULES_APPLIED
        return None

    def guard(tool_name: str, args: dict[str, Any]) -> Any:
        blocked = rebind_block(tool_name, args)
        if blocked:
            return blocked
        if tool_name in TERMINAL_TOOLS:
            return terminal_propose(args)
        if tool_name not in GUARDED_TOOLS:
            return None
        if tool_name != "fund_draft_proposal":
            return host_directive(tool_name, args)
        try:
            fund = rt.fund_for((args or {}).get("fund"))
            state = rt.service.load(fund)
            d = parse_draft_args(args or {}, state)
            if d.generic_call:
                require_agent_signer(rt.service.proposal_signer)
            verdict = rules.check_draft(
                state,
                rt.agent_name(),
                d.topic,
                max_capital=d.max_capital,
                max_drawdown_bps=d.max_drawdown_bps,
                legs=d.legs,
                now=rt.service.clock(),
                require_portfolio=rt.service.proposal_signer == "agent" and not d.generic_call,
            )
        except RuleViolation as exc:
            return f"fund draft blocked ({exc.code}): {exc.reason}"
        except GUARD_ERRORS as exc:
            return f"fund draft blocked: cannot verify it against fund state ({exc})"
        if verdict.ok:
            return None
        return f"fund draft blocked ({verdict.code}): {verdict.reason}"

    return guard
