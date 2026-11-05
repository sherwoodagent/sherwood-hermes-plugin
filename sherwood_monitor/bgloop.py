"""A plugin-owned event loop for the fund streams and the XMTP sidecar.

Hermes runs a plugin's async tools and hooks on its own loops: on the CLI a
persistent loop that only runs while a tool call is being awaited, and in the
gateway a fresh per-call loop in a worker thread that is closed afterwards.
Background work started there (the `sherwood session check --stream` readers,
the sidecar's stdout reader) either stalls between turns or is cancelled when
the call returns. So the monitor keeps its long-lived work on one loop of its
own, running forever in a daemon thread, and Hermes-side callers hand
coroutines to it.

Lifetime: the Hermes process. `close()` runs at shutdown (the
`on_session_finalize` hook with reason "shutdown", and `atexit` as the
fallback); daemon threads die with the process either way.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

_log = logging.getLogger(__name__)

T = TypeVar("T")


class BackgroundLoop:
    def __init__(self, name: str = "sherwood-monitor-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def started(self) -> bool:
        """True once the loop thread exists (something was submitted)."""
        return self._loop is not None

    def loop(self) -> asyncio.AbstractEventLoop:
        """The running background loop (started on first use)."""
        with self._lock:
            if self._closed:
                raise RuntimeError("background loop is closed")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def _run() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                thread = threading.Thread(target=_run, name=self._name, daemon=True)
                thread.start()
                ready.wait(timeout=5)
                self._loop, self._thread = loop, thread
            return self._loop

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Schedule ``coro`` on the background loop; returns a thread-safe future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop())

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coro`` on the background loop and block for its result (not from that loop)."""
        return self.submit(coro).result(timeout)

    async def call(self, coro: Coroutine[Any, Any, T]) -> T:
        """Await ``coro`` on the background loop from any other event loop."""
        if self._loop is not None and asyncio.get_running_loop() is self._loop:
            return await coro
        return await asyncio.wrap_future(self.submit(coro))

    def in_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def close(self, timeout: float = 5.0) -> None:
        """Stop the loop and join its thread (idempotent, never raises)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            return
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
