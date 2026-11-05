"""`hermes sherwood <cmd>` CLI commands."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
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
    """The plugin's own directory (the one holding `scripts/watchdog.sh`).

    A Hermes install (git clone into the plugins folder) and an editable
    checkout both keep `scripts/` next to the `sherwood_monitor` package.
    """
    src_candidate = Path(__file__).resolve().parent.parent
    if (src_candidate / _WATCHDOG_SCRIPT).exists():
        return src_candidate
    return Path.home() / ".hermes" / "plugins" / "sherwood-monitor"


HERMES_TIMEOUT_S = 60
WRAPPER_PREFIX = "sherwood-monitor-"


def hermes_home() -> Path:
    """The active Hermes home: `$HERMES_HOME` (Hermes exports it), else `~/.hermes`."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(os.path.expanduser(raw)) if raw else Path.home() / ".hermes"


def wrapper_name(spec: _CronSpec) -> str:
    """`sherwood-monitor-<mode>.sh`, the script Hermes runs for a no_agent cron."""
    return f"{WRAPPER_PREFIX}{spec.payload}.sh"


def _python_path(plugin_root: Path) -> list[str]:
    """The plugin root plus this interpreter's site-packages (where its dependencies live).

    Hermes runs a cron's `.sh` under bash with no arguments; a bare `python3`
    there would import neither this plugin nor PyYAML / eth-keys.
    """
    entries = [str(plugin_root)]
    for entry in sys.path:
        if entry and "site-packages" in entry and entry not in entries and Path(entry).is_dir():
            entries.append(entry)
    return entries


def wrapper_text(spec: _CronSpec, plugin_root: Path, python: str, python_path: list[str]) -> str:
    """One no_agent cron's wrapper: the mode baked in, the plugin's watchdog.sh by absolute path."""
    return (
        "#!/usr/bin/env bash\n"
        f"# Written by `hermes sherwood install-cron` (sherwood-monitor) for the {spec.name} cron.\n"
        "# Hermes runs cron scripts from this folder with no arguments, so the watchdog mode is\n"
        "# fixed here. Re-run `hermes sherwood install-cron` to rewrite it (e.g. after moving Hermes).\n"
        f"export SHERWOOD_MONITOR_PYTHON={shlex.quote(python)}\n"
        f"export PYTHONPATH={shlex.quote(os.pathsep.join(python_path))}\n"
        f"exec bash {shlex.quote(str(plugin_root / _WATCHDOG_SCRIPT))} {shlex.quote(spec.payload)}\n"
    )


def write_wrappers(scripts_dir: Path, plugin_root: Path, python: str | None = None) -> dict[str, Path]:
    """Write (or rewrite) one wrapper per no_agent cron into Hermes' scripts folder."""
    python = python or sys.executable
    python_path = _python_path(plugin_root)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for spec in CRONS:
        if spec.mode != "no_agent":
            continue
        target = scripts_dir / wrapper_name(spec)
        fd, tmp = tempfile.mkstemp(prefix=".sherwood-monitor.", suffix=".tmp", dir=str(scripts_dir))
        try:
            with os.fdopen(fd, "w") as f:
                f.write(wrapper_text(spec, plugin_root, python, python_path))
            os.chmod(tmp, 0o700)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        written[spec.name] = target
    return written


def _run_hermes(args: list[str]) -> tuple[int, str, str]:
    """Run the host `hermes` binary. Returns (rc, stdout, stderr).

    rc=127 + empty stdout/stderr signals "binary not on PATH" — the caller
    surfaces a clear install hint instead of a Python traceback.
    """
    hermes = shutil.which("hermes")
    if hermes is None:
        return 127, "", ""
    try:
        res = subprocess.run([hermes, *args], capture_output=True, text=True, timeout=HERMES_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 124, "", f"hermes {' '.join(args[:2])} timed out after {HERMES_TIMEOUT_S}s"
    return res.returncode, res.stdout, res.stderr


_JOB_LINE_RE = re.compile(r"^\s*([0-9a-f]{6,})\s+\[[^\]]*\]\s*$")
_NAME_LINE_RE = re.compile(r"^\s*Name:\s*(\S+)\s*$")


def parse_cron_list(stdout: str) -> dict[str, list[str]]:
    """`hermes cron list --all` text -> {job name: [job ids]} for this plugin's crons.

    Each job prints `<id> [state]` and then `Name: <name>`. Duplicate names are
    possible (Hermes does not enforce unique names), hence a list of ids.
    """
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in stdout.splitlines():
        m = _JOB_LINE_RE.match(line)
        if m:
            current = m.group(1)
            continue
        m = _NAME_LINE_RE.match(line)
        if m and current is not None:
            if m.group(1) in CRON_NAMES:
                out.setdefault(m.group(1), []).append(current)
            current = None
    return out


def _registered_cron_ids() -> dict[str, list[str]] | None:
    """This plugin's registered crons by name, or None when the list call fails."""
    rc, stdout, _ = _run_hermes(["cron", "list", "--all"])
    if rc != 0:
        return None
    return parse_cron_list(stdout)


def create_argv(spec: _CronSpec) -> list[str]:
    """`hermes cron create <schedule> [<prompt>] --name ...`: schedule and prompt are positional."""
    argv = ["cron", "create", spec.schedule]
    if spec.mode == "agent":
        argv.append(spec.payload)
    argv += ["--name", spec.name]
    if spec.mode == "no_agent":
        argv += ["--no-agent", "--script", wrapper_name(spec)]
    return argv


def edit_argv(spec: _CronSpec, job_id: str) -> list[str]:
    """`hermes cron edit <id> ...`: bring an existing job back to this plugin's definition."""
    argv = ["cron", "edit", job_id, "--schedule", spec.schedule]
    if spec.mode == "no_agent":
        argv += ["--script", wrapper_name(spec), "--no-agent"]
    else:
        argv += ["--prompt", spec.payload]
    return argv


# `hermes sherwood <sub>`: (help, takes a fund subdomain)
SUBCOMMANDS: dict[str, tuple[str, bool]] = {
    "start": ("start monitoring a fund", True),
    "stop": ("stop monitoring a fund", True),
    "status": ("show monitor status", False),
    "tail": ("tail stderr of a monitor", True),
    "install-cron": ("register (or update) all sherwood crons with Hermes: 4 no_agent watchdogs + 1 reasoning cron; idempotent", False),
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
        """Register (or re-register) every Sherwood cron with Hermes. Idempotent.

        Writes one wrapper per no_agent watchdog into `$HERMES_HOME/scripts/`
        (Hermes runs cron scripts only from there, with no arguments), then
        creates each missing cron with `hermes cron create <schedule> ...` and
        brings existing ones back to this definition with `hermes cron edit`.
        Reports per-entry status as JSON.
        """
        if shutil.which("hermes") is None:
            print(json.dumps({"installed": False, "error": "hermes binary not found on PATH"}))
            return 1

        # Skip the reasoning cron when explicitly disabled in config — keeps
        # token-sensitive operators in control of when LLM crons fire.
        try:
            cfg = load_config(Path.home() / ".hermes" / "plugins" / "sherwood-monitor" / "config.yaml")
            reasoning_enabled = cfg.proposal_reasoning_enabled
        except Exception:
            reasoning_enabled = True

        existing = _registered_cron_ids()
        if existing is None:
            # Without the list we cannot tell create from update; creating blind
            # would duplicate jobs (Hermes allows duplicate names).
            print(json.dumps({"installed": [], "updated": [], "skipped": [], "errors": [
                {"name": "*", "phase": "list", "error": "`hermes cron list --all` failed; nothing changed"}
            ]}))
            return 1

        scripts_dir = hermes_home() / "scripts"
        result: dict[str, Any] = {
            "installed": [], "updated": [], "skipped": [], "errors": [], "scripts_dir": str(scripts_dir),
        }
        try:
            write_wrappers(scripts_dir, _plugin_root())
        except OSError as exc:
            result["errors"].append({"name": "*", "phase": "write-scripts", "error": type(exc).__name__})
            print(json.dumps(result))
            return 1

        for spec in CRONS:
            if spec.mode == "agent" and not reasoning_enabled:
                result["skipped"].append({"name": spec.name, "reason": "proposal_reasoning_enabled=false"})
                continue
            entry = {"name": spec.name, "schedule": spec.schedule, "mode": spec.mode}
            if spec.mode == "no_agent":
                entry["script"] = wrapper_name(spec)
            job_ids = existing.get(spec.name, [])
            if not job_ids:
                rc, _out, err = _run_hermes(create_argv(spec))
                if rc == 0:
                    result["installed"].append(entry)
                else:
                    result["errors"].append(
                        {"name": spec.name, "phase": "create", "error": (err or "").strip() or f"rc={rc}"}
                    )
                continue
            for job_id in job_ids:
                rc, _out, err = _run_hermes(edit_argv(spec, job_id))
                if rc == 0:
                    result["updated"].append({**entry, "id": job_id})
                else:
                    result["errors"].append(
                        {"name": spec.name, "id": job_id, "phase": "edit", "error": (err or "").strip() or f"rc={rc}"}
                    )

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
