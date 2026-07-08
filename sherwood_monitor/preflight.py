"""Preflight checks for Sherwood CLI installation and config."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sidecar import Sidecar

# 0.68.0 is the first CLI release with the `sherwood fund` command (the
# `syndicate` → `fund` product rename). The plugin's docs and skill pack
# reference `sherwood fund`, so pin the floor to the release that ships it.
MIN_CLI_VERSION = "0.68.0"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightResult:
    cli_ok: bool
    cli_version: str | None
    config_ok: bool
    sidecar_built: bool
    sidecar_ok: bool
    sidecar_address: str | None
    missing_memberships: list[str]
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


def check_cli_configured(home: Path) -> bool:
    """Return True if `~/.sherwood/config.json` exists."""
    return (home / ".sherwood" / "config.json").exists()


def check_sidecar_built(sidecar_dir: Path) -> bool:
    """Return True if xmtp_sidecar/dist/index.js exists."""
    return (sidecar_dir / "dist" / "index.js").is_file()


async def check_sidecar_health(sidecar: "Sidecar") -> tuple[bool, str | None]:
    """Ping the sidecar. Return (ok, address_or_None)."""
    try:
        await sidecar.call("ping", {}, timeout=5.0)
        return True, sidecar.address
    except Exception as exc:
        _log.warning("sidecar ping failed: %s", exc)
        return False, None


async def check_memberships(sidecar: "Sidecar", funds: list[str]) -> list[str]:
    """Return list of subdomains where the sidecar isn't a member of the XMTP group."""
    missing: list[str] = []
    for sub in funds:
        try:
            if not await sidecar.is_member(sub):
                missing.append(sub)
        except Exception as exc:
            _log.warning("membership check failed for %s: %s", sub, exc)
            missing.append(sub)  # treat errors as "unknown, surface"
    return missing


def run_preflight(
    sherwood_bin: str,
    sidecar_dir: Path | None = None,
    home: Path | None = None,
) -> PreflightResult:
    """Run all preflight checks and collect warnings.

    Sidecar health and membership checks are NOT included here — those need
    an async context and an already-running sidecar.  Call
    ``check_sidecar_health`` and ``check_memberships`` from ``register()``
    after the sidecar is spawned.
    """
    home = home or Path.home()
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

    config_ok = check_cli_configured(home)
    if not config_ok:
        warnings.append(
            "Sherwood CLI not configured. Run: sherwood config set"
        )

    # Synchronous sidecar-built check (no running sidecar needed)
    if sidecar_dir is None:
        sidecar_built = False
        warnings.append(
            "XMTP sidecar directory not configured. "
            "Set sidecar_dir to enable XMTP streams."
        )
    else:
        sidecar_built = check_sidecar_built(sidecar_dir)
        if not sidecar_built:
            warnings.append(
                f"XMTP sidecar not built. Run: cd {sidecar_dir} && npm ci && npm run build"
            )

    return PreflightResult(
        cli_ok=cli_ok,
        cli_version=cli_version,
        config_ok=config_ok,
        sidecar_built=sidecar_built,
        sidecar_ok=False,       # filled in after async health check
        sidecar_address=None,   # filled in after async health check
        missing_memberships=[],  # filled in after async membership check
        warnings=warnings,
    )
