import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sherwood_monitor.config import Config
from sherwood_monitor.event_buffer import EventBuffer
from sherwood_monitor.hooks import make_pre_llm_call_hook, make_session_finalize_hook, make_session_hooks


@pytest.mark.asyncio
async def test_session_start_injects_catchup_summary(fixture):
    cfg = Config(sherwood_bin="sherwood", funds=["alpha"], auto_start=False)
    buffer = MagicMock(spec=EventBuffer)
    sup = MagicMock()
    sup.start = AsyncMock()

    payload = json.dumps(fixture("session_check_output"))

    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=[(payload.encode(), b"")])
    proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        hooks = make_session_hooks(cfg=cfg, buffer=buffer, supervisor=sup)
        await hooks["on_session_start"]()

    # Pushed a catch-up summary referencing the fund
    assert any(
        "alpha" in call.args[0]
        for call in buffer.push.call_args_list
    )


@pytest.mark.asyncio
async def test_session_start_auto_starts_supervisors(fixture):
    cfg = Config(sherwood_bin="sherwood", funds=["alpha"], auto_start=True)
    buffer = MagicMock(spec=EventBuffer)
    sup = MagicMock()
    sup.start = AsyncMock()

    payload = json.dumps(
        {"fund": "alpha", "messages": [], "events": [], "meta": {"newMessages": 0, "newEvents": 0, "blocksScanned": 0, "lastCheckAt": "never"}}
    )
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(payload.encode(), b""))
    proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        hooks = make_session_hooks(cfg=cfg, buffer=buffer, supervisor=sup)
        await hooks["on_session_start"]()

    sup.start.assert_awaited_once_with("alpha")


def test_session_finalize_tears_down_only_at_shutdown():
    """Hermes 0.22: on_session_end fires every turn, on_session_finalize also on /new.
    Only reason="shutdown" (CLI quit, gateway shutdown) stops the streams."""
    teardown = MagicMock()
    hook = make_session_finalize_hook(teardown)
    hook(session_id="s1", platform="cli", reason="session_boundary")  # CLI /new
    hook(session_id="s1", platform="telegram", reason="new_session")
    hook(session_id="s1", platform="telegram")
    teardown.assert_not_called()
    hook(session_id="s1", platform="cli", reason="shutdown")
    teardown.assert_called_once()


@pytest.mark.asyncio
async def test_session_start_calls_ensure_streams_after_catchup():
    cfg = Config(sherwood_bin="sherwood", funds=["alpha"], auto_start=True)
    sup = MagicMock()
    sup.start = AsyncMock()
    ensure = MagicMock()
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"{}", b""))
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        hooks = make_session_hooks(cfg=cfg, buffer=EventBuffer(), supervisor=sup, ensure_streams=ensure)
        await hooks["on_session_start"]()
    ensure.assert_called_once()
    sup.start.assert_not_awaited()  # the plugin loop starts streams, not the hook's loop


@pytest.mark.asyncio
async def test_pre_llm_call_runs_before_then_drains_the_buffer():
    buffer = EventBuffer()
    calls = []

    def before():
        calls.append("before")
        if len(calls) == 1:  # e.g. a stream event routed between turns
            buffer.push("<sherwood-event a/>")

    hook = make_pre_llm_call_hook(buffer, before=before)
    out = await hook(session_id="s1")
    assert out == {"context": "<sherwood-event a/>"}
    assert await hook(session_id="s1") is None  # drained
    assert calls == ["before", "before"]


from unittest.mock import AsyncMock

from sherwood_monitor.hooks import make_pre_tool_call_hook
from sherwood_monitor.risk import RiskVerdict


def _state_fetcher(result):
    async def fetch(sub):
        return result
    return fetch


@pytest.mark.asyncio
async def test_pre_tool_call_passes_through_non_sherwood_commands():
    fetch = _state_fetcher({"vault_aum_usd": 100_000, "current_exposure_usd": 0, "allowed_protocols": ["moonwell"]})
    hook = make_pre_tool_call_hook(state_fetcher=fetch)
    result = await hook(tool_name="bash", params={"command": "ls -la"})
    assert result is None


@pytest.mark.asyncio
async def test_pre_tool_call_passes_through_non_terminal_tools():
    fetch = _state_fetcher({"vault_aum_usd": 100_000, "current_exposure_usd": 0, "allowed_protocols": ["moonwell"]})
    hook = make_pre_tool_call_hook(state_fetcher=fetch)
    result = await hook(
        tool_name="web_search", params={"command": "sherwood proposal create alpha --size-usd 5000"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_pre_tool_call_blocks_oversized_proposal():
    fetch = _state_fetcher({"vault_aum_usd": 100_000, "current_exposure_usd": 0, "allowed_protocols": ["moonwell"]})
    hook = make_pre_tool_call_hook(state_fetcher=fetch)
    result = await hook(
        tool_name="bash",
        params={
            "command": "sherwood proposal create alpha --size-usd 30000 --protocol moonwell"
        },
    )
    # Hermes vetoes on {"action": "block", "message": ...}; the old keys ride along.
    assert result["action"] == "block"
    assert result["message"] == result["reason"]
    assert result["blocked"] is True
    assert "position" in result["reason"].lower()


@pytest.mark.asyncio
async def test_pre_tool_call_allows_compliant_proposal():
    fetch = _state_fetcher({"vault_aum_usd": 100_000, "current_exposure_usd": 0, "allowed_protocols": ["moonwell"]})
    hook = make_pre_tool_call_hook(state_fetcher=fetch)
    result = await hook(
        tool_name="bash",
        params={
            "command": "sherwood proposal create alpha --size-usd 5000 --protocol moonwell"
        },
    )
    assert result is None


@pytest.mark.asyncio
async def test_pre_tool_call_blocks_disallowed_protocol():
    fetch = _state_fetcher({"vault_aum_usd": 100_000, "current_exposure_usd": 0, "allowed_protocols": ["moonwell"]})
    hook = make_pre_tool_call_hook(state_fetcher=fetch)
    result = await hook(
        tool_name="bash",
        params={
            "command": "sherwood proposal create alpha --size-usd 5000 --protocol unknown"
        },
    )
    assert result is not None
    assert result["blocked"] is True
    assert "mandate" in result["reason"].lower()


@pytest.mark.asyncio
async def test_pre_tool_call_strategy_propose_pattern():
    fetch = _state_fetcher({"vault_aum_usd": 100_000, "current_exposure_usd": 0, "allowed_protocols": ["moonwell"]})
    hook = make_pre_tool_call_hook(state_fetcher=fetch)
    result = await hook(
        tool_name="terminal",
        params={
            "command": "sherwood strategy propose alpha --size-usd 5000 --protocol moonwell"
        },
    )
    assert result is None


@pytest.mark.asyncio
async def test_pre_tool_call_swallows_fetcher_exception():
    async def fetch(sub):
        raise RuntimeError("rpc down")

    hook = make_pre_tool_call_hook(state_fetcher=fetch)
    result = await hook(
        tool_name="bash",
        params={
            "command": "sherwood proposal create alpha --size-usd 5000 --protocol moonwell"
        },
    )
    # On fetcher error, pass through (don't block agent if we can't verify)
    assert result is None


@pytest.mark.asyncio
async def test_pre_tool_call_fails_open_when_state_is_none(caplog):
    """Regression: when state_fetcher returns None (CLI vault info unavailable),
    the hook must allow the proposal and log a warning — NOT block as if AUM=0."""
    async def fetch(sub):
        return None

    hook = make_pre_tool_call_hook(state_fetcher=fetch)
    result = await hook(
        tool_name="bash",
        params={
            "command": "sherwood proposal create alpha --size-usd 50000 --protocol moonwell"
        },
    )
    assert result is None
    assert any(
        "vault state unavailable" in r.message for r in caplog.records
    )


from sherwood_monitor.hooks import make_post_tool_call_hook


@pytest.mark.asyncio
async def test_post_tool_call_writes_memory_on_execute():
    writer = MagicMock()
    buffer = MagicMock(spec=EventBuffer)
    hook = make_post_tool_call_hook(memory_writer=writer, buffer=buffer)
    await hook(
        tool_name="bash",
        params={"command": "sherwood proposal execute alpha 42"},
        result='{"tx": "0xabc", "proposalId": 42}',
    )
    writer.assert_called_once()
    assert writer.call_args.args[0]["action"] == "execute"


@pytest.mark.asyncio
async def test_post_tool_call_writes_memory_on_settle():
    writer = MagicMock()
    buffer = MagicMock(spec=EventBuffer)
    hook = make_post_tool_call_hook(memory_writer=writer, buffer=buffer)
    await hook(
        tool_name="bash",
        params={"command": "sherwood proposal settle alpha 42"},
        result='{"tx": "0xdef", "proposalId": 42, "pnl": "500000000"}',
    )
    writer.assert_called_once()
    assert writer.call_args.args[0]["action"] == "settle"
    assert writer.call_args.args[0]["pnl_usd"] == 500.0


@pytest.mark.asyncio
async def test_post_tool_call_skips_other_commands():
    writer = MagicMock()
    buffer = MagicMock(spec=EventBuffer)
    hook = make_post_tool_call_hook(memory_writer=writer, buffer=buffer)
    await hook(
        tool_name="bash",
        params={"command": "ls -la"},
        result="total 0\n",
    )
    writer.assert_not_called()
    buffer.push.assert_not_called()


@pytest.mark.asyncio
async def test_post_tool_call_swallows_writer_error(caplog):
    writer = MagicMock(side_effect=RuntimeError("oom"))
    buffer = MagicMock(spec=EventBuffer)
    hook = make_post_tool_call_hook(memory_writer=writer, buffer=buffer)
    # Must not raise
    await hook(
        tool_name="bash",
        params={"command": "sherwood proposal execute alpha 42"},
        result='{"tx": "0xabc"}',
    )
    # Regression: writer failures must surface via logs, not be silently dropped
    assert any(
        "settlement memory write failed" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_post_tool_call_logs_buffer_push_error(caplog):
    writer = MagicMock()
    buffer = MagicMock(spec=EventBuffer)
    buffer.push.side_effect = RuntimeError("buffer gone")
    hook = make_post_tool_call_hook(memory_writer=writer, buffer=buffer)
    await hook(
        tool_name="bash",
        params={"command": "sherwood proposal settle alpha 42"},
        result='{"tx": "0xabc", "proposalId": 42}',
    )
    assert any(
        "settlement buffer push failed" in r.message for r in caplog.records
    )
    # Memory writer still got called despite buffer failure
    writer.assert_called_once()


@pytest.mark.asyncio
async def test_post_tool_call_pushes_settlement_block_to_buffer():
    writer = MagicMock()
    buffer = MagicMock(spec=EventBuffer)
    hook = make_post_tool_call_hook(memory_writer=writer, buffer=buffer)
    await hook(
        tool_name="bash",
        params={"command": "sherwood proposal settle alpha 7"},
        result='{"tx": "0xabc", "proposalId": 7, "pnl": "1000000000"}',
    )
    buffer.push.assert_called_once()
    pushed = buffer.push.call_args.args[0]
    assert "<sherwood-settlement" in pushed
    assert "REMEMBER THIS" in pushed
    assert "alpha" in pushed


from sherwood_monitor.hooks import make_pre_llm_call_hook


@pytest.mark.asyncio
async def test_pre_llm_call_drains_buffer_and_returns_context():
    buf = EventBuffer()
    buf.push("<a>1</a>")
    buf.push("<b>2</b>")
    hook = make_pre_llm_call_hook(buf)
    result = await hook(session_id="s1", user_message="hi")
    assert result == {"context": "<a>1</a>\n\n<b>2</b>"}
    assert len(buf) == 0


@pytest.mark.asyncio
async def test_pre_llm_call_returns_none_when_empty():
    hook = make_pre_llm_call_hook(EventBuffer())
    result = await hook(session_id="s1")
    assert result is None


def test_the_settlement_block_names_the_skill_the_way_hermes_resolves_it():
    """Hermes registers the pack as sherwood-monitor:sherwood-agent; remember-settlement is a file in it."""
    from pathlib import Path

    from sherwood_monitor.hooks import (
        SETTLEMENT_SKILL,
        SETTLEMENT_SKILL_FILE,
        _format_settlement_block,
    )

    block = _format_settlement_block({"fund": "alpha", "action": "settle", "proposal_id": "1", "tx_hash": "0x1"})
    assert 'skill_view name="sherwood-monitor:sherwood-agent"' in block
    assert 'file_path="skills/remember-settlement/SKILL.md"' in block
    assert "use the remember-settlement skill" not in block  # not a registered skill name
    pack = Path(__file__).parent.parent / "skills" / "sherwood-agent"
    assert SETTLEMENT_SKILL == "sherwood-monitor:" + pack.name
    assert (pack / SETTLEMENT_SKILL_FILE).is_file()
