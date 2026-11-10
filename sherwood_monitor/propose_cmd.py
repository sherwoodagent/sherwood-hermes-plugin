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
# The guard reads a command in time linear in its size; past this it refuses instead of parsing.
MAX_COMMAND_CHARS = 32 * 1024
# Words that may stand before the CLI in its command and still run it with exactly the words
# that follow (anything else, e.g. `xargs`, may add arguments the guard cannot see).
SAFE_LAUNCHERS = frozenset({
    "env", "npx", "pnpm", "dlx", "yarn", "bunx", "exec", "command", "time", "nohup", "node", "nice", "timeout",
    # shell keywords that start a command without adding words to it
    "if", "then", "else", "elif", "do", "while", "until", "!", "{",
})
_LAUNCHER_ARG_RE = re.compile(r"^(?:-[\w-]*|[A-Za-z_][A-Za-z0-9_]*=.*|\d+[smhd]?)$")  # -y, X=1, 60, 5m

_SHERWOOD_WORD_RE = re.compile(r"\bsherwood\b")
_SUBCOMMAND_RE = re.compile(r"\b(?:strategy\s+propose|proposal\s+create)\b")
_PROPOSE_WORD_RE = re.compile(r"\bpropose\b")
_PROPOSAL_WORD_RE = re.compile(r"\bproposal\b")
_CREATE_WORD_RE = re.compile(r"\bcreate\b")
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
    # -h / --help before `--` on an invocation with no --vault and no capital flag: usage only. A
    # help token next to --vault or an amount may be an option's value (`--name -h`), which
    # commander runs, so such an invocation is never treated as help.
    help: bool = False
    launcher_ok: bool = True  # only an env assignment or a SAFE_LAUNCHERS word precedes the CLI

    @property
    def drawdown(self) -> str | None:
        return self.drawdowns[-1] if self.drawdowns else None


def looks_like_propose(command: str) -> bool:
    """`sherwood` followed somewhere by `strategy propose` / `proposal create` (linear time)."""
    text = command or ""
    m = _SHERWOOD_WORD_RE.search(text)
    return bool(m and _SUBCOMMAND_RE.search(text, m.end()))


def mentions_propose(command: str) -> bool:
    """The command spells a propose once quotes and backslashes are dropped (it may hide it in
    `bash -c '...'`, `eval`, a quoted or escaped subcommand word). Linear time."""
    text = re.sub(r"[\\'\"]", "", command or "")
    if _PROPOSE_WORD_RE.search(text):
        return True
    m = _PROPOSAL_WORD_RE.search(text)
    return bool(m and _CREATE_WORD_RE.search(text, m.end()))


def shell_expansion(command: str) -> str | None:
    """The first thing bash would expand or substitute into the command's words, or None.

    Reads quoting the way bash does: inside single quotes nothing expands; inside double quotes `$`
    and backticks still do; a backslash outside single quotes escapes the next character. Reported:
    `$` (variables, `$(...)`, `$'...'`, `${...}`, arithmetic), backticks, an unquoted `{` that starts a
    brace expansion (`{a,b}`, `{1..3}`), unquoted glob characters (`*`, `?`, `[`) and process
    substitution (`<(`, `>(`). Each can hand the CLI words the guard never read.
    """
    text = command or ""
    single = double = False
    i = 0
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if single:
            if c == "'":
                single = False
        elif c == "\\":
            i += 2
            continue
        elif c == "'" and not double:
            single = True
        elif c == '"':
            double = not double
        elif c == "$":
            return "a `$` expansion"
        elif c == "`":
            return "a backtick substitution"
        elif not double:
            if c == "{" and nxt and not nxt.isspace():
                return "a brace expansion"
            if c in "*?[":
                return f"an unquoted glob character {c!r}"
            if c in "<>" and nxt == "(":
                return "a process substitution"
        i += 1
    return None


def _join_continuations(text: str) -> str:
    """Drop a backslash-newline where bash does: outside single quotes, and only when the
    backslash is not itself escaped (`x\\\\` then a newline is an escaped backslash and a newline)."""
    out: list[str] = []
    single = double = False
    i = 0
    while i < len(text):
        c = text[i]
        if single:
            single = c != "'"
            out.append(c)
        elif c == "\\":
            pair = text[i : i + 2]
            if pair != "\\\n":
                out.append(pair)
            i += 2
            continue
        else:
            if c == "'" and not double:
                single = True
            elif c == '"':
                double = not double
            out.append(c)
        i += 1
    return "".join(out)


def _lex(command: str, *, comments: bool) -> list[str]:
    lexer = shlex.shlex(_join_continuations(command), posix=True, punctuation_chars=_PUNCTUATION)
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
    `node .../@sherwoodagent/cli/dist/index.js`. Case-insensitive for the binary name: macOS
    file systems run `SHERWOOD` as `sherwood`."""
    low = tok.lower()
    return (
        low == "sherwood"
        or low.endswith("/sherwood")
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
                    inv.launcher_ok = all(_launcher_word(w) for w in segment[:i])
                    out.append(inv)
                break
        segment = []
    return out


def _launcher_word(word: str) -> bool:
    return word.rsplit("/", 1)[-1] in SAFE_LAUNCHERS or bool(_LAUNCHER_ARG_RE.match(word))


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
    before_end = args[: args.index("--")] if "--" in args else args  # after `--` commander sees operands
    help_token = any(a in HELP_FLAGS for a in before_end)
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
        # Legacy flags of the advisory check only: never let one swallow a following flag.
        legacy_nxt = nxt if nxt is not None and not nxt.startswith("-") else None
        for name, attr in (("--size-usd", "size_usd"), ("--protocol", "protocol")):
            value, used = _flag(tok, name, legacy_nxt)
            if used:
                setattr(inv, attr, value or "")
                k += used
                matched = True
                break
        if matched:
            continue
        k += 1
    inv.help = help_token and not inv.vaults and not inv.capital
    return inv


def is_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value or ""))
