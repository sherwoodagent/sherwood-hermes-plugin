"""Streams live for the Hermes process, not one turn (review #29 P5).

Hermes 0.22 fires `on_session_end` at the end of every turn and
`on_session_finalize` on /new too; only reason="shutdown" (CLI quit, gateway
shutdown) or process exit may stop the streams. These tests run a real
Supervisor on the plugin's own loop against a stub `sherwood` binary.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from tests.fake_ctx import FakeCtx

VOTE = json.dumps({"source": "chain", "type": "VoteCast", "block": 1, "tx": "0xbbb222",
                   "args": {"proposalId": "42", "voter": "0x" + "3" * 40, "support": "0", "weight": "1"}})


def _stub_sherwood(tmp_path: Path) -> Path:
    """`sherwood session check <sub> --stream --no-xmtp` prints one event then stays up;
    the one-shot catch-up prints `{}`."""
    stub = tmp_path / "sherwood-stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "if '--stream' in sys.argv:\n"
        f"    print({VOTE!r}, flush=True)\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    print('{}', flush=True)\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def _boot(tmp_path: Path, monkeypatch) -> FakeCtx:
    monkeypatch.delenv("SHERWOOD_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("SHERWOOD_AGENT_ADDRESS", "0x" + "12" * 20)
    cfg_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({
        "funds": ["alpha"], "auto_start": True, "sherwood_bin": str(_stub_sherwood(tmp_path)),
    }))
    ctx = FakeCtx()
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(side_effect=FileNotFoundError)):
        from sherwood_monitor import register

        register(ctx)
    return ctx


def _wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_stream_survives_turn_ends_and_new_sessions_and_stops_at_shutdown(tmp_path, monkeypatch):
    import sherwood_monitor

    ctx = _boot(tmp_path, monkeypatch)
    rt = sherwood_monitor._RUNTIME
    sup = rt["supervisor"]
    kw = {"session_id": "s1", "platform": "cli"}

    # Turn 1: pre_llm_call starts the auto_start stream on the plugin's loop.
    asyncio.run(ctx.fire("pre_llm_call", **kw))
    assert _wait_for(lambda: sup.status()["funds"] and sup.status()["funds"][0]["events_seen"] >= 1)
    pid = sup.status()["funds"][0]["pid"]
    assert pid and _alive(pid)

    # Two turn ends and a /new: Hermes fires these between turns. The stream must live on.
    asyncio.run(ctx.fire("on_session_end", completed=True, **kw))
    asyncio.run(ctx.fire("on_session_end", completed=True, **kw))
    asyncio.run(ctx.fire("on_session_finalize", reason="session_boundary", **kw))
    time.sleep(0.3)
    assert _alive(pid), "a turn end or /new stopped the stream"
    assert sup.status()["funds"][0]["pid"] == pid

    # Turn 2: the event the stream routed since turn 1 reaches the model.
    (out,) = asyncio.run(ctx.fire("pre_llm_call", **kw))
    assert '<sherwood-event fund="alpha"' in out["context"] and "VoteCast" in out["context"]

    # Hermes shutdown: CLI quit / gateway shutdown pass reason="shutdown".
    asyncio.run(ctx.fire("on_session_finalize", reason="shutdown", **kw))
    assert _wait_for(lambda: not _alive(pid), timeout=10)
    assert rt["bg"].closed
    status = json.loads((tmp_path / "stream_status.json").read_text())
    assert status["funds"] == []  # a clean stop, not a dead stream


def test_exit_hook_stops_streams_when_finalize_never_fired(tmp_path, monkeypatch):
    """The atexit fallback (os._exit paths skip it; a SIGKILL skips everything)."""
    import sherwood_monitor

    ctx = _boot(tmp_path, monkeypatch)
    rt = sherwood_monitor._RUNTIME
    asyncio.run(ctx.fire("pre_llm_call", session_id="s1"))
    assert _wait_for(lambda: rt["supervisor"].status()["funds"] and rt["supervisor"].status()["funds"][0]["pid"])
    pid = rt["supervisor"].status()["funds"][0]["pid"]
    rt["teardown"]()  # what atexit runs
    assert _wait_for(lambda: not _alive(pid), timeout=10)
    rt["teardown"]()  # idempotent


def test_a_short_cli_command_starts_nothing(tmp_path, monkeypatch):
    """register() runs for every `hermes` command; without a turn nothing is spawned."""
    import sherwood_monitor

    _boot(tmp_path, monkeypatch)
    rt = sherwood_monitor._RUNTIME
    assert rt["supervisor"].status()["funds"] == []
    assert not rt["bg"].started
    rt["teardown"]()
    assert rt["bg"].closed


def test_teardown_closes_the_telegram_fund_bridge(tmp_path, monkeypatch):
    """Shutdown also closes the fund's Telegram bridge, which cancels pending widget refreshes."""
    import sherwood_monitor
    from sherwood_monitor.fund import telegram as fund_telegram

    bridge = MagicMock()
    bridge.pre_llm_context = MagicMock(return_value=None)
    monkeypatch.setattr(fund_telegram, "wire_fund_telegram", lambda *a, **k: bridge)
    _boot(tmp_path, monkeypatch)
    bridge.close.assert_not_called()
    sherwood_monitor._RUNTIME["teardown"]()
    bridge.close.assert_called_once_with()


def test_boot_clears_a_status_file_left_by_a_dead_hermes(tmp_path, monkeypatch):
    (tmp_path / "stream_status.json").write_text(json.dumps(
        {"written_at": 0, "host_pid": 999999, "funds": [{"subdomain": "alpha", "pid": 999998}]}
    ))
    _boot(tmp_path, monkeypatch)
    data = json.loads((tmp_path / "stream_status.json").read_text())
    assert data["funds"] == [] and data["host_pid"] == os.getpid()


def test_tool_started_stream_outlives_the_tool_call_loop(tmp_path, monkeypatch):
    """sherwood_monitor_start runs on Hermes' loop (asyncio.run here, a closed worker loop in
    the gateway); the stream itself lives on the plugin's loop and keeps routing events."""
    import sherwood_monitor

    ctx = _boot(tmp_path, monkeypatch)
    rt = sherwood_monitor._RUNTIME
    out = ctx.call_tool("sherwood_monitor_start", {"subdomain": "alpha"})  # asyncio.run: loop closes after
    assert out["started"] is True and out["pid"]
    assert _wait_for(lambda: rt["supervisor"].status()["funds"][0]["events_seen"] >= 1)
    assert _alive(out["pid"])
    rt["teardown"]()
    assert _wait_for(lambda: not _alive(out["pid"]), timeout=10)


def test_boot_leaves_a_live_hermes_status_file_alone(tmp_path, monkeypatch):
    live = {"written_at": 0, "host_pid": os.getppid(), "funds": [{"subdomain": "alpha", "pid": os.getppid()}]}
    (tmp_path / "stream_status.json").write_text(json.dumps(live))
    _boot(tmp_path, monkeypatch)
    assert json.loads((tmp_path / "stream_status.json").read_text()) == live
