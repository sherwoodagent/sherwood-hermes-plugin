from unittest.mock import AsyncMock, patch

import pytest

from sherwood_monitor import state_fetcher
from sherwood_monitor.state_fetcher import default_state_fetcher


@pytest.fixture(autouse=True)
def _fresh_json_probe():
    state_fetcher._SUPPORTS_JSON.clear()
    yield
    state_fetcher._SUPPORTS_JSON.clear()


@pytest.mark.asyncio
async def test_fetcher_returns_none_on_cli_error():
    """Regression: fail-open on unknown state. Returning zero-defaults would
    make the pre_tool_call hook see AUM=0 and block every proposal."""
    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError),
    ):
        state = await default_state_fetcher("sherwood", "alpha")
    assert state is None


@pytest.mark.asyncio
async def test_fetcher_returns_none_on_nonzero_exit():
    """CLI ran but exited non-zero (e.g., `vault info --json` subcommand doesn't exist)."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b"unknown flag --json"))
    proc.returncode = 2
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        state = await default_state_fetcher("sherwood", "alpha")
    assert state is None


@pytest.mark.asyncio
async def test_fetcher_returns_none_on_malformed_payload():
    """CLI returned JSON but with non-numeric fields."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b'{"aumUsd": "not-a-number"}', b""))
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        state = await default_state_fetcher("sherwood", "alpha")
    assert state is None


@pytest.mark.asyncio
async def test_fetcher_parses_valid_json(monkeypatch):
    async def supports_json(sherwood_bin):  # a CLI whose `vault info --help` lists --json
        return True

    monkeypatch.setattr(state_fetcher, "vault_info_supports_json", supports_json)
    payload = b'{"aumUsd": "150000", "currentExposureUsd": "10000", "allowedProtocols": ["moonwell", "aerodrome"]}'
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(payload, b""))
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        state = await default_state_fetcher("sherwood", "alpha")
    assert state["vault_aum_usd"] == 150_000
    assert state["current_exposure_usd"] == 10_000
    assert state["allowed_protocols"] == ["moonwell", "aerodrome"]


# CLI 0.103.0 `sherwood vault info --help`, verbatim: no --json (and `--json` exits 1 "unknown option").
HELP_0_103_0 = b"""Usage: sherwood vault info [options] [id]

Display vault details by ID or subdomain (omit the ID to read the configured
vault)

Arguments:
  id                 Vault ID or subdomain name (default: the vault from
                     --vault or config)

Options:
  --vault <address>  Vault address (default: from config)
  -h, --help         display help for command
"""


@pytest.mark.asyncio
async def test_fetcher_never_passes_json_to_a_cli_whose_help_lacks_it(caplog):
    """CLI 0.103.0 rejects `vault info --json`: read the help once, run nothing else, say why."""
    calls = []

    async def fake_exec(*argv, **kw):
        calls.append(argv)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(HELP_0_103_0, b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", fake_exec):
        assert await default_state_fetcher("sherwood", "0x" + "11" * 20) is None
        assert await default_state_fetcher("sherwood", "alpha") is None
    assert calls == [("sherwood", "vault", "info", "--help")]  # probed once, cached, never --json
    assert any("no machine-readable output" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_fetcher_reads_json_when_the_help_lists_it():
    calls = []

    async def fake_exec(*argv, **kw):
        calls.append(argv)
        proc = AsyncMock()
        out = b"  --json  print JSON\n" if "--help" in argv else b'{"aumUsd": "100", "currentExposureUsd": "0"}'
        proc.communicate = AsyncMock(return_value=(out, b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", fake_exec):
        state = await default_state_fetcher("sherwood", "alpha")
    assert state["vault_aum_usd"] == 100
    assert calls[-1] == ("sherwood", "vault", "info", "alpha", "--json")
