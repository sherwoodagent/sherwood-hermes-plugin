"""Tests for `hermes sherwood …` CLI subcommands, especially install-cron."""
from __future__ import annotations

import json
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from sherwood_monitor.cli import (
    CRON_NAME,
    CRON_PROMPT,
    CRON_SCHEDULE,
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


def test_install_cron_missing_hermes_binary(install_cron_handler, capsys):
    with patch("sherwood_monitor.cli.shutil.which", return_value=None):
        rc = install_cron_handler(Namespace())
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["installed"] is False
    assert "hermes binary not found" in out["error"].lower()


def test_install_cron_idempotent_when_already_registered(install_cron_handler, capsys):
    """`hermes cron list` shows our entry → no create call, exit 0."""
    list_result = MagicMock(returncode=0, stdout=f"some-other\n{CRON_NAME}  */15 * * * *\n", stderr="")

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", return_value=list_result) as mock_run:
        rc = install_cron_handler(Namespace())

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"installed": False, "name": CRON_NAME, "reason": "already_registered"}
    # only one subprocess call: list. No create.
    assert mock_run.call_count == 1
    assert mock_run.call_args.args[0][1:] == ["cron", "list"]


def test_install_cron_creates_when_absent(install_cron_handler, capsys):
    """`hermes cron list` returns no entry → create is called → exit 0."""
    list_result = MagicMock(returncode=0, stdout="other-cron */5 * * * *\n", stderr="")
    create_result = MagicMock(returncode=0, stdout="", stderr="")

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=[list_result, create_result]) as mock_run:
        rc = install_cron_handler(Namespace())

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"installed": True, "name": CRON_NAME, "schedule": CRON_SCHEDULE}

    assert mock_run.call_count == 2
    create_args = mock_run.call_args_list[1].args[0]
    assert create_args[1:5] == ["cron", "create", "--name", CRON_NAME]
    assert "--schedule" in create_args
    assert CRON_SCHEDULE in create_args
    assert "--prompt" in create_args
    assert CRON_PROMPT in create_args


def test_install_cron_propagates_create_failure(install_cron_handler, capsys):
    """Non-zero exit from `hermes cron create` surfaces stderr in JSON output."""
    list_result = MagicMock(returncode=0, stdout="", stderr="")
    create_result = MagicMock(returncode=2, stdout="", stderr="invalid schedule\n")

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=[list_result, create_result]):
        rc = install_cron_handler(Namespace())

    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["installed"] is False
    assert out["name"] == CRON_NAME
    assert out["error"] == "invalid schedule"


def test_install_cron_treats_list_failure_as_absent(install_cron_handler, capsys):
    """If `hermes cron list` errors, fall through to create (don't silently skip)."""
    list_result = MagicMock(returncode=1, stdout="", stderr="error talking to daemon\n")
    create_result = MagicMock(returncode=0, stdout="", stderr="")

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=[list_result, create_result]) as mock_run:
        rc = install_cron_handler(Namespace())

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["installed"] is True
    assert mock_run.call_count == 2
