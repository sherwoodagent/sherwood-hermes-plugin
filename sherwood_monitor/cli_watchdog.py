"""no_agent watchdog scripts for sherwood-monitor.

These subcommands are invoked from Hermes' script-only cron mode
(no LLM, no agent loop — the script's stdout is delivered verbatim).
Each subcommand stays SILENT (empty stdout) unless its alert condition
is breached, then prints a single human-readable line. Exit 0 on
success (silent or alerting); non-zero only on hard errors so Hermes'
error-alert path stays meaningful.

Subcommands:
    digest             Bullet-format new interesting events from `cron_tick`.
    aum                Alert when TVL Δ since last reading exceeds threshold.
    gas                Alert when agent wallet ETH balance is below floor.
    stream             Alert when a supervised fund stream is stale.

State is persisted in `~/.hermes/plugins/sherwood-monitor/watchdog_state.json`
under per-subcommand keys. This file is separate from `cron_cursor.json` so
the digest cron's cursor (shared with the in-process tool path) stays
isolated from the alert-suppression bookkeeping.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .cron_tick import cron_tick
from .exposure import aggregate_exposure
from .fund.secrets import SHERWOOD_AGENT_ADDRESS, SHERWOOD_PRIVATE_KEY, get_secret
from .key_derivation import address_from_private_key, normalize_address, normalize_private_key

_log = logging.getLogger(__name__)

PLUGIN_ROOT = Path.home() / ".hermes" / "plugins" / "sherwood-monitor"
CONFIG_PATH = PLUGIN_ROOT / "config.yaml"
WATCHDOG_STATE_PATH = PLUGIN_ROOT / "watchdog_state.json"
# The gas watchdog checks one chain: Robinhood Chain (4663), RPC from `gas_rpc_url`.
GAS_CHAIN_LABEL = "robinhood"

# Stream watchdog reads supervisor in-process status via a one-shot
# `hermes sherwood status` call. The supervisor lives in the Hermes runtime,
# so the cron script can't introspect it directly — it shells out instead.


# ---------------------------------------------------------------------------
# Watchdog state persistence (atomic, mirrors cron_tick._save_cursors)
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(WATCHDOG_STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    WATCHDOG_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".watchdog_state.", suffix=".tmp", dir=str(WATCHDOG_STATE_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, WATCHDOG_STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Monitor wallet (for the gas watchdog). Declared secrets only: the plugin
# never reads the Sherwood CLI's config file.
# ---------------------------------------------------------------------------


def _agent_address() -> str | None:
    """``SHERWOOD_AGENT_ADDRESS``, else the address of ``SHERWOOD_PRIVATE_KEY``, else ``None``.

    Logs one warning (stderr, never stdout: stdout is the alert) when neither is
    usable. Never logs a value.
    """
    raw_address = get_secret(SHERWOOD_AGENT_ADDRESS)
    if raw_address:
        address = normalize_address(raw_address)
        if address is None:
            _log.warning("gas watchdog skipped: %s is not a 0x address", SHERWOOD_AGENT_ADDRESS)
        return address
    raw_key = get_secret(SHERWOOD_PRIVATE_KEY)
    if raw_key:
        key = normalize_private_key(raw_key)
        if key is None:
            _log.warning("gas watchdog skipped: %s is not a 32-byte hex key", SHERWOOD_PRIVATE_KEY)
            return None
        try:
            return address_from_private_key(key)
        except Exception as exc:
            _log.warning("gas watchdog skipped: address derivation failed (%s)", type(exc).__name__)
            return None
    _log.warning(
        "gas watchdog unconfigured: set %s (or %s); skipping",
        SHERWOOD_AGENT_ADDRESS,
        SHERWOOD_PRIVATE_KEY,
    )
    return None


# ---------------------------------------------------------------------------
# digest — bullet-format interesting events
# ---------------------------------------------------------------------------


def _format_event(fund: str, ev: dict) -> str:
    kind = ev.get("kind")
    if kind == "chain":
        etype = ev.get("type", "Event")
        pid = ev.get("proposalId") or ev.get("id") or ""
        block = ev.get("block", "")
        pid_str = f" proposal={pid}" if pid else ""
        return f"- {fund}: {etype}{pid_str} block={block}"
    if kind == "xmtp":
        mtype = ev.get("type", "MESSAGE")
        sender = ev.get("from", "")
        text = (ev.get("text") or ev.get("content") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        sender_str = f" from={sender[:10]}…" if sender else ""
        return f"- {fund}: {mtype}{sender_str} {text}"
    return f"- {fund}: {json.dumps(ev)}"


async def _digest(cfg: Config) -> str:
    """Run cron_tick per fund, render new events as bullets.

    Returns empty string when no events / no alerts — caller prints nothing,
    Hermes treats as silent tick. Concentration alerts are appended below
    the events block when `include_exposure` returns any.
    """
    lines: list[str] = []
    alerts: list[str] = []
    seen_alerts: set[str] = set()  # dedupe protocols across funds

    for sub in cfg.funds:
        try:
            result = await cron_tick(
                cfg.sherwood_bin,
                sub,
                include_exposure=True,
                funds_for_exposure=cfg.funds,
                concentration_threshold_pct=cfg.concentration_threshold_pct,
            )
        except Exception as exc:
            _log.warning("digest: cron_tick failed for %s: %s", sub, exc)
            continue

        for ev in result.get("events", []) or []:
            lines.append(_format_event(sub, ev))

        # `concentration_alerts` is aggregated across the same fund list
        # each time cron_tick is called — only emit each protocol once per run.
        for alert in result.get("concentration_alerts", []) or []:
            proto = alert.get("protocol", "")
            if not proto or proto in seen_alerts:
                continue
            seen_alerts.add(proto)
            pct = alert.get("pct", 0)
            exposed = ", ".join(alert.get("funds", []) or [])
            alerts.append(f"- CONCENTRATION: {proto} {pct}% across [{exposed}]")

    if not lines and not alerts:
        return ""

    out: list[str] = []
    if lines:
        out.append("Sherwood digest:")
        out.extend(lines)
    if alerts:
        if out:
            out.append("")
        out.append("Concentration alerts:")
        out.extend(alerts)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# aum — alert when TVL Δ exceeds threshold
# ---------------------------------------------------------------------------


async def _aum(cfg: Config) -> str:
    if cfg.aum_alert_threshold_pct <= 0 or not cfg.funds:
        return ""

    try:
        report = await aggregate_exposure(cfg.sherwood_bin, cfg.funds)
    except Exception as exc:
        _log.warning("aum: aggregate_exposure failed: %s", exc)
        return ""

    state = _load_state()
    aum_state = state.setdefault("aum", {})

    aum_key = "total"
    prior = aum_state.get(aum_key, {}).get("aum_usd")
    current = float(report.total_aum_usd)
    ts = int(time.time())

    alert_line = ""
    if prior is not None and prior > 0:
        delta_pct = (current - prior) / prior * 100.0
        if abs(delta_pct) >= cfg.aum_alert_threshold_pct:
            sign = "+" if delta_pct >= 0 else ""
            alert_line = (
                f"Sherwood AUM Δ {sign}{delta_pct:.2f}% "
                f"(prev ${prior:,.0f} → now ${current:,.0f})"
            )

    # Persist current reading regardless of whether we alerted, so the next
    # tick compares against the latest snapshot (sticky-alert avoidance).
    aum_state[aum_key] = {"aum_usd": current, "ts": ts}
    state["aum"] = aum_state
    _save_state(state)

    return alert_line


# ---------------------------------------------------------------------------
# gas — alert when agent wallet ETH falls below floor
# ---------------------------------------------------------------------------


def _eth_balance(rpc_url: str, address: str) -> float | None:
    """Make a single eth_getBalance JSON-RPC call. Returns wei→ether or None."""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [address, "latest"],
            "id": 1,
        }
    ).encode()
    req = urllib.request.Request(
        rpc_url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        body = json.loads(raw)
        result = body.get("result")
        if not isinstance(result, str):
            return None
        wei = int(result, 16)
        return wei / 1e18
    except Exception as exc:
        _log.warning("eth_getBalance failed (%s): %s", _redact_url(rpc_url), exc)
        return None


def _redact_url(url: str) -> str:
    """scheme://host only: provider RPC URLs carry API keys in the path or query."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "<rpc>"
    host = parts.hostname or "<rpc>"
    tail = "/<redacted>" if (parts.path not in ("", "/") or parts.query or parts.username) else ""
    return f"{parts.scheme or 'https'}://{host}{tail}"


def _gas(cfg: Config) -> str:
    """Alert when the monitor wallet's ETH on Robinhood Chain is below the floor.

    Silent when the wallet is unconfigured (one stderr warning), when
    `gas_rpc_url` is empty, or when the RPC call fails.
    """
    address = _agent_address()
    if address is None:
        return ""
    rpc_url = (cfg.gas_rpc_url or "").strip()
    if not rpc_url:
        return ""
    balance_eth = _eth_balance(rpc_url, address)
    if balance_eth is None or balance_eth >= cfg.gas_alert_min_eth:
        return ""
    return "\n".join(
        [
            f"Sherwood gas alert — agent {address}:",
            f"- {GAS_CHAIN_LABEL}: {balance_eth:.6f} ETH (below {cfg.gas_alert_min_eth} ETH floor)",
        ]
    )


# ---------------------------------------------------------------------------
# stream — alert when a fund's supervisor stream is stale
# ---------------------------------------------------------------------------


def _stream(cfg: Config) -> str:
    """Read `hermes sherwood status` (one-shot subprocess) and flag stale streams.

    The supervisor lives inside the Hermes runtime; a script can't introspect
    its in-memory state directly. Status is exposed via the host `hermes`
    binary, which we shell out to. If `hermes` is unavailable we stay silent
    (no false alarms when the host plugin isn't even running).
    """
    import shutil
    import subprocess

    hermes = shutil.which("hermes")
    if hermes is None:
        return ""

    try:
        res = subprocess.run(
            [hermes, "sherwood", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        _log.warning("stream: `hermes sherwood status` failed to invoke: %s", exc)
        return ""

    if res.returncode != 0 or not res.stdout.strip():
        # Hermes not currently running, or status path errored. Either way,
        # surface as silent — the user has bigger problems to address first.
        return ""

    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError:
        return ""

    items = payload.get("funds") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return ""

    stale_threshold_s = cfg.stream_stale_minutes * 60
    now_s = int(time.time())
    stale: list[str] = []
    dead: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        sub = item.get("subdomain", "?")
        pid = item.get("pid", 0)
        last_event_at = item.get("last_event_at")  # epoch seconds or null

        if not pid:
            dead.append(sub)
            continue
        if isinstance(last_event_at, (int, float)) and last_event_at > 0:
            age_s = now_s - int(last_event_at)
            if age_s > stale_threshold_s:
                stale.append(f"{sub} ({age_s // 60}m since last event)")

    if not stale and not dead:
        return ""

    lines = ["Sherwood stream health:"]
    for sub in dead:
        lines.append(f"- {sub}: NO SUPERVISOR (pid=0)")
    for entry in stale:
        lines.append(f"- {entry}: stale, threshold {cfg.stream_stale_minutes}m")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _emit(text: str) -> None:
    """Print to stdout if non-empty; otherwise stay silent (Hermes → silent tick)."""
    text = (text or "").strip()
    if text:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sherwood-monitor-watchdog")
    parser.add_argument(
        "subcommand",
        choices=["digest", "aum", "gas", "stream"],
        help="Which watchdog to run",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(CONFIG_PATH)
    except Exception as exc:
        _log.error("config load failed: %s", exc)
        return 2

    if not cfg.funds and args.subcommand in {"digest", "aum"}:
        # Nothing to monitor; silent tick (NOT an error).
        return 0

    try:
        if args.subcommand == "digest":
            _emit(asyncio.run(_digest(cfg)))
        elif args.subcommand == "aum":
            _emit(asyncio.run(_aum(cfg)))
        elif args.subcommand == "gas":
            _emit(_gas(cfg))
        elif args.subcommand == "stream":
            _emit(_stream(cfg))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Hard failure → non-zero exit so Hermes delivers an error alert.
        _log.exception("watchdog %s failed: %s", args.subcommand, exc)
        sys.stderr.write(f"watchdog {args.subcommand} failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
