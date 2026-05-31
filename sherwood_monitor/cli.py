"""`hermes sherwood <cmd>` CLI commands."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_config
from .supervisor import Supervisor

# The five crons registered by `hermes sherwood install-cron`:
#   - four no_agent scripts (cheap, deterministic alerts)
#   - one agent cron (reasoning over open proposals; only this one costs tokens)
#
# Tests assert against these constants — change a name/schedule here, expect
# `tests/test_cli.py` to need an update.
PROPOSAL_REASONING_PROMPT = (
    "Open proposals review. For each syndicate in "
    "~/.hermes/plugins/sherwood-monitor/config.yaml, list any proposals that "
    "are currently in Pending or GuardianReview state. For each: summarize "
    "what it does, which protocol/contract it touches, the size relative to "
    "vault AUM, and your vote recommendation (FOR / AGAINST / ABSTAIN) with a "
    "one-sentence rationale grounded in the syndicate's prior positions and "
    "the proposed strategy's risk profile. If there are NO open proposals "
    "across all syndicates, reply with the single word HEARTBEAT_OK so the "
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


def register_cli(ctx: Any, sup: Supervisor) -> None:
    """Register `hermes sherwood start|stop|status|tail|install-cron` commands."""

    def _setup_common(parser: Any) -> None:
        parser.add_argument("subdomain", help="syndicate subdomain")

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

    ctx.register_cli_command(
        name="start", help="start monitoring a syndicate", setup_fn=_setup_common, handler_fn=start_handler
    )
    ctx.register_cli_command(
        name="stop", help="stop monitoring a syndicate", setup_fn=_setup_common, handler_fn=stop_handler
    )
    ctx.register_cli_command(
        name="status", help="show monitor status", setup_fn=lambda p: None, handler_fn=status_handler
    )
    ctx.register_cli_command(
        name="tail", help="tail stderr of a monitor", setup_fn=_setup_common, handler_fn=tail_handler
    )
    ctx.register_cli_command(
        name="install-cron",
        help="register all sherwood crons with Hermes (no_agent watchdogs + reasoning; idempotent)",
        setup_fn=lambda p: None,
        handler_fn=install_cron_handler,
    )
