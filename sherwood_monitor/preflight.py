"""Preflight checks for the Sherwood CLI.

The plugin never opens or inspects the Sherwood CLI's own config file; the CLI
reports its own configuration errors when the supervisor runs it.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .childenv import child_env

# 0.71.0 is the CLI release that ships the `syndicate` → `fund` product
# rename (the `sherwood fund` command) — 0.68.0/0.69.0 were claimed by the
# parallel Robinhood-testnet work and 0.70.0 by the per-vault governor (#421).
# The plugin's docs and skill pack reference `sherwood fund`, so pin the
# floor to the release that ships it.
MIN_CLI_VERSION = "0.71.0"


@dataclass(frozen=True)
class PreflightResult:
    cli_ok: bool
    cli_version: str | None
    warnings: list[str] = field(default_factory=list)


def _parse_version(s: str) -> tuple[int, ...]:
    return tuple(int(p) for p in s.strip().split(".") if p.isdigit())


def check_cli_installed(
    bin_path: str, min_version: str = MIN_CLI_VERSION
) -> tuple[bool, str | None]:
    """Return (ok, version_string)."""
    try:
        res = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=child_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, None

    if res.returncode != 0:
        return False, None

    version = res.stdout.strip().split()[-1]
    try:
        if _parse_version(version) < _parse_version(min_version):
            return False, version
    except ValueError:
        return False, version

    return True, version


def run_preflight(
    sherwood_bin: str,
    home: Path | None = None,
) -> PreflightResult:
    """Run all preflight checks and collect warnings.

    ``home`` is accepted for call-site compatibility and unused: nothing here
    reads files under the user's home.
    """
    del home
    warnings: list[str] = []

    cli_ok, cli_version = check_cli_installed(sherwood_bin)
    if not cli_ok:
        if cli_version is None:
            warnings.append(
                "Sherwood CLI not found. Install: npm i -g @sherwoodagent/cli"
            )
        else:
            warnings.append(
                f"Sherwood CLI version {cli_version} is below minimum "
                f"{MIN_CLI_VERSION}. Upgrade: npm i -g @sherwoodagent/cli@latest"
            )

    return PreflightResult(
        cli_ok=cli_ok,
        cli_version=cli_version,
        warnings=warnings,
    )
