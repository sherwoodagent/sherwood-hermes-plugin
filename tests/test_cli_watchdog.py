"""Tests for sherwood_monitor.cli_watchdog (no_agent script-only cron path)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sherwood_monitor.cli_watchdog as wd
from sherwood_monitor.config import Config
from sherwood_monitor.exposure import ConcentrationAlert, ExposureReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redirect_paths(monkeypatch, tmp_path):
    """Point every persisted path at tmp_path so tests never touch real state."""
    monkeypatch.setattr(wd, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(wd, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(wd, "WATCHDOG_STATE_PATH", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(wd, "SHERWOOD_CONFIG_PATH", tmp_path / "sherwood_config.json")


def _cfg(**overrides) -> Config:
    base = dict(syndicates=["alpha-fund"])
    base.update(overrides)
    return Config(**base)


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_silent_when_no_events(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    fake_tick = AsyncMock(return_value={"subdomain": "alpha-fund", "events": []})
    with patch("sherwood_monitor.cli_watchdog.cron_tick", fake_tick):
        out = await wd._digest(_cfg())
    assert out == ""


@pytest.mark.asyncio
async def test_digest_formats_chain_and_xmtp_events(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    fake_tick = AsyncMock(
        return_value={
            "subdomain": "alpha-fund",
            "events": [
                {"kind": "chain", "type": "ProposalCreated", "proposalId": "42", "block": 100},
                {"kind": "xmtp", "type": "RISK_ALERT", "from": "0xabcdef0123", "text": "high concentration"},
            ],
        }
    )
    with patch("sherwood_monitor.cli_watchdog.cron_tick", fake_tick):
        out = await wd._digest(_cfg())
    assert "Sherwood digest:" in out
    assert "alpha-fund: ProposalCreated proposal=42 block=100" in out
    assert "alpha-fund: RISK_ALERT" in out
    assert "high concentration" in out


@pytest.mark.asyncio
async def test_digest_dedupes_concentration_alerts_across_syndicates(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    # Both syndicates' cron_tick calls return the same protocol alert — emit once.
    fake_tick = AsyncMock(
        return_value={
            "events": [],
            "concentration_alerts": [
                {"protocol": "aerodrome", "pct": 42.0, "syndicates": ["alpha", "beta"]},
            ],
        }
    )
    cfg = _cfg(syndicates=["alpha", "beta"])
    with patch("sherwood_monitor.cli_watchdog.cron_tick", fake_tick):
        out = await wd._digest(cfg)
    assert out.count("CONCENTRATION: aerodrome") == 1
    assert "Concentration alerts:" in out


@pytest.mark.asyncio
async def test_digest_swallows_per_syndicate_errors(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    # cron_tick raises for one syndicate; the other still produces output.
    async def _tick(_bin, sub, **_kw):
        if sub == "alpha":
            raise RuntimeError("boom")
        return {"events": [{"kind": "chain", "type": "ProposalSettled", "proposalId": "9", "block": 200}]}

    cfg = _cfg(syndicates=["alpha", "beta"])
    with patch("sherwood_monitor.cli_watchdog.cron_tick", side_effect=_tick):
        out = await wd._digest(cfg)
    # Alpha's error did not abort; beta's event survived.
    assert "beta: ProposalSettled" in out
    assert "alpha" not in out  # no bullet for the failing one


# ---------------------------------------------------------------------------
# aum
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aum_silent_on_first_reading(monkeypatch, tmp_path):
    """First tick records the baseline; should NOT alert (no prior value)."""
    _redirect_paths(monkeypatch, tmp_path)
    fake_report = ExposureReport(
        total_aum_usd=100000.0, by_protocol={}, concentration_pct={}
    )
    with patch(
        "sherwood_monitor.cli_watchdog.aggregate_exposure",
        AsyncMock(return_value=fake_report),
    ):
        out = await wd._aum(_cfg(aum_alert_threshold_pct=5.0))
    assert out == ""
    # State persisted for next tick.
    state = json.loads((tmp_path / "watchdog_state.json").read_text())
    assert state["aum"]["total"]["aum_usd"] == 100000.0


@pytest.mark.asyncio
async def test_aum_alerts_when_delta_exceeds_threshold(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    # Seed prior reading at $100k.
    (tmp_path / "watchdog_state.json").write_text(
        json.dumps({"aum": {"total": {"aum_usd": 100000.0, "ts": 1}}})
    )
    fake_report = ExposureReport(
        total_aum_usd=110000.0, by_protocol={}, concentration_pct={}
    )
    with patch(
        "sherwood_monitor.cli_watchdog.aggregate_exposure",
        AsyncMock(return_value=fake_report),
    ):
        out = await wd._aum(_cfg(aum_alert_threshold_pct=5.0))
    assert "+10.00%" in out
    assert "$100,000" in out
    assert "$110,000" in out


@pytest.mark.asyncio
async def test_aum_silent_when_delta_below_threshold(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / "watchdog_state.json").write_text(
        json.dumps({"aum": {"total": {"aum_usd": 100000.0, "ts": 1}}})
    )
    fake_report = ExposureReport(
        total_aum_usd=102000.0, by_protocol={}, concentration_pct={}
    )
    with patch(
        "sherwood_monitor.cli_watchdog.aggregate_exposure",
        AsyncMock(return_value=fake_report),
    ):
        out = await wd._aum(_cfg(aum_alert_threshold_pct=5.0))
    assert out == ""
    # State STILL advanced — sticky-alert avoidance.
    state = json.loads((tmp_path / "watchdog_state.json").read_text())
    assert state["aum"]["total"]["aum_usd"] == 102000.0


@pytest.mark.asyncio
async def test_aum_silent_on_aggregate_failure(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    with patch(
        "sherwood_monitor.cli_watchdog.aggregate_exposure",
        AsyncMock(side_effect=RuntimeError("rpc down")),
    ):
        out = await wd._aum(_cfg())
    assert out == ""


# ---------------------------------------------------------------------------
# gas
# ---------------------------------------------------------------------------


def test_gas_silent_when_no_sherwood_config(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    # Don't create sherwood_config.json — _read_sherwood_config returns {}
    out = wd._gas(_cfg())
    assert out == ""


def test_gas_alerts_when_balance_below_floor(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / "sherwood_config.json").write_text(
        json.dumps(
            {
                # 32-byte test private key (NOT a real key — keccak("test-only"))
                "privateKey": "0x" + "11" * 32,
                "rpc": {"base": "https://example.invalid/rpc"},
            }
        )
    )
    # 0.001 ETH = 10^15 wei = 0x38d7ea4c68000
    with patch(
        "sherwood_monitor.cli_watchdog._eth_balance", return_value=0.001
    ), patch(
        "sherwood_monitor.cli_watchdog._agent_address_from_privkey",
        return_value="0x1234567890123456789012345678901234567890",
    ):
        out = wd._gas(_cfg(gas_alert_min_eth=0.002))
    assert "Sherwood gas alert" in out
    assert "base: 0.001000 ETH" in out
    assert "0x1234567890123456789012345678901234567890" in out


def test_gas_silent_when_balance_above_floor(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / "sherwood_config.json").write_text(
        json.dumps({"privateKey": "0x" + "11" * 32, "rpc": {"base": "https://example.invalid"}})
    )
    with patch("sherwood_monitor.cli_watchdog._eth_balance", return_value=0.5), patch(
        "sherwood_monitor.cli_watchdog._agent_address_from_privkey",
        return_value="0xabc",
    ):
        out = wd._gas(_cfg(gas_alert_min_eth=0.002))
    assert out == ""


def test_gas_skips_unreadable_rpc(monkeypatch, tmp_path):
    """An RPC that returns None (network down, malformed) must not block other chains."""
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / "sherwood_config.json").write_text(
        json.dumps(
            {
                "privateKey": "0x" + "11" * 32,
                "rpc": {"base": "https://up.invalid", "hyperevm": "https://down.invalid"},
            }
        )
    )
    # base returns low balance, hyperevm returns None (failed call).
    def _bal(rpc, _addr):
        return 0.001 if "up.invalid" in rpc else None

    with patch("sherwood_monitor.cli_watchdog._eth_balance", side_effect=_bal), patch(
        "sherwood_monitor.cli_watchdog._agent_address_from_privkey",
        return_value="0xabc",
    ):
        out = wd._gas(_cfg(gas_alert_min_eth=0.002))
    # Alert on base; hyperevm silently dropped (not False-positive flagged).
    assert "base: 0.001000 ETH" in out
    assert "hyperevm" not in out


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------


def test_stream_silent_when_hermes_missing(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    with patch("shutil.which", return_value=None):
        out = wd._stream(_cfg())
    assert out == ""


def test_stream_flags_dead_supervisor(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    status_payload = {
        "syndicates": [
            {"subdomain": "alpha", "pid": 0, "last_event_at": None},
            {"subdomain": "beta", "pid": 1234, "last_event_at": int(__import__("time").time())},
        ]
    }
    with patch("shutil.which", return_value="/usr/local/bin/hermes"), patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=json.dumps(status_payload), stderr=""),
    ):
        out = wd._stream(_cfg(stream_stale_minutes=30))
    assert "alpha: NO SUPERVISOR (pid=0)" in out
    assert "beta" not in out  # fresh event, healthy


def test_stream_flags_stale_event(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    import time as _time

    stale_ts = int(_time.time()) - 3600  # 60 min ago
    status_payload = {
        "syndicates": [{"subdomain": "gamma", "pid": 4242, "last_event_at": stale_ts}]
    }
    with patch("shutil.which", return_value="/usr/local/bin/hermes"), patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=json.dumps(status_payload), stderr=""),
    ):
        out = wd._stream(_cfg(stream_stale_minutes=30))
    assert "gamma" in out and "stale" in out


def test_stream_silent_when_status_returns_empty(monkeypatch, tmp_path):
    """No configured syndicates → empty syndicates list → silent."""
    _redirect_paths(monkeypatch, tmp_path)
    with patch("shutil.which", return_value="/usr/local/bin/hermes"), patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=json.dumps({"syndicates": []}), stderr=""),
    ):
        out = wd._stream(_cfg())
    assert out == ""


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_digest_silent_when_no_syndicates(monkeypatch, tmp_path, capsys):
    _redirect_paths(monkeypatch, tmp_path)
    rc = wd.main(["digest"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_emits_alert_with_trailing_newline(monkeypatch, tmp_path, capsys):
    """Alert text must terminate with \\n so Hermes' line-buffered reader picks it up."""
    _redirect_paths(monkeypatch, tmp_path)
    # Stub _gas to return a non-empty alert; verify main wraps it correctly.
    monkeypatch.setattr(wd, "_gas", lambda cfg: "TESTING_ALERT_LINE")
    # Avoid load_config touching tmp_path (write a minimal config so it exists).
    (tmp_path / "config.yaml").write_text("syndicates: [foo]\n")
    rc = wd.main(["gas"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "TESTING_ALERT_LINE\n"


def test_main_returns_nonzero_on_hard_failure(monkeypatch, tmp_path, capsys):
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("syndicates: [foo]\n")
    monkeypatch.setattr(wd, "_gas", lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = wd.main(["gas"])
    assert rc == 1
    assert "boom" in capsys.readouterr().err
