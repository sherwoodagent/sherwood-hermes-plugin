from pathlib import Path
from unittest.mock import MagicMock, patch

from sherwood_monitor.preflight import (
    PreflightResult,
    check_cli_installed,
    run_preflight,
)


def test_check_cli_installed_ok():
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.71.0"))
    with patch("subprocess.run", fake_run):
        ok, version = check_cli_installed("sherwood")
    assert ok is True
    assert version == "0.71.0"


def test_check_cli_installed_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        ok, version = check_cli_installed("sherwood")
    assert ok is False
    assert version is None


def test_check_cli_installed_version_too_old():
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.70.9"))
    with patch("subprocess.run", fake_run):
        ok, version = check_cli_installed("sherwood", min_version="0.71.0")
    assert ok is False
    assert version == "0.70.9"


def test_run_preflight_never_touches_the_cli_config(tmp_path: Path):
    """No ~/.sherwood/config.json at all, and no warning about it (catalog rule 11)."""
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.71.0"))
    with patch("subprocess.run", fake_run):
        result = run_preflight("sherwood", home=tmp_path)
    assert result.warnings == []
    assert not hasattr(result, "config_ok")


def test_run_preflight_all_ok(tmp_path: Path):
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.71.0"))
    with patch("subprocess.run", fake_run):
        result = run_preflight("sherwood", home=tmp_path)
    assert isinstance(result, PreflightResult)
    assert result.cli_ok is True
    assert result.warnings == []


def test_run_preflight_missing_cli(tmp_path: Path):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = run_preflight("sherwood", home=tmp_path)
    assert result.cli_ok is False
    assert any("npm i -g @sherwoodagent/cli" in w for w in result.warnings)
