"""`hermes sherwood <cmd>` CLI commands."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .supervisor import Supervisor

# The five crons registered by `hermes sherwood install-cron`:
#   - four no_agent scripts (cheap, deterministic alerts)
#   - one agent cron (reasoning over open proposals; only this one costs tokens)
#
# Tests assert against these constants — change a name/schedule here, expect
# `tests/test_cli.py` to need an update.
PROPOSAL_REASONING_PROMPT = (
    "Open proposals review. For each fund in "
    "~/.hermes/plugins/sherwood-monitor/config.yaml, list any proposals that "
    "are currently in Pending or GuardianReview state. For each: summarize "
    "what it does, which protocol/contract it touches, the size relative to "
    "vault AUM, and your vote recommendation (FOR / AGAINST / ABSTAIN) with a "
    "one-sentence rationale grounded in the fund's prior positions and "
    "the proposed strategy's risk profile. If there are NO open proposals "
    "across all funds, reply with the single word HEARTBEAT_OK so the "
    "cron stays silent."
)


@dataclass(frozen=True)
class _CronSpec:
    name: str
    schedule: str
    mode: str  # "no_agent" or "agent"
    payload: str  # script-arg subcommand for no_agent, prompt for agent


_WATCHDOG_SCRIPT = "scripts/watchdog.sh"  # relative to plugin root

CRONS: tuple[_CronSpec, ...] = (
    _CronSpec("sherwood-monitor-digest",     "*/15 * * * *", "no_agent", "digest"),
    _CronSpec("sherwood-aum-watchdog",       "*/15 * * * *", "no_agent", "aum"),
    _CronSpec("sherwood-gas-watchdog",       "*/30 * * * *", "no_agent", "gas"),
    _CronSpec("sherwood-stream-watchdog",    "*/5 * * * *",  "no_agent", "stream"),
    _CronSpec("sherwood-proposal-reasoning", "0 */6 * * *",  "agent",    PROPOSAL_REASONING_PROMPT),
)

# Public constants for tests that want to know the per-cron names without
# walking the tuple shape.
CRON_NAMES: tuple[str, ...] = tuple(c.name for c in CRONS)

# Back-compat constants — the previous single-cron registration used these
# names. Kept as aliases so any external script that imports them still works,
# pointing at the new digest cron (closest semantic equivalent).
CRON_NAME = "sherwood-monitor-digest"
CRON_SCHEDULE = "*/15 * * * *"
CRON_PROMPT = (
    "[Deprecated alias — the digest cron is now a no_agent script. "
    "See sherwood_monitor.cli.PROPOSAL_REASONING_PROMPT for the active agent prompt.]"
)


def _plugin_root() -> Path:
    """Locate the plugin install root so we can build absolute script paths.

    The Hermes harness invokes cron scripts via their absolute path, so we
    need to resolve `scripts/watchdog.sh` to a path that exists at registration
    time. Two layouts:
      1. Editable install (`pip install -e .`) — the `scripts/` dir lives next
         to the package source.
      2. Wheel install — the `scripts/` dir lives in the plugin's
         `~/.hermes/plugins/sherwood-monitor/` mirror.
    Prefer (1) when running from a checkout, fall back to (2).
    """
    src_candidate = Path(__file__).resolve().parent.parent
    if (src_candidate / _WATCHDOG_SCRIPT).exists():
        return src_candidate
    return Path.home() / ".hermes" / "plugins" / "sherwood-monitor"


def _run_hermes(args: list[str]) -> tuple[int, str, str]:
    """Run the host `hermes` binary. Returns (rc, stdout, stderr).

    rc=127 + empty stdout/stderr signals "binary not on PATH" — the caller
    surfaces a clear install hint instead of a Python traceback.
    """
    hermes = shutil.which("hermes")
    if hermes is None:
        return 127, "", ""
    res = subprocess.run([hermes, *args], capture_output=True, text=True, timeout=15)
    return res.returncode, res.stdout, res.stderr


def _registered_cron_names() -> set[str]:
    """Names of crons currently registered with Hermes. Empty set on error.

    `hermes cron list` output isn't documented as JSON. Match by whole token
    so ``sherwood-monitor-foo`` doesn't false-match ``sherwood-monitor-bar``.
    Worst case (false negative): we attempt a redundant ``cron create`` which
    surfaces its own conflict error — never silently creates a duplicate.
    """
    import re

    rc, stdout, _ = _run_hermes(["cron", "list"])
    if rc != 0:
        return set()
    tokens = set(re.findall(r"[A-Za-z0-9_-]+", stdout))
    return tokens & set(CRON_NAMES)


def _create_cron(spec: _CronSpec, plugin_root: Path) -> tuple[bool, str]:
    cmd = ["cron", "create", "--name", spec.name, "--schedule", spec.schedule]
    if spec.mode == "no_agent":
        script_path = plugin_root / _WATCHDOG_SCRIPT
        cmd += ["--no-agent", "--script", str(script_path), spec.payload]
    else:
        cmd += ["--prompt", spec.payload]
    rc, _stdout, stderr = _run_hermes(cmd)
    if rc == 0:
        return True, ""
    return False, (stderr or "").strip() or f"rc={rc}"


# `hermes sherwood <sub>`: (help, takes a fund subdomain)
SUBCOMMANDS: dict[str, tuple[str, bool]] = {
    "start": ("start monitoring a fund", True),
    "stop": ("stop monitoring a fund", True),
    "status": ("show monitor status", False),
    "tail": ("tail stderr of a monitor", True),
    "install-cron": ("register all sherwood crons with Hermes (no_agent watchdogs + reasoning; idempotent)", False),
}


def build_cli_handlers(sup: Supervisor) -> dict[str, Callable[[Any], int]]:
    """The `hermes sherwood <sub>` handlers, keyed by subcommand name."""

    def start_handler(args: Any) -> int:
        async def _start() -> None:
            await sup.start(args.subdomain)

        asyncio.run(_start())
        return 0

    def stop_handler(args: Any) -> int:
        async def _stop() -> None:
            await sup.stop(args.subdomain)

        asyncio.run(_stop())
        return 0

    def status_handler(_args: Any) -> int:
        print(json.dumps(sup.status()))
        return 0

    def tail_handler(args: Any) -> int:
        if not args.subdomain:
            print("subdomain required", flush=True)
            return 2
        lines = sup.stderr_tail(args.subdomain)
        for line in lines:
            print(line)
        return 0

    def install_cron_handler(_args: Any) -> int:
        """Idempotently register every Sherwood cron with Hermes.

        Reports per-entry status as JSON so a human or follow-up tool can
        see what changed without parsing stderr.
        """
        hermes = shutil.which("hermes")
        if hermes is None:
            print(json.dumps({"installed": False, "error": "hermes binary not found on PATH"}))
            return 1

        # Skip the reasoning cron when explicitly disabled in config — keeps
        # token-sensitive operators in control of when LLM crons fire.
        try:
            cfg = load_config(Path.home() / ".hermes" / "plugins" / "sherwood-monitor" / "config.yaml")
            reasoning_enabled = cfg.proposal_reasoning_enabled
        except Exception:
            reasoning_enabled = True

        existing = _registered_cron_names()
        plugin_root = _plugin_root()

        result: dict[str, Any] = {"installed": [], "skipped": [], "errors": []}

        for spec in CRONS:
            if spec.mode == "agent" and not reasoning_enabled:
                result["skipped"].append({"name": spec.name, "reason": "proposal_reasoning_enabled=false"})
                continue
            if spec.name in existing:
                result["skipped"].append({"name": spec.name, "reason": "already_registered"})
                continue
            ok, err = _create_cron(spec, plugin_root)
            if ok:
                result["installed"].append({"name": spec.name, "schedule": spec.schedule, "mode": spec.mode})
            else:
                result["errors"].append({"name": spec.name, "phase": "create", "error": err})

        print(json.dumps(result))
        return 1 if result["errors"] else 0

    return {
        "start": start_handler,
        "stop": stop_handler,
        "status": status_handler,
        "tail": tail_handler,
        "install-cron": install_cron_handler,
    }


FUND_BIND = "fund-bind"
FUND_BIND_HELP = (
    "operator only: set or change a fund's vault, governor, Telegram chat id or host Telegram id "
    "(the agent's fund_setup tool may set them once, never change them)"
)
FUND_BIND_FLAGS = ("vault", "governor", "telegram-chat-id", "host-telegram-id")
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TG_RE = re.compile(r"^-?\d{1,20}$")


def fund_bind(fund_rt: Any, args: Any) -> int:
    """`hermes sherwood fund-bind <fund> [--vault 0x..] [--governor 0x..] [--telegram-chat-id -100..] [--host-telegram-id 123]`."""
    if fund_rt is None:
        print(json.dumps({"ok": False, "error": "fund features are not loaded"}))
        return 1
    from .session import current_caller

    caller = current_caller()
    if caller.messaging:
        # Run by the agent from a chat turn: the terminal child inherits HERMES_SESSION_*.
        # Defense in depth only (env can be stripped); the boundary is the toolset.
        print(json.dumps({"ok": False, "error": "fund-bind is for the operator's own shell, not a chat turn"}))
        return 3
    values = {flag.replace("-", "_"): getattr(args, flag.replace("-", "_"), None) for flag in FUND_BIND_FLAGS}
    values = {k: str(v).strip() for k, v in values.items() if v not in (None, "")}
    if not values:
        print(json.dumps({"ok": False, "error": "pass at least one of --" + ", --".join(FUND_BIND_FLAGS)}))
        return 2
    for key in ("vault", "governor"):
        if key in values and not _ADDR_RE.match(values[key]):
            print(json.dumps({"ok": False, "error": f"{key} must be a 0x address"}))
            return 2
    for key in ("telegram_chat_id", "host_telegram_id"):
        if key in values and not _TG_RE.match(values[key]):
            print(json.dumps({"ok": False, "error": f"{key} must be a numeric Telegram id"}))
            return 2

    def mutate(state: Any) -> dict:
        before = {k: getattr(state.fund, k) for k in values}
        for k, v in values.items():
            setattr(state.fund, k, v)
        return before

    try:
        before = fund_rt.service.backend.update(args.fund, mutate)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "fund": args.fund, "changed": {k: {"from": before[k], "to": v} for k, v in values.items()}}))
    return 0


def register_cli(ctx: Any, sup: Supervisor, fund_rt: Any = None) -> None:
    """Register ONE top-level `hermes sherwood` command with subcommands.

    Hermes turns every `register_cli_command` into a top-level `hermes <name>`
    parser, so registering `status` / `tail` / `install-cron` on their own
    collides with Hermes' built-in `hermes status` (argparse raises, Hermes
    swallows it, and the later commands never attach). One `sherwood` parser
    with subparsers is the documented `hermes sherwood <sub>` CLI.
    """
    handlers = build_cli_handlers(sup)
    handlers[FUND_BIND] = lambda args: fund_bind(fund_rt, args)

    def setup(parser: Any) -> None:
        subs = parser.add_subparsers(dest="sherwood_command", metavar="<command>")
        for name, (help_text, takes_fund) in SUBCOMMANDS.items():
            sub = subs.add_parser(name, help=help_text, description=help_text)
            if takes_fund:
                sub.add_argument("subdomain", help="fund subdomain")
        bind = subs.add_parser(FUND_BIND, help=FUND_BIND_HELP, description=FUND_BIND_HELP)
        bind.add_argument("fund", help="fund name (subdomain)")
        for flag in FUND_BIND_FLAGS:
            bind.add_argument(f"--{flag}", default=None)

    def dispatch(args: Any) -> int:
        handler = handlers.get(getattr(args, "sherwood_command", None) or "")
        if handler is None:
            print("usage: hermes sherwood {" + ",".join([*SUBCOMMANDS, FUND_BIND]) + "} ...", flush=True)
            return 2
        return handler(args)

    ctx.register_cli_command(
        name="sherwood",
        help="Sherwood fund monitor: start | stop | status | tail | install-cron | fund-bind",
        setup_fn=setup,
        handler_fn=dispatch,
        description="Monitor Sherwood funds and register the Sherwood crons.",
    )
