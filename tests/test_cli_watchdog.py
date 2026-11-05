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
    # The monitor wallet comes from env only; clear it so tests never see a real one.
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SHERWOOD_AGENT_ADDRESS", raising=False)


def _cfg(**overrides) -> Config:
    base = dict(funds=["alpha-fund"])
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
async def test_digest_dedupes_concentration_alerts_across_funds(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    # Both funds' cron_tick calls return the same protocol alert — emit once.
    fake_tick = AsyncMock(
        return_value={
            "events": [],
            "concentration_alerts": [
                {"protocol": "aerodrome", "pct": 42.0, "funds": ["alpha", "beta"]},
            ],
        }
    )
    cfg = _cfg(funds=["alpha", "beta"])
    with patch("sherwood_monitor.cli_watchdog.cron_tick", fake_tick):
        out = await wd._digest(cfg)
    assert out.count("CONCENTRATION: aerodrome") == 1
    assert "Concentration alerts:" in out


@pytest.mark.asyncio
async def test_digest_swallows_per_fund_errors(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    # cron_tick raises for one fund; the other still produces output.
    async def _tick(_bin, sub, **_kw):
        if sub == "alpha":
            raise RuntimeError("boom")
        return {"events": [{"kind": "chain", "type": "ProposalSettled", "proposalId": "9", "block": 200}]}

    cfg = _cfg(funds=["alpha", "beta"])
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


TEST_KEY = "0x" + "11" * 32  # test-only key, not a real wallet
ADDR = "0x1234567890123456789012345678901234567890"


def test_gas_silent_and_warns_when_unconfigured(monkeypatch, tmp_path, caplog):
    _redirect_paths(monkeypatch, tmp_path)
    with patch("sherwood_monitor.cli_watchdog._eth_balance") as bal, caplog.at_level("WARNING"):
        out = wd._gas(_cfg())
    assert out == ""
    bal.assert_not_called()
    assert "gas watchdog unconfigured" in caplog.text


def test_gas_never_reads_the_sherwood_cli_config(monkeypatch, tmp_path):
    """A key and RPCs in ~/.sherwood/config.json are ignored (catalog rule 11)."""
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / ".sherwood").mkdir()
    (tmp_path / ".sherwood" / "config.json").write_text(
        json.dumps({"privateKey": TEST_KEY, "rpc": {"robinhood": "https://example.invalid"}})
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    with patch("sherwood_monitor.cli_watchdog._eth_balance", return_value=0.0) as bal:
        out = wd._gas(_cfg())
    assert out == ""
    bal.assert_not_called()
    assert not hasattr(wd, "SHERWOOD_CONFIG_PATH")


def test_gas_alerts_when_balance_below_floor(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", ADDR)
    with patch("sherwood_monitor.cli_watchdog._eth_balance", return_value=0.001) as bal:
        out = wd._gas(_cfg(gas_alert_min_eth=0.002, gas_rpc_url="https://rpc.example.invalid/key"))
    bal.assert_called_once_with("https://rpc.example.invalid/key", ADDR)
    assert "Sherwood gas alert" in out
    assert "robinhood: 0.001000 ETH" in out
    assert ADDR in out


def test_gas_defaults_to_the_public_robinhood_rpc(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", ADDR)
    with patch("sherwood_monitor.cli_watchdog._eth_balance", return_value=1.0) as bal:
        wd._gas(_cfg())
    bal.assert_called_once_with("https://rpc.mainnet.chain.robinhood.com", ADDR)


def test_gas_derives_the_address_from_the_key(monkeypatch, tmp_path):
    from sherwood_monitor.key_derivation import address_from_private_key

    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", TEST_KEY)
    with patch("sherwood_monitor.cli_watchdog._eth_balance", return_value=0.001) as bal:
        out = wd._gas(_cfg(gas_alert_min_eth=0.002))
    expected = address_from_private_key(TEST_KEY)
    assert bal.call_args.args[1] == expected
    assert expected in out
    assert TEST_KEY not in out


def test_gas_address_wins_over_the_key(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", ADDR)
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", TEST_KEY)
    with patch("sherwood_monitor.cli_watchdog._eth_balance", return_value=1.0) as bal:
        wd._gas(_cfg())
    assert bal.call_args.args[1] == ADDR


def test_gas_skips_a_malformed_address_without_echoing_it(monkeypatch, tmp_path, caplog):
    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", "0xnope-secretish")
    with patch("sherwood_monitor.cli_watchdog._eth_balance") as bal, caplog.at_level("WARNING"):
        out = wd._gas(_cfg())
    assert out == ""
    bal.assert_not_called()
    assert "not a 0x address" in caplog.text
    assert "secretish" not in caplog.text


def test_gas_skips_a_malformed_key_without_echoing_it(monkeypatch, tmp_path, caplog):
    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_PRIVATE_KEY", "0xdeadbeef")
    with patch("sherwood_monitor.cli_watchdog._eth_balance") as bal, caplog.at_level("WARNING"):
        out = wd._gas(_cfg())
    assert out == ""
    bal.assert_not_called()
    assert "deadbeef" not in caplog.text


def test_gas_silent_when_balance_above_floor(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", ADDR)
    with patch("sherwood_monitor.cli_watchdog._eth_balance", return_value=0.5):
        out = wd._gas(_cfg(gas_alert_min_eth=0.002))
    assert out == ""


def test_gas_silent_when_rpc_unreadable(monkeypatch, tmp_path):
    """An RPC that returns None (network down, malformed) is not a false alarm."""
    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", ADDR)
    with patch("sherwood_monitor.cli_watchdog._eth_balance", return_value=None):
        out = wd._gas(_cfg(gas_alert_min_eth=0.002))
    assert out == ""


def test_gas_silent_when_rpc_url_blank(monkeypatch, tmp_path):
    _redirect_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", ADDR)
    with patch("sherwood_monitor.cli_watchdog._eth_balance") as bal:
        out = wd._gas(_cfg(gas_rpc_url=""))
    assert out == ""
    bal.assert_not_called()


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
        "funds": [
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
        "funds": [{"subdomain": "gamma", "pid": 4242, "last_event_at": stale_ts}]
    }
    with patch("shutil.which", return_value="/usr/local/bin/hermes"), patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=json.dumps(status_payload), stderr=""),
    ):
        out = wd._stream(_cfg(stream_stale_minutes=30))
    assert "gamma" in out and "stale" in out


def test_stream_silent_when_status_returns_empty(monkeypatch, tmp_path):
    """No configured funds → empty funds list → silent."""
    _redirect_paths(monkeypatch, tmp_path)
    with patch("shutil.which", return_value="/usr/local/bin/hermes"), patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=json.dumps({"funds": []}), stderr=""),
    ):
        out = wd._stream(_cfg())
    assert out == ""


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_digest_silent_when_no_funds(monkeypatch, tmp_path, capsys):
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
    (tmp_path / "config.yaml").write_text("funds: [foo]\n")
    rc = wd.main(["gas"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "TESTING_ALERT_LINE\n"


def test_main_returns_nonzero_on_hard_failure(monkeypatch, tmp_path, capsys):
    _redirect_paths(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("funds: [foo]\n")
    monkeypatch.setattr(wd, "_gas", lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    rc = wd.main(["gas"])
    assert rc == 1
    assert "boom" in capsys.readouterr().err
