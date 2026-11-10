"""Read `sherwood strategy propose` / `sherwood proposal create` out of a terminal command.

Shared by the pre_tool_call risk hook (hooks.py) and the fund guard (fund/guard.py),
so both see the same flags. No imports from either: this module only parses.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

TERMINAL_TOOLS = frozenset({"bash", "terminal", "shell"})

# The capital a propose deploys, in human asset units, by CLI 0.103.0 flag:
# `strategy propose --amount` (all templates; `--deposit` is its Lighter alias,
# `--asset-in` the launchpad budget) and `proposal create --max-capital`.
CAPITAL_FLAGS = ("--amount", "--max-capital", "--deposit", "--asset-in")
DRAWDOWN_FLAG = "--max-drawdown-bps"
# `--max-drawdown-bps` omitted: the CLI proposes with 2000 (its help text, 0.103.0).
CLI_DEFAULT_DRAWDOWN_BPS = 2000

_PROPOSE_RE = re.compile(r"\bsherwood\b.*?\b(?:strategy\s+propose|proposal\s+create)\b", re.S)
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", ";;"})
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class _FundRulesApplied:
    """A fund guard's answer for a terminal propose on a fund vault that passed the fund's own
    ceilings: allow it, and do not run the general position-sizing rule on top."""

    def __repr__(self) -> str:
        return "FUND_RULES_APPLIED"


FUND_RULES_APPLIED = _FundRulesApplied()


@dataclass
class ProposeInvocation:
    """One `sherwood ... propose` in a command line."""

    kind: str  # "strategy propose" or "proposal create"
    template: str = ""  # the strategy template (strategy propose), else ""
    vault: str = ""  # --vault value as typed ("" when absent)
    capital: list[tuple[str, str]] = field(default_factory=list)  # (flag, raw value)
    drawdown: str | None = None  # raw --max-drawdown-bps value, None when absent
    size_usd: str | None = None  # legacy --size-usd
    protocol: str = ""
    positional: str = ""  # first positional after the subcommand (legacy fund subdomain)


def looks_like_propose(command: str) -> bool:
    return bool(_PROPOSE_RE.search(command or ""))


def _tokens(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return None


def _is_sherwood(tok: str) -> bool:
    return tok == "sherwood" or tok.endswith("/sherwood")


def parse_propose_invocations(command: str) -> list[ProposeInvocation] | None:
    """Every propose invocation in `command`; [] when there is none; None when the command
    looks like a propose but cannot be tokenised (unbalanced quotes)."""
    if not looks_like_propose(command):
        return []
    tokens = _tokens(command)
    if tokens is None:
        return None
    out: list[ProposeInvocation] = []
    i = 0
    while i < len(tokens):
        if not _is_sherwood(tokens[i]):
            i += 1
            continue
        end = i + 1
        while end < len(tokens) and tokens[end] not in _SEPARATORS:
            end += 1
        inv = _parse_one(tokens[i + 1 : end])
        if inv is not None:
            out.append(inv)
        i = end
    return out


def _flag(tok: str, name: str, nxt: str | None) -> tuple[str | None, int]:
    """(value, tokens consumed) for `--name value` / `--name=value`, else (None, 0)."""
    if tok == name:
        return (nxt, 2) if nxt is not None else ("", 1)
    if tok.startswith(name + "="):
        return tok[len(name) + 1 :], 1
    return None, 0


def _parse_one(args: list[str]) -> ProposeInvocation | None:
    # Find the subcommand pair; anything before it is a global option (`--chain x`, `--calldata-only`).
    kind = ""
    start = 0
    for j in range(len(args) - 1):
        pair = (args[j], args[j + 1])
        if pair == ("strategy", "propose") or pair == ("proposal", "create"):
            kind = " ".join(pair)
            start = j + 2
            break
    if not kind:
        return None
    inv = ProposeInvocation(kind=kind)
    rest = args[start:]
    if rest and not rest[0].startswith("-"):
        inv.positional = rest[0]  # the template (strategy propose) or a legacy fund subdomain
        if kind == "strategy propose":
            inv.template = rest[0]
    k = 0
    while k < len(rest):
        tok = rest[k]
        nxt = rest[k + 1] if k + 1 < len(rest) else None
        matched = False
        for name in CAPITAL_FLAGS:
            value, used = _flag(tok, name, nxt)
            if used:
                inv.capital.append((name, value or ""))
                k += used
                matched = True
                break
        if matched:
            continue
        for name, attr in ((DRAWDOWN_FLAG, "drawdown"), ("--vault", "vault"), ("--size-usd", "size_usd"),
                           ("--protocol", "protocol")):
            value, used = _flag(tok, name, nxt)
            if used:
                setattr(inv, attr, value or "")
                k += used
                matched = True
                break
        if matched:
            continue
        k += 1
    return inv


def is_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value or ""))
