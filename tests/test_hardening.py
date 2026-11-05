"""Release-review hardening: child envs, the gas RPC URL, upgrade and sidecar warnings."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sherwood_monitor.cli_watchdog as wd
from sherwood_monitor.childenv import child_env
from sherwood_monitor.config import Config
from sherwood_monitor.preflight import run_preflight

SECRETS = {
    "SHERWOOD_PRIVATE_KEY": "0x" + "11" * 32,
    "TELEGRAM_BOT_TOKEN": "tg-token",
    "SHERWOOD_TG_LINK_SECRET": "link",
    "SHERWOOD_TG_API_TOKEN": "api",
    "SHERWOOD_TG_PERSONAL_LINKS": "dm",
}


# -- N4: children never inherit the plugin's secrets ---------------------------------------
def test_child_env_strips_plugin_secrets_and_keeps_the_rest():
    env = child_env({**SECRETS, "PATH": "/usr/bin", "HOME": "/h", "PRIVATE_KEY": "cli-own", "SHERWOOD_AGENT_ADDRESS": "0xabc"})
    assert env == {"PATH": "/usr/bin", "HOME": "/h", "PRIVATE_KEY": "cli-own", "SHERWOOD_AGENT_ADDRESS": "0xabc"}


@pytest.mark.asyncio
async def test_sherwood_cli_children_get_the_stripped_env(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"{}", b""))
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    spawn = AsyncMock(return_value=proc)
    with patch("asyncio.create_subprocess_exec", spawn):
        from sherwood_monitor.cron_tick import _run_session_check
        from sherwood_monitor.hooks import _catchup_one
        from sherwood_monitor.state_fetcher import fetch_vault_info

        await _catchup_one("sherwood", "alpha")
        await fetch_vault_info("sherwood", "alpha")
        await _run_session_check("sherwood", "alpha")
    assert spawn.call_count == 3
    for call in spawn.call_args_list:
        env = call.kwargs["env"]
        assert not set(SECRETS) & set(env)
        assert "PATH" in env


def test_cli_version_check_gets_the_stripped_env(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.71.0"))
    with patch("subprocess.run", run):
        run_preflight("sherwood", sidecar_dir=None)
    assert not set(SECRETS) & set(run.call_args.kwargs["env"])


@pytest.mark.asyncio
async def test_node_sidecar_gets_the_stripped_env(monkeypatch, tmp_path):
    from sherwood_monitor.sidecar import Sidecar

    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.js").write_text("// built")
    spawn = AsyncMock(side_effect=RuntimeError("stop after spawn"))
    sc = Sidecar(sidecar_dir=tmp_path, primary_private_key_hex=SECRETS["SHERWOOD_PRIVATE_KEY"], db_path=tmp_path / "db")
    with patch("asyncio.create_subprocess_exec", spawn), pytest.raises(RuntimeError):
        await sc.start()
    assert not set(SECRETS) & set(spawn.call_args.kwargs["env"])


# -- N3: a bad gas_rpc_url never reaches a cron alert ----------------------------------------
def test_schemeless_rpc_url_is_refused_without_echo(caplog):
    marker = "RPCAPIKEYMARKER"
    with caplog.at_level("WARNING"):
        assert wd._eth_balance(f"rpc.example.invalid/v2/{marker}", "0x" + "12" * 20) is None
    assert marker not in caplog.text
    assert "must be an http(s) URL" in caplog.text


def test_rpc_failure_logs_scheme_and_host_only(caplog):
    marker = "RPCAPIKEYMARKER"
    with patch("urllib.request.urlopen", side_effect=OSError(f"boom {marker}")), caplog.at_level("WARNING"):
        assert wd._eth_balance(f"https://rpc.example.invalid/v2/{marker}", "0x" + "12" * 20) is None
    assert marker not in caplog.text
    assert "https://rpc.example.invalid/<redacted>" in caplog.text


def test_gas_main_with_a_schemeless_url_exits_zero_and_silent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wd, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(wd, "WATCHDOG_STATE_PATH", tmp_path / "watchdog_state.json")
    (tmp_path / "config.yaml").write_text("funds: []\ngas_rpc_url: rpc.example.invalid/v2/RPCAPIKEYMARKER\n")
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", "0x" + "12" * 20)
    assert wd.main(["gas"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "RPCAPIKEYMARKER" not in captured.err


# -- P4 / N2: warnings only when they apply ---------------------------------------------------
def test_sidecar_not_built_warning_only_when_xmtp_is_configured(tmp_path):
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.71.0"))
    with patch("subprocess.run", run):
        off = run_preflight("sherwood", sidecar_dir=tmp_path, xmtp_configured=False)
        on = run_preflight("sherwood", sidecar_dir=tmp_path, xmtp_configured=True)
    assert off.warnings == [] and off.sidecar_built is False
    assert any("sidecar not built" in w for w in on.warnings)


def _register_and_capture(tmp_path: Path, mock_ctx, funds: list[str]) -> str:
    import yaml

    from sherwood_monitor.event_buffer import EventBuffer

    cfg_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(yaml.dump({"funds": funds}))
    pushed: list[str] = []
    original = EventBuffer.push

    def capture(self, msg):
        pushed.append(msg)
        return original(self, msg)

    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(return_value=MagicMock(returncode=0, stdout="0.71.0"))), \
         patch("sherwood_monitor._sidecar_dir", return_value=tmp_path / "no-sidecar"), \
         patch.object(EventBuffer, "push", capture):
        from sherwood_monitor import register

        register(mock_ctx)
    return "\n".join(pushed)


def test_upgrade_warning_when_funds_are_configured_and_no_wallet_is_set(tmp_path, mock_ctx, monkeypatch):
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SHERWOOD_AGENT_ADDRESS", raising=False)
    text = _register_and_capture(tmp_path, mock_ctx, ["alpha-fund"])
    assert "gas watchdog is off" in text and "SHERWOOD_AGENT_ADDRESS" in text
    assert "sidecar not built" not in text  # XMTP is not configured, so no sidecar warning (P4)


def test_no_upgrade_warning_when_the_address_is_set(tmp_path, mock_ctx, monkeypatch):
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", "0x" + "12" * 20)
    assert "gas watchdog is off" not in _register_and_capture(tmp_path, mock_ctx, ["alpha-fund"])


def test_no_upgrade_warning_without_funds(tmp_path, mock_ctx, monkeypatch):
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("SHERWOOD_AGENT_ADDRESS", raising=False)
    assert "gas watchdog is off" not in _register_and_capture(tmp_path, mock_ctx, [])


def test_config_default_gas_rpc_is_https():
    assert Config().gas_rpc_url.startswith("https://")
