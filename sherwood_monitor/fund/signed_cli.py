"""Sherwood CLI commands for `proposal_signer: agent` (the open protocol).

Under the open protocol the host is the Hermes agent's own wallet: the one the
`sherwood` CLI holds in its own config. The plugin never reads that key and
never signs or sends anything. It builds the exact command the agent runs with
its terminal tool, outside the group chat (a `hermes -z` one-shot or a cron the
operator configured in approve mode; the Telegram toolset has no terminal), and
records the result afterwards (`fund_set_proposal_status ... proposed`, or
`fund_approve_agent` with the tx hash). The CLI signs with its wallet.

Quoting: every argument goes through `shlex.quote`, so a quote, `$(...)` or a
backtick in the thesis stays inert in the shell. Free text that could start
with `-` (the name and the thesis) is passed in the attached form,
`--name=<value>` / `--description=<value>`: the CLI's option parser
(commander) scans every argument before the subcommand does, so a separate
value such as `--chain=robinhood-fork` or `-V` would be read as a flag. Every
other value is validated to never start with `-`.

Flags checked against `sherwood strategy propose --help` and
`sherwood vault add --help` (CLI 0.88.2 and 0.103.0). No `--chain` is emitted:
Robinhood Chain mainnet is the CLI's default and only network from 0.93.0 on,
and 0.93.0 shipped with zero bond-escrow addresses on the propose path, so the
floor for these commands is 0.103.0, the release the flags were checked
against (`MIN_CLI_FOR_COMMANDS`). `MIN_CLI_VERSION` (preflight) stays lower:
the monitor itself needs less.
"""
from __future__ import annotations

import logging
import re
import shlex

from .models import Agent, Fund, ProposalRecord
from .rules import RuleViolation, deny, is_address
from .units import format_units

_log = logging.getLogger(__name__)

MIN_CLI_FOR_COMMANDS = "0.103.0"
CLI_NOTE = (
    f"Run it with the Sherwood CLI {MIN_CLI_FOR_COMMANDS} or later (check `sherwood --version`), its wallet signs. "
    "Run it only in a `hermes -z` one-shot the operator started for it, or a cron the operator configured with "
    "`approvals.cron_mode: approve`: a cron in deny mode cannot record the result, and an unrecorded propose is "
    "listed again. Never from the group chat, never on boot."
)
# A propose command handed out by fund_pending_commands is held back this long (a run that could
# not record its result would otherwise propose the same draft again).
HANDOUT_HOLD_SECONDS = 1800
# A shipped command older than this is listed as expired, not runnable: the host ships it again.
SHIP_TTL_SECONDS = 86400
MAX_LEGS = 20  # prepare/propose-portfolio takes 1..20 legs; the CLI path matches it

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_AGENT_ID_RE = re.compile(r"^[0-9]{1,78}$")


def cli_duration(seconds: int) -> str:
    """Seconds in the CLI's `--duration` form (`^(\\d+)(d|h|m|s)?$`): 86400 -> 1d, 21600 -> 6h, 90 -> 90s."""
    s = int(seconds)
    if s <= 0:
        raise ValueError("duration must be positive")
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s % size == 0:
            return f"{s // size}{unit}"
    return f"{s}s"


def proposal_name(fund: Fund, record: ProposalRecord) -> str:
    """The proposal name: the same one the prepare/propose-portfolio body carries."""
    return f"{fund.name}: {record.topic or record.thesis[:40]}"


def _not_ready(record: ProposalRecord, what: str) -> RuleViolation:
    return RuleViolation(deny("draft_not_ready", f"{record.id} {what}"))


def propose_portfolio_command(fund: Fund, record: ProposalRecord) -> str:
    """`sherwood strategy propose portfolio ...` for a shipped PortfolioStrategy draft.

    Raises RuleViolation when the draft cannot be one (no vault, no 1..20
    distinct symbol + weight legs summing to 10000, no duration, no valid
    drawdown). `--max-drawdown-bps` carries the drawdown the fund rules
    checked: the CLI's default is 2000.
    """
    if not is_address(fund.vault):
        raise RuleViolation(deny("no_vault", "the fund has no vault address yet"))
    execute = [leg for leg in record.legs if leg.phase == "execute"]
    weights = [leg.weight_bps for leg in execute]
    if not execute or not all(leg.symbol and w > 0 for leg, w in zip(execute, weights)) or sum(weights) != 10_000:
        raise RuleViolation(deny(
            "not_portfolio",
            "with proposal_signer: agent the host agent proposes with `sherwood strategy propose portfolio`: "
            "the draft needs legs with symbol + weight_bps summing to 10000",
        ))
    if len(execute) > MAX_LEGS:
        raise RuleViolation(deny("too_many_legs", f"a portfolio takes at most {MAX_LEGS} legs, not {len(execute)}"))
    symbols = [leg.symbol.upper() for leg in execute]
    for symbol in symbols:
        if not _SYMBOL_RE.match(symbol):
            raise RuleViolation(deny("bad_symbol", f"{symbol!r} is not a token symbol"))
    if len(set(symbols)) != len(symbols):
        raise RuleViolation(deny("duplicate_symbol", f"each symbol appears once in a portfolio: {','.join(symbols)}"))
    if not isinstance(record.strategy_duration, int) or record.strategy_duration <= 0:
        raise _not_ready(record, "has no strategy duration yet")
    try:
        capital = int(record.max_capital or 0)
    except (TypeError, ValueError):
        capital = 0
    if capital <= 0:
        raise _not_ready(record, "has no max capital yet")
    drawdown = record.max_drawdown_bps
    if isinstance(drawdown, bool) or not isinstance(drawdown, int) or not 0 <= drawdown <= 10_000:
        raise _not_ready(record, f"has no valid max drawdown (0-10000 bps, got {drawdown!r})")
    args = [
        "sherwood", "strategy", "propose", "portfolio",
        "--vault", fund.vault,
        "--tokens", ",".join(symbols),
        "--weights", ",".join(str(w) for w in weights),
        "--amount", format_units(capital, fund.asset_decimals),
        "--duration", cli_duration(record.strategy_duration),
        "--max-drawdown-bps", str(drawdown),
        # Attached form: the value can never be read as a flag, whatever it starts with.
        "--name=" + " ".join(proposal_name(fund, record).split()),
        "--description=" + " ".join(record.thesis.split()),
    ]
    return shlex.join(args)


def register_agent_command(fund: Fund, agent: Agent) -> str:
    """`sherwood vault add ...`: the vault owner registers a guest agent (registerAgent)."""
    if not is_address(fund.vault):
        raise RuleViolation(deny("no_vault", "the fund has no vault address yet"))
    if not is_address(agent.address):
        raise RuleViolation(deny("missing_identity", f"{agent.name} has no 0x wallet address on record"))
    if not _AGENT_ID_RE.match(str(agent.identity_id or "")):
        raise RuleViolation(deny("missing_identity", f"{agent.name} has no ERC-8004 identity id on record"))
    return shlex.join([
        "sherwood", "vault", "add",
        "--vault", fund.vault,
        "--wallet", agent.address,
        "--agent-id", str(agent.identity_id),
    ])


def cli_version_warning(sherwood_bin: str, min_version: str = MIN_CLI_FOR_COMMANDS) -> str | None:
    """A warning when the installed CLI is below ``min_version`` (read-only `sherwood --version`,
    through preflight's check). None when it is new enough. Never raises: a failed check is a warning.

    Portfolio commands use ``MIN_CLI_FOR_COMMANDS`` (0.103.0). A pending generic-call command
    passes 0.104.0, the CLI that added ``strategy propose generic-call``. The preflight floor
    stays lower: the monitor itself does not need the newer command.
    """
    try:
        from ..preflight import check_cli_installed

        ok, version = check_cli_installed(sherwood_bin or "sherwood", min_version)
    except Exception as exc:  # the listing never fails on the check
        _log.warning("sherwood --version check failed: %s", exc)
        return f"could not check the Sherwood CLI version; these commands need {min_version} or later"
    if ok:
        return None
    if version is None:
        return (
            f"could not read `{sherwood_bin} --version`; these commands need the Sherwood CLI "
            f"{min_version} or later"
        )
    reasons = _cli_shortfalls(version, min_version)
    return (
        f"the installed Sherwood CLI is {version}, below {min_version}: upgrade it before running these "
        f"commands ({'; '.join(reasons)})"
    )


# What each floor means for the commands this module builds, oldest first: (the release that
# fixed it, what a CLI below it lacks). Only the ones below the installed version are named.
_CLI_MILESTONES = (
    ("0.93.0", "it targets another network: Robinhood Chain mainnet is the only network from 0.93.0"),
    ("0.103.0", "it predates 0.103.0, the release the propose flags and bond escrow were checked against "
                "(0.93.0 shipped without the bond escrow addresses)"),
    ("0.104.0", "it cannot propose generic-call, which 0.104.0 added"),
)


def _cli_shortfalls(version: str, min_version: str) -> list[str]:
    """What an installed ``version`` below ``min_version`` lacks, naming only what applies to it."""
    from ..preflight import _parse_version

    try:
        have, need = _parse_version(version), _parse_version(min_version)
    except ValueError:
        return [f"this plugin was checked against {min_version}"]
    reasons = [text for fixed, text in _CLI_MILESTONES if have < _parse_version(fixed) <= need]
    return reasons or [f"this plugin was checked against {min_version}"]


__all__ = [
    "CLI_NOTE",
    "HANDOUT_HOLD_SECONDS",
    "MAX_LEGS",
    "MIN_CLI_FOR_COMMANDS",
    "SHIP_TTL_SECONDS",
    "cli_duration",
    "cli_version_warning",
    "propose_portfolio_command",
    "proposal_name",
    "register_agent_command",
]
