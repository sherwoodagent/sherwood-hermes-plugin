"""`hermes sherwood <cmd>` CLI commands."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from typing import Any

from .supervisor import Supervisor

CRON_NAME = "sherwood-monitor"
CRON_SCHEDULE = "*/15 * * * *"
CRON_PROMPT = (
    "For each syndicate in ~/.hermes/plugins/sherwood-monitor/config.yaml, "
    "call sherwood_monitor_cron_tick(subdomain, include_exposure=true). "
    "Compose a concise digest of any returned events and concentration alerts. "
    "If all ticks returned empty events and no alerts, say nothing (deliver no "
    "message). Otherwise deliver the digest."
)


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


def _cron_already_registered() -> bool:
    """Return True if a cron entry named `sherwood-monitor` already exists."""
    rc, stdout, _ = _run_hermes(["cron", "list"])
    if rc != 0:
        return False
    # Output format isn't documented as JSON; grep by name. The name token is
    # specific enough that a substring check is safe even if the column layout
    # changes between Hermes versions.
    return CRON_NAME in stdout


def register_cli(ctx: Any, sup: Supervisor) -> None:
    """Register `hermes sherwood start|stop|status|tail|install-cron` commands."""

    def _setup_common(parser: Any) -> None:
        parser.add_argument("subdomain", nargs="?")

    def start_handler(args: Any) -> int:
        if not args.subdomain:
            print("subdomain required", flush=True)
            return 2
        pid = asyncio.run(sup.start(args.subdomain))
        print(json.dumps({"started": True, "pid": pid}))
        return 0

    def stop_handler(args: Any) -> int:
        if not args.subdomain:
            print("subdomain required", flush=True)
            return 2
        asyncio.run(sup.stop(args.subdomain))
        print(json.dumps({"stopped": True}))
        return 0

    def status_handler(_args: Any) -> int:
        print(json.dumps(sup.status(), indent=2))
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
        """Idempotently register the autonomous-mode cron entry with Hermes."""
        hermes = shutil.which("hermes")
        if hermes is None:
            print(
                json.dumps(
                    {
                        "installed": False,
                        "error": "hermes binary not found on PATH",
                    }
                )
            )
            return 1

        if _cron_already_registered():
            print(json.dumps({"installed": False, "name": CRON_NAME, "reason": "already_registered"}))
            return 0

        rc, _, stderr = _run_hermes(
            [
                "cron",
                "create",
                "--name",
                CRON_NAME,
                "--schedule",
                CRON_SCHEDULE,
                "--prompt",
                CRON_PROMPT,
            ]
        )
        if rc != 0:
            print(
                json.dumps(
                    {
                        "installed": False,
                        "name": CRON_NAME,
                        "error": (stderr or "").strip() or f"hermes cron create exited rc={rc}",
                    }
                )
            )
            return rc or 1

        print(json.dumps({"installed": True, "name": CRON_NAME, "schedule": CRON_SCHEDULE}))
        return 0

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
        help="register the 15-min autonomous-digest cron with Hermes (idempotent)",
        setup_fn=lambda p: None,
        handler_fn=install_cron_handler,
    )
