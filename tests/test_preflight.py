from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sherwood_monitor.preflight import (
    PreflightResult,
    check_cli_configured,
    check_cli_installed,
    check_memberships,
    check_sidecar_built,
    check_sidecar_health,
    run_preflight,
)


def test_check_cli_installed_ok():
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.40.5"))
    with patch("subprocess.run", fake_run):
        ok, version = check_cli_installed("sherwood")
    assert ok is True
    assert version == "0.40.5"


def test_check_cli_installed_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        ok, version = check_cli_installed("sherwood")
    assert ok is False
    assert version is None


def test_check_cli_installed_version_too_old():
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.40.4"))
    with patch("subprocess.run", fake_run):
        ok, version = check_cli_installed("sherwood", min_version="0.40.5")
    assert ok is False
    assert version == "0.40.4"


def test_check_cli_configured_ok(tmp_path: Path):
    cfg = tmp_path / ".sherwood" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text("{}")
    assert check_cli_configured(tmp_path) is True


def test_check_cli_configured_missing(tmp_path: Path):
    assert check_cli_configured(tmp_path) is False


def test_run_preflight_all_ok(tmp_path: Path):
    cfg = tmp_path / ".sherwood" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text("{}")
    # Build a fake sidecar dist so sidecar_built=True and no warning is emitted
    sidecar_dir = tmp_path / "xmtp_sidecar"
    (sidecar_dir / "dist").mkdir(parents=True)
    (sidecar_dir / "dist" / "index.js").write_text("// built")
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.40.5"))
    with patch("subprocess.run", fake_run):
        result = run_preflight("sherwood", sidecar_dir=sidecar_dir, home=tmp_path)
    assert isinstance(result, PreflightResult)
    assert result.cli_ok is True
    assert result.config_ok is True
    assert result.sidecar_built is True
    assert result.warnings == []


def test_run_preflight_missing_cli(tmp_path: Path):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = run_preflight("sherwood", home=tmp_path)
    assert result.cli_ok is False
    assert any("npm i -g @sherwoodagent/cli" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# check_sidecar_built
# ---------------------------------------------------------------------------

def test_check_sidecar_built_present(tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// built")
    assert check_sidecar_built(tmp_path) is True


def test_check_sidecar_built_absent(tmp_path: Path):
    assert check_sidecar_built(tmp_path) is False


# ---------------------------------------------------------------------------
# check_sidecar_health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_sidecar_health_success():
    mock_sidecar = MagicMock()
    mock_sidecar.call = AsyncMock(return_value={"ok": True})
    mock_sidecar.address = "0xABCDEF"

    ok, addr = await check_sidecar_health(mock_sidecar)

    assert ok is True
    assert addr == "0xABCDEF"
    mock_sidecar.call.assert_awaited_once_with("ping", {}, timeout=5.0)


@pytest.mark.asyncio
async def test_check_sidecar_health_failure():
    mock_sidecar = MagicMock()
    mock_sidecar.call = AsyncMock(side_effect=RuntimeError("connection refused"))
    mock_sidecar.address = None

    ok, addr = await check_sidecar_health(mock_sidecar)

    assert ok is False
    assert addr is None


# ---------------------------------------------------------------------------
# check_memberships
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_memberships_all_members():
    mock_sidecar = MagicMock()
    mock_sidecar.is_member = AsyncMock(return_value=True)

    missing = await check_memberships(mock_sidecar, ["alpha-fund", "beta-fund"])

    assert missing == []


@pytest.mark.asyncio
async def test_check_memberships_partial_missing():
    async def _is_member(sub: str) -> bool:
        return sub == "alpha-fund"  # only alpha is a member

    mock_sidecar = MagicMock()
    mock_sidecar.is_member = _is_member

    missing = await check_memberships(mock_sidecar, ["alpha-fund", "beta-fund"])

    assert missing == ["beta-fund"]


@pytest.mark.asyncio
async def test_check_memberships_errors_surface():
    mock_sidecar = MagicMock()
    mock_sidecar.is_member = AsyncMock(side_effect=RuntimeError("rpc timeout"))

    missing = await check_memberships(mock_sidecar, ["alpha-fund"])

    # Error counts as missing — surface it so the user knows
    assert "alpha-fund" in missing


# ---------------------------------------------------------------------------
# run_preflight includes sidecar_built warning
# ---------------------------------------------------------------------------

def test_run_preflight_includes_sidecar_built_warning(tmp_path: Path):
    """sidecar_dir provided but dist/index.js absent → warning in result."""
    sidecar_dir = tmp_path / "xmtp_sidecar"
    sidecar_dir.mkdir()
    # No dist/ subdirectory → sidecar not built

    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.40.5"))
    sherwood_home = tmp_path / "home"
    (sherwood_home / ".sherwood").mkdir(parents=True)
    (sherwood_home / ".sherwood" / "config.json").write_text("{}")

    with patch("subprocess.run", fake_run):
        result = run_preflight("sherwood", sidecar_dir=sidecar_dir, home=sherwood_home)

    assert result.sidecar_built is False
    assert any("npm ci && npm run build" in w for w in result.warnings)


def test_run_preflight_sidecar_dir_none_adds_warning(tmp_path: Path):
    """sidecar_dir=None → sidecar_built=False + warning about not configured."""
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="0.40.5"))
    sherwood_home = tmp_path / "home"
    (sherwood_home / ".sherwood").mkdir(parents=True)
    (sherwood_home / ".sherwood" / "config.json").write_text("{}")

    with patch("subprocess.run", fake_run):
        result = run_preflight("sherwood", sidecar_dir=None, home=sherwood_home)

    assert result.sidecar_built is False
    assert any("sidecar" in w.lower() for w in result.warnings)
