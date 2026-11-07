"""Signed Sherwood CLI commands for `proposal_signer: agent` (the open protocol).

Under the open protocol the host is the Hermes agent's own wallet: the one the
signed `sherwood` CLI holds in its own config. The plugin never reads that key
and never signs or sends anything. It builds the exact command the agent runs
with its terminal tool, outside the group chat (a Hermes cron run or a
`hermes -z` one-shot; the Telegram toolset has no terminal), and records the
result afterwards (`fund_set_proposal_status ... proposed`, or
`fund_approve_agent` with the tx hash).

Every argument goes through `shlex.quote`: the thesis is free text, so a quote,
`$(...)` or a backtick in it stays inert data in the shell.

Flags checked against `sherwood strategy propose --help` and
`sherwood vault add --help` (CLI 0.88.2 and 0.103.0). No `--chain` is emitted:
Robinhood Chain mainnet (4663) is the CLI's default and only network from
0.93.0 on, and older releases default to a fork and reject the mainnet name,
so the agent's CLI must be 0.93.0 or later (`CLI_NOTE`).
"""
from __future__ import annotations

import re
import shlex

from .models import Agent, Fund, ProposalRecord
from .rules import RuleViolation, deny, is_address
from .units import format_units

# The first CLI release whose default (and only) network is Robinhood Chain mainnet.
MIN_CLI_FOR_COMMANDS = "0.93.0"
CLI_NOTE = (
    f"Run it with the signed Sherwood CLI {MIN_CLI_FOR_COMMANDS} or later (Robinhood Chain mainnet is its default "
    "network; check `sherwood --version`), from a Hermes cron run or `hermes -z`, never from the group chat. "
    "It signs with the wallet the CLI holds, not through the plugin."
)

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


def propose_portfolio_command(fund: Fund, record: ProposalRecord) -> str:
    """`sherwood strategy propose portfolio ...` for a shipped PortfolioStrategy draft.

    Raises RuleViolation when the draft cannot be one (no vault, no symbol +
    weight legs summing to 10000, no duration). `--max-drawdown-bps` carries
    the drawdown the fund rules checked: the CLI's default is 2000.
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
    for leg in execute:
        if not _SYMBOL_RE.match(leg.symbol):
            raise RuleViolation(deny("bad_symbol", f"{leg.symbol!r} is not a token symbol"))
    if record.strategy_duration <= 0:
        raise RuleViolation(deny("draft_not_ready", f"{record.id} has no strategy duration yet"))
    if int(record.max_capital or 0) <= 0:
        raise RuleViolation(deny("draft_not_ready", f"{record.id} has no max capital yet"))
    args = [
        "sherwood", "strategy", "propose", "portfolio",
        "--vault", fund.vault,
        "--tokens", ",".join(leg.symbol.upper() for leg in execute),
        "--weights", ",".join(str(w) for w in weights),
        "--amount", format_units(record.max_capital, fund.asset_decimals),
        "--duration", cli_duration(record.strategy_duration),
        "--max-drawdown-bps", str(int(record.max_drawdown_bps)),
        "--name", " ".join(proposal_name(fund, record).split()),
        "--description", " ".join(record.thesis.split()),
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


__all__ = [
    "CLI_NOTE",
    "MIN_CLI_FOR_COMMANDS",
    "cli_duration",
    "propose_portfolio_command",
    "proposal_name",
    "register_agent_command",
]
