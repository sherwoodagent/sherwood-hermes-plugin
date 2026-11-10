"""Shared pytest fixtures."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sherwood_monitor.fund import applink

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a JSON fixture by filename (without .json)."""
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def fixture():
    """Factory fixture for loading test fixtures by name."""
    return load_fixture


@pytest.fixture
def mock_ctx():
    """Mock Hermes plugin context with common methods."""
    ctx = MagicMock()
    ctx.register_tool = MagicMock()
    ctx.register_hook = MagicMock()
    ctx.register_cli_command = MagicMock()
    ctx.register_skill = MagicMock()
    return ctx


@pytest.fixture(autouse=True)
def _no_sherwood_strategy_list(monkeypatch):
    """`sherwood strategy list` never runs in tests, whatever is on PATH: the real CLI 0.103.0
    output (fixtures/strategy_list_0.103.0.txt) unless a test passes its own runner. The
    discovery cache is cleared around every test."""
    from sherwood_monitor.fund import scope

    listing = (FIXTURES_DIR / "strategy_list_0.103.0.txt").read_text()
    monkeypatch.setattr(scope, "_run", lambda argv, timeout: subprocess.CompletedProcess(argv, 0, listing, ""))
    monkeypatch.setattr(scope, "_which", lambda name: f"/stub/bin/{name}")
    scope._DISCOVERY.clear()
    yield
    scope._DISCOVERY.clear()


@pytest.fixture(autouse=True)
def _no_app_link_network(monkeypatch):
    """The app-link lookup (GET /api/tg/link) never leaves the test process: 'not linked' unless a test stubs it."""
    monkeypatch.setattr(applink, "_http_get_json", lambda url, headers, timeout: (404, {}))


@pytest.fixture(autouse=True)
def _no_prices_network(monkeypatch):
    """The draft symbol check's GET /sources/prices never leaves the test process: unreachable
    (drafts go ahead unchecked, with a note) unless a test stubs it. Cache cleared around every test."""
    from sherwood_monitor.fund import symbols

    def offline(url, timeout):
        raise OSError("no network in tests")

    monkeypatch.setattr(symbols, "_get_json", offline)
    symbols._SOURCES.clear()
    yield
    symbols._SOURCES.clear()


@pytest.fixture(autouse=True)
def _no_share_image_network(monkeypatch):
    """Share-image readiness checks never leave the test process: 404 (send text only) unless stubbed."""
    monkeypatch.setattr(applink, "_image_status", lambda url, timeout: 404)


@pytest.fixture(autouse=True)
def _stream_status_in_tmp(monkeypatch, tmp_path):
    """The Supervisor's stream-status file never lands in the real ~/.hermes."""
    from sherwood_monitor import supervisor

    monkeypatch.setattr(supervisor, "default_status_path", lambda: tmp_path / "stream_status.json")
