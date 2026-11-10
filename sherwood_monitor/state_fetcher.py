"""Default state fetcher used by the pre_tool_call risk hook.

Shells out to `sherwood vault info <subdomain> --json` and returns a state
dict for risk evaluation, OR `None` if the data is unavailable.

**Fail-open contract (safety-critical):** returning `None` signals "state
unknown" to the pre_tool_call hook, which then ALLOWS the proposal and logs
a warning. Returning a dict with zeros signals "state known and is zero" —
that is treated as a real signal and will (correctly) block.

CLI 0.103.0 has no machine-readable `vault info`: its only options are `--vault`
and `-h` (no `--json`, no global `--json`), and passing `--json` makes it exit
with an error. So the fetcher first reads `sherwood vault info --help` (once per
binary) and runs `vault info ... --json` only when that help lists `--json`.
Until then it returns `None` without running the read, logging why, and the
general risk check fails open. This general check is advisory; a fund vault's
own ceilings (max capital, max drawdown) are enforced from the local fund
record by the fund guard (sherwood_monitor/fund/guard.py), with no chain read.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from .childenv import child_env

_log = logging.getLogger(__name__)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_JSON_FLAG_RE = re.compile(r"(^|\s)--json\b", re.M)
_SUPPORTS_JSON: dict[str, bool] = {}  # sherwood binary -> `vault info --help` lists --json
NO_JSON_REASON = (
    "`sherwood vault info` has no machine-readable output on the installed CLI (its help lists no --json); "
    "the general position-sizing check is skipped (advisory, fails open)"
)


async def vault_info_supports_json(sherwood_bin: str) -> bool:
    """Whether `sherwood vault info --help` lists a `--json` option (read once per binary)."""
    if sherwood_bin in _SUPPORTS_JSON:
        return _SUPPORTS_JSON[sherwood_bin]
    supported = False
    try:
        proc = await asyncio.create_subprocess_exec(
            sherwood_bin, "vault", "info", "--help",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env(),
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        supported = proc.returncode == 0 and bool(_JSON_FLAG_RE.search(stdout.decode("utf-8", "replace")))
    except Exception as exc:
        _log.warning("could not read `sherwood vault info --help`: %s", exc)
        return False  # not cached: a missing binary may be installed later
    _SUPPORTS_JSON[sherwood_bin] = supported
    return supported


async def fetch_vault_info(sherwood_bin: str, subdomain: str) -> dict | None:
    """Shell out to `sherwood vault info <subdomain> --json` (or `--vault <address>` for a
    0x vault address) and return the raw dict.

    Returns None on any failure (non-zero exit, parse error, subprocess error,
    or no `--json` on this CLI version: then nothing runs).
    """
    if not await vault_info_supports_json(sherwood_bin):
        _log.warning("vault state for %s not read: %s", subdomain, NO_JSON_REASON)
        return None
    target = ["--vault", subdomain] if _ADDRESS_RE.match(subdomain or "") else [subdomain]
    try:
        proc = await asyncio.create_subprocess_exec(
            sherwood_bin,
            "vault",
            "info",
            *target,
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            return None
        return json.loads(stdout.decode("utf-8", "replace") or "{}") or None
    except Exception as exc:
        _log.warning("fetch_vault_info failed for %s: %s", subdomain, exc)
        return None


async def default_state_fetcher(sherwood_bin: str, subdomain: str) -> dict | None:
    """Return the risk-check state dict for a fund, or None if unknown.

    Contract:
    - dict with `vault_aum_usd`, `current_exposure_usd`, `allowed_protocols`
      when `vault info --json` returned parseable data.
    - `None` when the CLI returned no data, returned malformed data, or the
      subcommand is unavailable. The caller (pre_tool_call hook) treats
      `None` as fail-open: allow the proposal and log a warning.

    Previously returned zero-defaults on failure, which the risk checks
    interpreted as "vault has zero AUM" and blocked every proposal. That was
    a fail-closed regression contradicting the documented fail-open intent.
    """
    payload = await fetch_vault_info(sherwood_bin, subdomain)
    if payload is None:
        return None

    try:
        return {
            "vault_aum_usd": float(payload.get("aumUsd", 0)),
            "current_exposure_usd": float(payload.get("currentExposureUsd", 0)),
            "allowed_protocols": list(payload.get("allowedProtocols", [])),
        }
    except (ValueError, TypeError):
        return None


def stderr_memory_writer(record: dict) -> None:
    """Day-1 memory writer: log to stderr. Hermes memory provider wiring is follow-up."""
    import sys

    sys.stderr.write(f"[sherwood-monitor memory] {json.dumps(record)}\n")
