"""Tests for sherwood_monitor.sidecar.Sidecar."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sherwood_monitor.sidecar import Sidecar, SidecarError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIDECAR_DIR = Path("/fake/xmtp_sidecar")
_PRIMARY_KEY = "0x" + "aa" * 32
_DB_PATH = Path("/fake/xmtp.db")


def _make_sidecar(sidecar_dir: Path, **kwargs) -> Sidecar:
    defaults = dict(
        primary_private_key_hex=_PRIMARY_KEY,
        db_path=_DB_PATH,
        node_bin="node",
        npm_bin="npm",
    )
    defaults.update(kwargs)
    return Sidecar(sidecar_dir=sidecar_dir, **defaults)


def _fake_proc(stdout_responses: list[dict], stderr_lines: list[str] = (), rc: int = 0):
    """Build a fake asyncio subprocess whose stdout yields JSON-encoded dicts.

    After all scripted responses are consumed, stdout blocks until the done event
    fires (via ``terminate()``), then returns ``b""`` to let the reader task exit.
    """
    proc = MagicMock()
    proc.pid = 99999
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()

    # Event that fires when terminate() is called so readers can unblock quickly
    _done = asyncio.Event()

    def _terminate():
        _done.set()

    proc.terminate.side_effect = _terminate
    proc.kill = MagicMock()

    stdout_queue: list[dict] = list(stdout_responses)
    queue_lock = asyncio.Lock()

    async def stdout_readline():
        async with queue_lock:
            if stdout_queue:
                msg = stdout_queue.pop(0)
                return (json.dumps(msg) + "\n").encode()
        # No more scripted responses — wait until terminate() signals done
        await _done.wait()
        return b""

    stderr_iter = iter(stderr_lines)

    async def stderr_readline():
        try:
            return (next(stderr_iter) + "\n").encode()
        except StopIteration:
            await _done.wait()
            return b""

    proc.stdout.readline = stdout_readline
    proc.stderr.readline = stderr_readline
    proc.wait = AsyncMock(return_value=rc)
    return proc


def _create_client_response(req_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"address": "0xDeAdBeEf", "inbox_id": "inbox_abc"},
    }


def _ok_response(req_id: int, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error_response(req_id: int, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


async def _force_shutdown(s: Sidecar) -> None:
    """Terminate sidecar tasks without going through the full RPC shutdown.

    Used in tests that don't need to verify shutdown behaviour but still need
    to clean up background tasks so the test suite doesn't hang.
    """
    if s._proc is not None:
        s._proc.terminate()
    for task in (s._reader_task, s._stderr_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    s._proc = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_calls_create_client_and_returns_identity(tmp_path):
    """start() spawns subprocess, calls create_client, returns {address, inbox_id}."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// fake")

    proc = _fake_proc([_create_client_response(1)])

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        s = _make_sidecar(tmp_path)
        result = await s.start()

    assert result["address"] == "0xDeAdBeEf"
    assert result["inbox_id"] == "inbox_abc"
    assert s.address == "0xDeAdBeEf"

    await _force_shutdown(s)


@pytest.mark.asyncio
async def test_call_rpc_round_trip(tmp_path):
    """call() writes proper JSON-RPC to stdin and resolves from stdout response."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// fake")

    proc = _fake_proc([
        _create_client_response(1),
        _ok_response(2, {"ok": True}),
    ])

    captured_stdin: list[str] = []

    def capture_write(data: bytes):
        captured_stdin.append(data.decode())

    proc.stdin.write = capture_write

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        s = _make_sidecar(tmp_path)
        await s.start()
        ping_result = await s.call("ping", {})

    assert ping_result == {"ok": True}

    # Verify proper JSON-RPC framing on stdin for the ping call
    ping_line = next(l for l in captured_stdin if '"ping"' in l)
    msg = json.loads(ping_line.strip())
    assert msg["jsonrpc"] == "2.0"
    assert msg["method"] == "ping"
    assert "id" in msg

    await _force_shutdown(s)


@pytest.mark.asyncio
async def test_call_handles_error_response(tmp_path):
    """call() raises SidecarError when sidecar returns an error object."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// fake")

    proc = _fake_proc([
        _create_client_response(1),
        _error_response(2, -32000, "boom"),
    ])

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        s = _make_sidecar(tmp_path)
        await s.start()
        with pytest.raises(SidecarError) as exc_info:
            await s.call("ping", {})

    assert exc_info.value.code == -32000
    assert "boom" in str(exc_info.value)

    await _force_shutdown(s)


@pytest.mark.asyncio
async def test_stream_event_notifications_dispatch_to_callback(tmp_path):
    """Unsolicited stream_event notifications are dispatched to registered callbacks."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// fake")

    notification = {
        "jsonrpc": "2.0",
        "method": "stream_event",
        "params": {
            "stream_id": "s_abc",
            "group_id": "0xgroup",
            "message_id": "msg_xyz",
            "sender_inbox_id": "inbox_sender",
            "content": "hello world",
            "sent_at_ns": "1710000000000000000",
        },
    }

    proc = _fake_proc([_create_client_response(1), notification])

    received: list[dict] = []

    async def on_event(params: dict) -> None:
        received.append(params)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        s = _make_sidecar(tmp_path)
        s.on_stream_event(on_event)
        await s.start()
        # Give the reader task a moment to process the notification
        await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0]["stream_id"] == "s_abc"
    assert received[0]["content"] == "hello world"

    await _force_shutdown(s)


@pytest.mark.asyncio
async def test_shutdown_sigterm_then_sigkill(tmp_path, monkeypatch):
    """shutdown() sends RPC shutdown, SIGTERM, then SIGKILL if proc survives grace."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// fake")

    proc = _fake_proc([
        _create_client_response(1),
        _ok_response(2, {"ok": True}),  # shutdown RPC response
    ])

    # Override terminate so it doesn't signal done (simulating a stubborn proc)
    proc.terminate.side_effect = MagicMock()

    # Proc refuses to exit on wait()
    async def never_exit():
        await asyncio.sleep(60)

    proc.wait = AsyncMock(side_effect=never_exit)

    monkeypatch.setattr("sherwood_monitor.sidecar.TERMINATION_GRACE_SEC", 0.05)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        s = _make_sidecar(tmp_path)
        await s.start()
        await s.shutdown()

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_stderr_ring_bounded(tmp_path):
    """stderr_tail() returns at most 200 lines even after 250 are pushed."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// fake")

    stderr_lines = [f"stderr line {i}" for i in range(250)]
    proc = _fake_proc([_create_client_response(1)], stderr_lines=stderr_lines)

    # Override stderr readline to drain all lines immediately without blocking
    stderr_iter = iter(stderr_lines)

    async def fast_stderr():
        try:
            return (next(stderr_iter) + "\n").encode()
        except StopIteration:
            return b""

    proc.stderr.readline = fast_stderr

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        s = _make_sidecar(tmp_path)
        await s.start()
        # Drain stderr fully
        for _ in range(30):
            if len(s.stderr_tail()) >= 200:
                break
            await asyncio.sleep(0.01)

    assert len(s.stderr_tail()) <= 200

    await _force_shutdown(s)


@pytest.mark.asyncio
async def test_missing_dist_triggers_rebuild(tmp_path):
    """When dist/index.js is absent, start() runs npm ci && npm run build."""
    # Do NOT create dist/index.js — sidecar must detect it is missing and build

    proc = _fake_proc([_create_client_response(1)])

    run_calls: list[list] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(list(cmd))
        # After "npm run build" pretend dist/index.js now exists
        if "build" in cmd:
            dist_dir = tmp_path / "dist"
            dist_dir.mkdir(exist_ok=True)
            (dist_dir / "index.js").write_text("// built")
        result = MagicMock()
        result.returncode = 0
        return result

    with (
        patch("subprocess.run", side_effect=fake_run),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
    ):
        s = _make_sidecar(tmp_path)
        await s.start()

    # Both npm ci and npm run build must have been called
    assert any("ci" in cmd for cmd in run_calls), "expected 'npm ci'"
    assert any("build" in cmd for cmd in run_calls), "expected 'npm run build'"

    await _force_shutdown(s)


@pytest.mark.asyncio
async def test_send_text_resolves_subdomain(tmp_path):
    """send_text(subdomain=...) resolves subdomain then sends text, returns message_id."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// fake")

    proc = _fake_proc([
        _create_client_response(1),
        _ok_response(2, {"group_id": "0xgroup123"}),  # resolve_subdomain
        _ok_response(3, {"message_id": "msg_001"}),   # send_text
    ])

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        s = _make_sidecar(tmp_path)
        await s.start()
        msg_id = await s.send_text(subdomain="alpha", text="hello")

    assert msg_id == "msg_001"
    await _force_shutdown(s)


@pytest.mark.asyncio
async def test_is_member_returns_bool(tmp_path):
    """is_member() resolves subdomain, calls get_conversation, returns member_of bool."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("// fake")

    proc = _fake_proc([
        _create_client_response(1),
        _ok_response(2, {"group_id": "0xgroup123"}),  # resolve_subdomain
        _ok_response(3, {"member_of": True}),          # get_conversation
    ])

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        s = _make_sidecar(tmp_path)
        await s.start()
        result = await s.is_member("alpha")

    assert result is True
    await _force_shutdown(s)
