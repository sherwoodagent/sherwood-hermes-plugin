"""Tests for `hermes sherwood …` CLI subcommands, especially install-cron."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sherwood_monitor.cli import (
    CRON_NAMES,
    CRONS,
    PROPOSAL_REASONING_PROMPT,
    SUBCOMMANDS,
    create_argv,
    edit_argv,
    parse_cron_list,
    register_cli,
    wrapper_name,
    write_wrappers,
)


def _parser_for(mock_ctx: MagicMock) -> tuple[argparse.ArgumentParser, Any]:
    """Build the `hermes sherwood` parser the way Hermes does (setup_fn + set_defaults)."""
    (call,) = mock_ctx.register_cli_command.call_args_list
    top = argparse.ArgumentParser(prog="hermes")
    subs = top.add_subparsers(dest="command")
    plugin_parser = subs.add_parser(call.kwargs["name"])
    call.kwargs["setup_fn"](plugin_parser)
    plugin_parser.set_defaults(func=call.kwargs["handler_fn"])
    return top, call.kwargs["handler_fn"]


@pytest.fixture
def install_cron_handler():
    """Build a mock ctx + supervisor, run register_cli, return the install-cron handler."""
    ctx = MagicMock()
    sup = MagicMock()
    register_cli(ctx, sup)
    top, dispatch = _parser_for(ctx)
    return lambda ns=None: dispatch(top.parse_args(["sherwood", "install-cron"]))


def test_register_cli_registers_one_sherwood_command_with_five_subcommands():
    """One top-level `hermes sherwood` (a bare `status` would collide with Hermes' own)."""
    ctx = MagicMock()
    register_cli(ctx, MagicMock())
    assert [c.kwargs["name"] for c in ctx.register_cli_command.call_args_list] == ["sherwood"]
    assert list(SUBCOMMANDS) == ["start", "stop", "status", "tail", "install-cron"]
    top, _ = _parser_for(ctx)
    args = top.parse_args(["sherwood", "tail", "alpha-fund"])
    assert args.sherwood_command == "tail" and args.subdomain == "alpha-fund"


def test_sherwood_dispatch_routes_subcommands(capsys):
    ctx = MagicMock()
    sup = MagicMock()
    sup.status.return_value = {"alpha": "running"}
    register_cli(ctx, sup)
    top, _ = _parser_for(ctx)
    args = top.parse_args(["sherwood", "status"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out) == {"alpha": "running"}
    bare = top.parse_args(["sherwood"])
    assert bare.func(bare) == 2


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


# ---------------------------------------------------------------------------
# install-cron
#
# Hermes (0.22.x) takes `cron create <schedule> [<prompt>]` positionally, runs
# cron scripts only from $HERMES_HOME/scripts/, and passes them no arguments.
# The last test below runs the real `hermes` binary in a scratch HERMES_HOME.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scratch_hermes_home(tmp_path, monkeypatch):
    """Every install-cron test writes into a scratch HERMES_HOME and HOME, never ~/.hermes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr("sherwood_monitor.cli.Path.home", lambda: tmp_path / "home")
    return tmp_path / "hermes-home"


LISTING = """
  1f55246e5eaf [active]
    Name:      sherwood-monitor-digest
    Schedule:  */15 * * * *

  0f1067d27ef6 [paused]
    Name:      sherwood-gas-watchdog
    Script:    old-watchdog.sh

  aaaaaaaaaaaa [active]
    Name:      someone-elses-job

  90d2adf38572 [active]
    Name:      sherwood-gas-watchdog
"""


def test_parse_cron_list_maps_our_names_to_ids():
    assert parse_cron_list(LISTING) == {
        "sherwood-monitor-digest": ["1f55246e5eaf"],
        "sherwood-gas-watchdog": ["0f1067d27ef6", "90d2adf38572"],
    }


def test_create_argv_is_positional():
    by_name = {c.name: c for c in CRONS}
    gas = create_argv(by_name["sherwood-gas-watchdog"])
    assert gas[:3] == ["cron", "create", "*/30 * * * *"]
    assert "--schedule" not in gas and "--prompt" not in gas
    assert gas[gas.index("--script") + 1] == "sherwood-monitor-gas.py"  # relative to $HERMES_HOME/scripts
    assert "--no-agent" in gas and gas[-1] == "sherwood-monitor-gas.py"  # no trailing mode argument
    reasoning = create_argv(by_name["sherwood-proposal-reasoning"])
    assert reasoning[:4] == ["cron", "create", "0 */6 * * *", PROPOSAL_REASONING_PROMPT]
    assert "--no-agent" not in reasoning


def test_edit_argv_restores_the_definition():
    by_name = {c.name: c for c in CRONS}
    argv = edit_argv(by_name["sherwood-stream-watchdog"], "534f2b79fff9")
    assert argv[:5] == ["cron", "edit", "534f2b79fff9", "--schedule", "*/5 * * * *"]
    assert argv[argv.index("--script") + 1] == "sherwood-monitor-stream.py" and "--no-agent" in argv
    argv = edit_argv(by_name["sherwood-proposal-reasoning"], "90d2adf38572")
    assert argv[argv.index("--prompt") + 1] == PROPOSAL_REASONING_PROMPT


def test_wrappers_are_python_with_the_mode_fixed(_scratch_hermes_home):
    """`.py` wrappers: Hermes runs them on its current dependency environment, so no
    interpreter or site-packages path is baked in (they survive Hermes updates)."""
    plugin_root = Path(__file__).resolve().parent.parent
    scripts = _scratch_hermes_home / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "sherwood-monitor-gas.sh").write_text("# a pre-release wrapper\n")
    written = write_wrappers(scripts, plugin_root)
    assert set(written) == {c.name for c in CRONS if c.mode == "no_agent"}
    gas = (scripts / "sherwood-monitor-gas.py").read_text()
    assert "from sherwood_monitor.cli_watchdog import main" in gas
    assert "raise SystemExit(main(['gas']))" in gas
    assert f"sys.path.append({str(plugin_root)!r})" in gas
    assert "site-packages" not in gas and "PYTHONPATH" not in gas
    compile(gas, "sherwood-monitor-gas.py", "exec")
    assert not (scripts / "sherwood-monitor-gas.sh").exists()  # the old .sh wrapper is removed


def test_install_cron_missing_hermes_binary(install_cron_handler, capsys):
    with patch("sherwood_monitor.cli.shutil.which", return_value=None):
        rc = install_cron_handler(Namespace())
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["installed"] is False
    assert "hermes binary not found" in out["error"].lower()


def test_install_cron_list_failure_changes_nothing(install_cron_handler, capsys, _scratch_hermes_home):
    """Without the list we cannot tell create from update: abort rather than duplicate jobs."""
    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run",
               return_value=MagicMock(returncode=1, stdout="", stderr="boom")) as run:
        rc = install_cron_handler(Namespace())
    assert rc == 1
    assert run.call_count == 1
    assert json.loads(capsys.readouterr().out)["errors"][0]["phase"] == "list"
    assert not (_scratch_hermes_home / "scripts").exists()


def test_install_cron_creates_missing_and_edits_existing(install_cron_handler, capsys, _scratch_hermes_home):
    ok = MagicMock(returncode=0, stdout="", stderr="")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv[1:])
        return MagicMock(returncode=0, stdout=LISTING, stderr="") if argv[1:3] == ["cron", "list"] else ok

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=fake_run):
        rc = install_cron_handler(Namespace())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {e["name"] for e in out["installed"]} == {
        "sherwood-aum-watchdog", "sherwood-stream-watchdog", "sherwood-proposal-reasoning"}
    assert sorted((e["name"], e["id"]) for e in out["updated"]) == [
        ("sherwood-gas-watchdog", "0f1067d27ef6"),
        ("sherwood-gas-watchdog", "90d2adf38572"),
        ("sherwood-monitor-digest", "1f55246e5eaf"),
    ]
    assert [c[:2] for c in calls].count(["cron", "create"]) == 3
    assert [c[:2] for c in calls].count(["cron", "edit"]) == 3
    assert (_scratch_hermes_home / "scripts" / "sherwood-monitor-digest.py").is_file()


def test_install_cron_reports_create_failure(install_cron_handler, capsys):
    def fake_run(argv, **kwargs):
        if argv[1:3] == ["cron", "list"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "sherwood-gas-watchdog" in argv:
            return MagicMock(returncode=2, stdout="", stderr="invalid schedule\n")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=fake_run):
        rc = install_cron_handler(Namespace())
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == [{"name": "sherwood-gas-watchdog", "phase": "create", "error": "invalid schedule"}]
    assert len(out["installed"]) == len(CRONS) - 1


def test_install_cron_skips_reasoning_when_disabled(install_cron_handler, capsys, tmp_path):
    cfg_dir = tmp_path / "home" / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("funds: []\nproposal_reasoning_enabled: false\n")

    def fake_run(argv, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("sherwood_monitor.cli.shutil.which", return_value="/usr/local/bin/hermes"), \
         patch("sherwood_monitor.cli.subprocess.run", side_effect=fake_run) as run:
        rc = install_cron_handler(Namespace())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {e["name"] for e in out["installed"]} == set(CRON_NAMES) - {"sherwood-proposal-reasoning"}
    assert [e["name"] for e in out["skipped"]] == ["sherwood-proposal-reasoning"]
    assert run.call_count == 1 + 4  # list + four creates


def _real_hermes() -> str | None:
    return os.environ.get("HERMES_BIN") or shutil.which("hermes")


@pytest.mark.skipif(_real_hermes() is None, reason="needs the real `hermes` binary (set HERMES_BIN or put it on PATH)")
def test_install_cron_against_the_real_hermes_binary(install_cron_handler, capsys, tmp_path, _scratch_hermes_home):
    """Real `hermes` in a scratch HERMES_HOME: five crons, idempotent re-run, and every wrapper runs.

    Never touches ~/.hermes: HERMES_HOME and HOME both point into tmp_path.
    """
    hermes = _real_hermes()
    with patch("sherwood_monitor.cli.shutil.which", return_value=hermes):
        assert install_cron_handler(Namespace()) == 0
        first = json.loads(capsys.readouterr().out)
        assert install_cron_handler(Namespace()) == 0
        second = json.loads(capsys.readouterr().out)
    assert {e["name"] for e in first["installed"]} == set(CRON_NAMES)
    assert second["installed"] == [] and {e["name"] for e in second["updated"]} == set(CRON_NAMES)

    env = {**os.environ, "HERMES_HOME": str(_scratch_hermes_home), "HOME": str(tmp_path / "home")}
    listing = subprocess.run([hermes, "cron", "list", "--all"], capture_output=True, text=True, env=env, timeout=120)
    assert listing.returncode == 0
    jobs = parse_cron_list(listing.stdout)
    assert {name: len(ids) for name, ids in jobs.items()} == {name: 1 for name in CRON_NAMES}
    for spec in CRONS:
        if spec.mode == "no_agent":
            assert f"Script:    {wrapper_name(spec)}" in listing.stdout

    # What Hermes does at each tick for a `.py` cron script: run it with its dependency
    # environment's Python (here, this test's Python), no arguments, a minimal environment.
    (tmp_path / "home").mkdir(exist_ok=True)
    for spec in CRONS:
        if spec.mode != "no_agent":
            continue
        script = _scratch_hermes_home / "scripts" / wrapper_name(spec)
        res = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")},
        )
        assert res.returncode == 0, (spec.name, res.stderr[-400:])
        assert res.stdout == "", spec.name  # nothing configured: every watchdog is silent
