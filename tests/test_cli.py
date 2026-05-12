"""Tests for `hermes sherwood …` CLI subcommands, especially install-cron."""
from __future__ import annotations

import json
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from sherwood_monitor.cli import (
    CRON_NAMES,
    CRONS,
    register_cli,
)


def _captured_handler(mock_ctx: MagicMock, name: str):
    """Extract the handler_fn registered for a given subcommand name."""
    for call in mock_ctx.register_cli_command.call_args_list:
        if call.kwargs.get("name") == name:
            return call.kwargs["handler_fn"]
    raise AssertionError(f"no handler registered for {name!r}")


def _captured_names(mock_ctx: MagicMock) -> list[str]:
    return [call.kwargs.get("name") for call in mock_ctx.register_cli_command.call_args_list]


@pytest.fixture
def install_cron_handler():
    """Build a mock ctx + supervisor, run register_cli, return the install-cron handler."""
    ctx = MagicMock()
    sup = MagicMock()
    register_cli(ctx, sup)
    return _captured_handler(ctx, "install-cron")


def test_register_cli_registers_all_five_commands():
    ctx = MagicMock()
    register_cli(ctx, MagicMock())
    names = _captured_names(ctx)
    assert names == ["start", "stop", "status", "tail", "install-cron"]


def test_crons_inventory_has_expected_names():
    """Lock the public cron inventory so a rename surfaces in this file."""
    assert CRON_NAMES == (
        "sherwood-monitor-digest",
        "sherwood-aum-watchdog",
        "sherwood-gas-watchdog",
        "sherwood-stream-watchdog",
        "sherwood-proposal-reasoning",
    )
    # Exactly one of them costs LLM tokens.
    agent_crons = [c for c in CRONS if c.mode == "agent"]
    assert len(agent_crons) == 1
    assert agent_crons[0].name == "sherwood-proposal-reasoning"


def test_install_cron_missing_hermes_binary(install_cron_handler, capsys):
    with patch("sherwood_monitor.cli.shutil.which", return_value=None):
        rc = install_cron_handler(Namespace())
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["installed"] is False
    assert "hermes binary not found" in out["error"].lower()


def test_install_cron_creates_all_when_absent(install_cron_handler, capsys):
    """Empty `hermes cron list` → register every cron, none skipped, exit 0."""
    list_result = MagicMock(returncode=0, stdout="", stderr="")
    create_result = MagicMock(returncode=0, stdout="", stderr="")

    # 1 list + N creates, where N = len(CRONS).
    # Each create resolves the same way (success).
    side_effects = [list_result] + [create_result] * len(CRONS)

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=side_effects) as mock_run:
        rc = install_cron_handler(Namespace())

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    installed_names = {entry["name"] for entry in out["installed"]}
    assert installed_names == set(CRON_NAMES)
    assert out["skipped"] == []
    assert out["errors"] == []
    # 1 list + 5 creates
    assert mock_run.call_count == 1 + len(CRONS)


def test_install_cron_idempotent_when_all_registered(install_cron_handler, capsys):
    """Every entry already present → no create calls, exit 0."""
    listing = "\n".join(CRON_NAMES) + "\n"
    list_result = MagicMock(returncode=0, stdout=listing, stderr="")

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", return_value=list_result) as mock_run:
        rc = install_cron_handler(Namespace())

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["installed"] == []
    skipped_names = {entry["name"] for entry in out["skipped"]}
    assert skipped_names == set(CRON_NAMES)
    # Only the list call — no creates.
    assert mock_run.call_count == 1


def test_install_cron_no_agent_command_shape(install_cron_handler, capsys):
    """no_agent crons must be invoked as `cron create --no-agent --script <abs> <sub>`."""
    list_result = MagicMock(returncode=0, stdout="", stderr="")
    create_result = MagicMock(returncode=0, stdout="", stderr="")
    side_effects = [list_result] + [create_result] * len(CRONS)

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=side_effects) as mock_run:
        install_cron_handler(Namespace())

    # Skip the list call; inspect every create.
    create_calls = mock_run.call_args_list[1:]
    no_agent_count = 0
    agent_count = 0
    for call in create_calls:
        argv = call.args[0]
        if "--no-agent" in argv:
            no_agent_count += 1
            assert "--script" in argv
            # Last positional is the subcommand (digest|aum|gas|stream)
            assert argv[-1] in {"digest", "aum", "gas", "stream"}
        elif "--prompt" in argv:
            agent_count += 1

    assert no_agent_count == 4
    assert agent_count == 1


def test_install_cron_reports_create_failure(install_cron_handler, capsys):
    """A failing create surfaces in `errors`; rc is non-zero."""
    list_result = MagicMock(returncode=0, stdout="", stderr="")
    create_ok = MagicMock(returncode=0, stdout="", stderr="")
    create_fail = MagicMock(returncode=2, stdout="", stderr="invalid schedule\n")
    # First create succeeds, second fails, rest succeed.
    side_effects = [list_result, create_ok, create_fail] + [create_ok] * (len(CRONS) - 2)

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=side_effects):
        rc = install_cron_handler(Namespace())

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert len(out["errors"]) == 1
    err = out["errors"][0]
    assert err["phase"] == "create"
    assert err["error"] == "invalid schedule"


def test_install_cron_treats_list_failure_as_absent(install_cron_handler, capsys):
    """If `hermes cron list` errors, fall through to create (don't silently skip)."""
    list_result = MagicMock(returncode=1, stdout="", stderr="error talking to daemon\n")
    create_result = MagicMock(returncode=0, stdout="", stderr="")
    side_effects = [list_result] + [create_result] * len(CRONS)

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=side_effects) as mock_run:
        rc = install_cron_handler(Namespace())

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {entry["name"] for entry in out["installed"]} == set(CRON_NAMES)
    assert mock_run.call_count == 1 + len(CRONS)


def test_install_cron_skips_reasoning_when_disabled(install_cron_handler, capsys, tmp_path, monkeypatch):
    """`proposal_reasoning_enabled: false` in config skips the agent cron, registers the four no_agent crons."""
    cfg_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("syndicates: []\nproposal_reasoning_enabled: false\n")
    monkeypatch.setattr("sherwood_monitor.cli.Path.home", lambda: tmp_path)

    list_result = MagicMock(returncode=0, stdout="", stderr="")
    create_result = MagicMock(returncode=0, stdout="", stderr="")
    # 4 creates (reasoning is skipped)
    side_effects = [list_result] + [create_result] * 4

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=side_effects) as mock_run:
        rc = install_cron_handler(Namespace())

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    installed_names = {entry["name"] for entry in out["installed"]}
    assert "sherwood-proposal-reasoning" not in installed_names
    assert installed_names == set(CRON_NAMES) - {"sherwood-proposal-reasoning"}
    skipped_names = {entry["name"] for entry in out["skipped"]}
    assert "sherwood-proposal-reasoning" in skipped_names
    # 1 list + 4 creates
    assert mock_run.call_count == 5
