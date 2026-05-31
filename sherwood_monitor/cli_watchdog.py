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
    stream             Alert when a supervised syndicate stream is stale.

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

_log = logging.getLogger(__name__)

PLUGIN_ROOT = Path.home() / ".hermes" / "plugins" / "sherwood-monitor"
CONFIG_PATH = PLUGIN_ROOT / "config.yaml"
WATCHDOG_STATE_PATH = PLUGIN_ROOT / "watchdog_state.json"
SHERWOOD_CONFIG_PATH = Path.home() / ".sherwood" / "config.json"

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
# Sherwood config reader (for gas watchdog — RPC URLs + agent privkey)
# ---------------------------------------------------------------------------


def _read_sherwood_config() -> dict[str, Any]:
    if not SHERWOOD_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(SHERWOOD_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("sherwood config unreadable: %s", exc)
        return {}


def _agent_address_from_privkey(priv_hex: str) -> str | None:
    """Derive 0x-prefixed checksum-less address from a hex private key."""
    try:
        from eth_keys import keys
        from eth_utils import to_checksum_address

        key_hex = priv_hex[2:] if priv_hex.startswith("0x") else priv_hex
        pk = keys.PrivateKey(bytes.fromhex(key_hex))
        return to_checksum_address(pk.public_key.to_address())
    except Exception as exc:
        _log.warning("agent address derivation failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# digest — bullet-format interesting events
# ---------------------------------------------------------------------------


def _format_event(syndicate: str, ev: dict) -> str:
    kind = ev.get("kind")
    if kind == "chain":
        etype = ev.get("type", "Event")
        pid = ev.get("proposalId") or ev.get("id") or ""
        block = ev.get("block", "")
        pid_str = f" proposal={pid}" if pid else ""
        return f"- {syndicate}: {etype}{pid_str} block={block}"
    if kind == "xmtp":
        mtype = ev.get("type", "MESSAGE")
        sender = ev.get("from", "")
        text = (ev.get("text") or ev.get("content") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        sender_str = f" from={sender[:10]}…" if sender else ""
        return f"- {syndicate}: {mtype}{sender_str} {text}"
    return f"- {syndicate}: {json.dumps(ev)}"


async def _digest(cfg: Config) -> str:
    """Run cron_tick per syndicate, render new events as bullets.

    Returns empty string when no events / no alerts — caller prints nothing,
    Hermes treats as silent tick. Concentration alerts are appended below
    the events block when `include_exposure` returns any.
    """
    lines: list[str] = []
    alerts: list[str] = []
    seen_alerts: set[str] = set()  # dedupe protocols across syndicates

    for sub in cfg.syndicates:
        try:
            result = await cron_tick(
                cfg.sherwood_bin,
                sub,
                include_exposure=True,
                syndicates_for_exposure=cfg.syndicates,
                concentration_threshold_pct=cfg.concentration_threshold_pct,
            )
        except Exception as exc:
            _log.warning("digest: cron_tick failed for %s: %s", sub, exc)
            continue

        for ev in result.get("events", []) or []:
            lines.append(_format_event(sub, ev))

        # `concentration_alerts` is aggregated across the same syndicate list
        # each time cron_tick is called — only emit each protocol once per run.
        for alert in result.get("concentration_alerts", []) or []:
            proto = alert.get("protocol", "")
            if not proto or proto in seen_alerts:
                continue
            seen_alerts.add(proto)
            pct = alert.get("pct", 0)
            exposed = ", ".join(alert.get("syndicates", []) or [])
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
    if cfg.aum_alert_threshold_pct <= 0 or not cfg.syndicates:
        return ""

    try:
        report = await aggregate_exposure(cfg.sherwood_bin, cfg.syndicates)
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
        _log.warning("eth_getBalance failed (%s): %s", rpc_url, exc)
        return None


def _gas(cfg: Config) -> str:
    sherwood = _read_sherwood_config()
    priv = sherwood.get("privateKey") or sherwood.get("private_key")
    rpc_map = sherwood.get("rpc") or {}
    if not priv or not isinstance(rpc_map, dict) or not rpc_map:
        # No agent wallet or no RPCs configured — nothing to check. Silent.
        return ""

    address = _agent_address_from_privkey(str(priv))
    if address is None:
        return ""

    low: list[tuple[str, float]] = []
    for chain_name, rpc_url in rpc_map.items():
        if not isinstance(rpc_url, str) or not rpc_url:
            continue
        balance_eth = _eth_balance(rpc_url, address)
        if balance_eth is None:
            continue
        if balance_eth < cfg.gas_alert_min_eth:
            low.append((str(chain_name), balance_eth))

    if not low:
        return ""

    lines = [f"Sherwood gas alert — agent {address}:"]
    for chain_name, bal in low:
        lines.append(
            f"- {chain_name}: {bal:.6f} ETH (below {cfg.gas_alert_min_eth} ETH floor)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# stream — alert when a syndicate's supervisor stream is stale
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

    items = payload.get("syndicates") if isinstance(payload, dict) else None
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

    if not cfg.syndicates and args.subcommand in {"digest", "aum"}:
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
