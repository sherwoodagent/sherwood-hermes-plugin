"""Read `sherwood strategy propose` / `sherwood proposal create` out of a terminal command.

Shared by the pre_tool_call risk hook (hooks.py) and the fund guard (fund/guard.py),
so both see the same flags. No imports from either: this module only parses.

This reads the agent's own command text the way bash splits it into commands
(newlines, `;`, `&&`, `||`, `|`, `|&`, `&`, `(`, `)` all end a command; a
redirection such as `2>&1` does not). It does not run the shell: a command built
at run time (`bash -c`, `eval`, `$(...)`, a variable) is not expanded, which the
fund guard handles by refusing what it cannot read (see fund/guard.py).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

TERMINAL_TOOLS = frozenset({"bash", "terminal", "shell"})

# Every flag that carries vault-asset capital on a CLI 0.103.0 propose, in human units.
CAPITAL_FLAGS = ("--amount", "--max-capital", "--deposit", "--asset-in", "--collateral-amount", "--borrow-amount")
DRAWDOWN_FLAG = "--max-drawdown-bps"
# `--max-drawdown-bps` omitted: the CLI proposes with 2000 (its help text, 0.103.0).
CLI_DEFAULT_DRAWDOWN_BPS = 2000
HELP_FLAGS = frozenset({"-h", "--help"})

_PROPOSE_RE = re.compile(r"\bsherwood\b.*?\b(?:strategy\s+propose|proposal\s+create)\b", re.DOTALL)
# Words a propose is spelled with once quotes and backslashes are gone (`strategy 'propose'`, `pro\pose`).
_PROPOSE_WORDS_RE = re.compile(r"\bpropose\b|\bproposal\b[\s\S]*\bcreate\b")
_PUNCTUATION = "();<>|&\n"
_REDIRECTION_RE = re.compile(r"^(?:[<>]{1,3}&?|&>>?|<>)$")  # `>`, `>>`, `2>&1`'s `>&`, `<<<`, `&>`
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CLI_PACKAGE = "@sherwoodagent/cli"


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
    vault: str = ""  # --vault value as typed ("" when absent); the last one wins, as in the CLI
    vaults: list[str] = field(default_factory=list)  # every --vault value, in order
    capital: list[tuple[str, str]] = field(default_factory=list)  # (flag, raw value)
    drawdowns: list[str] = field(default_factory=list)  # every raw --max-drawdown-bps value
    size_usd: str | None = None  # legacy --size-usd
    protocol: str = ""
    positional: str = ""  # first positional after the subcommand (legacy fund subdomain)
    help: bool = False  # -h / --help: prints usage, proposes nothing

    @property
    def drawdown(self) -> str | None:
        return self.drawdowns[-1] if self.drawdowns else None


def looks_like_propose(command: str) -> bool:
    return bool(_PROPOSE_RE.search(command or ""))


def mentions_propose(command: str) -> bool:
    """The command spells a propose once quotes and backslashes are dropped (it may hide it in
    `bash -c '...'`, `eval`, a quoted or escaped subcommand word)."""
    return bool(_PROPOSE_WORDS_RE.search(re.sub(r"[\\'\"]", "", command or "")))


def _lex(command: str, *, comments: bool) -> list[str]:
    lexer = shlex.shlex(command.replace("\\\n", ""), posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"  # a newline ends a command: it is a punctuation token here
    lexer.whitespace_split = True
    lexer.commenters = "#" if comments else ""
    return list(lexer)


def tokens(command: str) -> list[str] | None:
    """The command's words and operators (no comment stripping), or None if quotes do not balance."""
    try:
        return _lex(command, comments=False)
    except ValueError:
        return None


def has_unquoted_hash(command: str) -> bool:
    """A `#` outside quotes. bash reads it as a comment only at the start of a word, a parser as it
    likes; either way the two can disagree on the flags, so a fund propose with one is refused."""
    try:
        return _lex(command, comments=True) != _lex(command, comments=False)
    except ValueError:
        return True


def _is_separator(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCTUATION for c in tok) and not _REDIRECTION_RE.match(tok)


def _is_sherwood(tok: str) -> bool:
    """The CLI as a word: `sherwood`, a path to it, `npx @sherwoodagent/cli[@x]`, or
    `node .../@sherwoodagent/cli/dist/index.js`."""
    return (
        tok == "sherwood"
        or tok.endswith("/sherwood")
        or tok == CLI_PACKAGE
        or tok.startswith(CLI_PACKAGE + "@")
        or (CLI_PACKAGE + "/") in tok
    )


def parse_propose_invocations(command: str) -> list[ProposeInvocation] | None:
    """Every propose invocation in `command`, one per shell command; [] when there is none;
    None when the command cannot be tokenised (unbalanced quotes)."""
    low = (command or "").lower()
    if "sherwood" not in low:
        return []
    toks = tokens(command)
    if toks is None:
        return None
    out: list[ProposeInvocation] = []
    segment: list[str] = []
    for tok in [*toks, ";"]:
        if not _is_separator(tok):
            segment.append(tok)
            continue
        for i, word in enumerate(segment):
            if _is_sherwood(word):
                inv = _parse_one(segment[i + 1 :])
                if inv is not None:
                    out.append(inv)
                break
        segment = []
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
    inv = ProposeInvocation(kind=kind, help=any(a in HELP_FLAGS for a in args))
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
        value, used = _flag(tok, DRAWDOWN_FLAG, nxt)
        if used:
            inv.drawdowns.append(value or "")
            k += used
            continue
        value, used = _flag(tok, "--vault", nxt)
        if used:
            inv.vaults.append(value or "")
            inv.vault = value or ""
            k += used
            continue
        for name, attr in (("--size-usd", "size_usd"), ("--protocol", "protocol")):
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
