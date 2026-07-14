"""Sidecar: spawns and manages the TypeScript XMTP JSON-RPC sidecar subprocess.

The sidecar exposes JSON-RPC 2.0 over stdin/stdout (newline-delimited).
This module handles:
  - Auto-build when dist/index.js is missing (npm ci && npm run build)
  - Subprocess lifecycle (spawn, stdin write lock, stdout reader, stderr ring)
  - Request/response demuxing via pending-future dict keyed on request id
  - Notification dispatch for unsolicited ``stream_event`` messages
  - Graceful shutdown: RPC shutdown → SIGTERM (5s grace) → SIGKILL
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .key_derivation import DEFAULT_SALT, address_from_private_key, derive_sidecar_key

_log = logging.getLogger(__name__)

_STDERR_RING_SIZE = 200
TERMINATION_GRACE_SEC = 5.0


class SidecarError(RuntimeError):
    """Raised when the sidecar returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"sidecar error {code}: {message}")
        self.code = code
        self.rpc_message = message


class Sidecar:
    """Manages the XMTP TypeScript sidecar subprocess.

    Usage::

        sidecar = Sidecar(
            sidecar_dir=Path("xmtp_sidecar"),
            primary_private_key_hex="0x...",
            db_path=Path("~/.hermes/xmtp.db"),
        )
        identity = await sidecar.start()   # {address, inbox_id}
        await sidecar.send_text(subdomain="my-fund", text="hello")
        await sidecar.shutdown()
    """

    def __init__(
        self,
        sidecar_dir: Path,
        primary_private_key_hex: str,
        db_path: Path,
        *,
        env: str = "production",
        salt: str = DEFAULT_SALT,
        node_bin: str = "node",
        npm_bin: str = "npm",
    ) -> None:
        self._sidecar_dir = Path(sidecar_dir)
        self._primary_key_hex = primary_private_key_hex
        self._db_path = Path(db_path)
        self._env = env
        self._salt = salt
        self._node_bin = node_bin
        self._npm_bin = npm_bin

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict]] = {}
        # Buffer for responses that arrive before call() registers a future
        self._response_buffer: dict[int, dict] = {}

        self._stderr_ring: deque[str] = deque(maxlen=_STDERR_RING_SIZE)
        self._stream_event_callbacks: list[Callable[[dict], Awaitable[None]]] = []

        self._address: str | None = None
        self._sidecar_ok: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> dict:
        """Build sidecar if needed, spawn subprocess, call create_client.

        Returns ``{address, inbox_id}`` from the sidecar.
        """
        self._maybe_build()

        entry = self._sidecar_dir / "dist" / "index.js"
        self._proc = await asyncio.create_subprocess_exec(
            self._node_bin,
            str(entry),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._sidecar_dir),
        )

        self._reader_task = asyncio.create_task(
            self._read_stdout(), name="sidecar-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name="sidecar-stderr"
        )

        sidecar_key = derive_sidecar_key(self._primary_key_hex, self._salt)
        result = await self.call(
            "create_client",
            {
                "private_key_hex": sidecar_key,
                "db_path": str(self._db_path),
                "env": self._env,
            },
        )
        self._address = result.get("address")
        self._sidecar_ok = True
        return result

    async def call(
        self, method: str, params: dict | None = None, timeout: float = 30.0
    ) -> dict:
        """Send a JSON-RPC request and await its response.

        The lock serializes writes to stdin so requests don't interleave on the
        wire.  The future is registered before the lock is released so the
        reader task can resolve it at any point after the write completes.
        Raises ``SidecarError`` on error responses and ``asyncio.TimeoutError``
        if the sidecar doesn't respond within ``timeout`` seconds.
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()

        async with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = fut

            msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params

            line = json.dumps(msg) + "\n"
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(line.encode())
            await self._proc.stdin.drain()

            # Check if the reader already buffered a response for this id
            # (can happen if the sidecar responds before we even yield the lock).
            buffered = self._response_buffer.pop(req_id, None)
            if buffered is not None and not fut.done():
                fut.set_result(buffered)
        # Lock released here — reader task can now resolve the future concurrently

        try:
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise
        finally:
            self._pending.pop(req_id, None)

        if "error" in result:
            err = result["error"]
            raise SidecarError(err.get("code", -1), err.get("message", "unknown"))
        return result.get("result", {})

    async def send_text(
        self,
        *,
        group_id: str | None = None,
        subdomain: str | None = None,
        text: str,
        markdown: bool = False,
    ) -> str:
        """Send a text message to a group.

        Accepts either a ``group_id`` directly or a ``subdomain`` that will be
        resolved first.  Returns the ``message_id``.
        """
        if group_id is None:
            if subdomain is None:
                raise ValueError("Either group_id or subdomain must be provided")
            group_id = await self._resolve(subdomain)

        params: dict[str, Any] = {"group_id": group_id, "text": text}
        if markdown:
            params["markdown"] = True

        result = await self.call("send_text", params)
        return result["message_id"]

    async def is_member(self, subdomain: str) -> bool:
        """Return True if the sidecar wallet is a member of the subdomain's group."""
        group_id = await self._resolve(subdomain)
        result = await self.call("get_conversation", {"group_id": group_id})
        return bool(result.get("member_of", False))

    async def stream_start(self, subdomain: str) -> str:
        """Resolve subdomain to group_id and start a stream. Returns ``stream_id``."""
        group_id = await self._resolve(subdomain)
        result = await self.call("stream_start", {"group_id": group_id})
        return result["stream_id"]

    async def stream_stop(self, stream_id: str) -> bool:
        """Stop a running stream. Returns True when stopped."""
        result = await self.call("stream_stop", {"stream_id": stream_id})
        return bool(result.get("stopped", False))

    def on_stream_event(
        self, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        """Register an async callback fired for each ``stream_event`` notification.

        The callback receives the ``params`` dict from the notification::

            {"stream_id": "s_abc", "group_id": "0x...", "message_id": "...",
             "sender_inbox_id": "...", "content": "text", "sent_at_ns": "..."}
        """
        self._stream_event_callbacks.append(callback)

    async def shutdown(self) -> None:
        """Gracefully shut down the sidecar.

        Sequence: RPC ``shutdown`` → SIGTERM → wait 5s → SIGKILL.
        """
        if self._proc is None:
            return

        # Best-effort RPC shutdown; ignore errors (proc may already be gone)
        try:
            await asyncio.wait_for(self.call("shutdown", {}, timeout=5.0), timeout=5.0)
        except Exception:
            pass

        proc = self._proc
        try:
            proc.terminate()
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=TERMINATION_GRACE_SEC)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        self._proc = None
        self._sidecar_ok = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def address(self) -> str | None:
        """The Ethereum address of the sidecar wallet (set after start())."""
        return self._address

    @property
    def sidecar_ok(self) -> bool:
        """True when the sidecar started successfully and hasn't crashed."""
        return self._sidecar_ok

    def stderr_tail(self) -> list[str]:
        """Return recent stderr lines (up to 200)."""
        return list(self._stderr_ring)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _maybe_build(self) -> None:
        """Build the sidecar if dist/index.js is missing."""
        entry = self._sidecar_dir / "dist" / "index.js"
        if entry.exists():
            return

        print(
            f"[sidecar] dist/index.js not found — building in {self._sidecar_dir}",
            file=sys.stderr,
        )
        subprocess.run(
            [self._npm_bin, "ci"],
            cwd=str(self._sidecar_dir),
            check=True,
        )
        subprocess.run(
            [self._npm_bin, "run", "build"],
            cwd=str(self._sidecar_dir),
            check=True,
        )

    async def _resolve(self, subdomain: str) -> str:
        """Resolve a fund subdomain to its XMTP group_id."""
        result = await self.call("resolve_subdomain", {"subdomain": subdomain})
        return result["group_id"]

    async def _read_stdout(self) -> None:
        """Background task: read stdout lines and demux RPC responses / notifications."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue

            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                _log.warning("sidecar: malformed JSON: %r", text[:200])
                continue

            # Notification (no id field) — dispatch to stream_event callbacks
            if "id" not in msg:
                if msg.get("method") == "stream_event":
                    await self._dispatch_stream_event(msg.get("params", {}))
                else:
                    _log.debug("sidecar: unhandled notification: %s", msg.get("method"))
                continue

            # Response — resolve pending future or buffer for call() to pick up
            req_id = msg.get("id")
            fut = self._pending.get(req_id)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            elif req_id is not None:
                # Future not registered yet — buffer so call() can find it
                self._response_buffer[req_id] = msg
            else:
                _log.debug("sidecar: response with no id: %r", msg)

    async def _read_stderr(self) -> None:
        """Background task: drain stderr into the ring buffer."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self._stderr_ring.append(line.decode("utf-8", "replace").rstrip())

    async def _dispatch_stream_event(self, params: dict) -> None:
        """Call all registered stream_event callbacks with the notification params."""
        for cb in self._stream_event_callbacks:
            try:
                await cb(params)
            except Exception:
                _log.exception("sidecar: stream_event callback raised")
