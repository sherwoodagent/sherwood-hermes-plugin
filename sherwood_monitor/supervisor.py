"""Supervisor: manages one `sherwood session check --stream` subprocess per fund."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .childenv import child_env
from .router import EventRouter

_log = logging.getLogger(__name__)

_STDERR_RING_SIZE = 200
TERMINATION_GRACE_SEC = 5.0
# The stream watchdog (a separate cron process) reads this file; it cannot see
# the in-memory supervisor. Written on start, exit and stop, and at most every
# STATUS_WRITE_MIN_INTERVAL_S on events.
STATUS_WRITE_MIN_INTERVAL_S = 30.0


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def kill_quietly(pid: Any) -> None:
    """SIGTERM a child we spawned, if it is still there (sync; for process-exit teardown)."""
    if not _pid_alive(pid):
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def default_status_path() -> Path:
    """``~/.hermes/plugins/sherwood-monitor/stream_status.json`` (resolved at call time)."""
    return Path.home() / ".hermes" / "plugins" / "sherwood-monitor" / "stream_status.json"

# Capture the real asyncio.sleep at import time so monkeypatches in tests
# (which target sherwood_monitor.supervisor.asyncio.sleep) don't break the
# pid-poll loop inside start().
_real_sleep = asyncio.sleep


@dataclass
class _State:
    subdomain: str
    proc: Any = None
    started_at: float = 0.0
    stop_requested: bool = False
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_STDERR_RING_SIZE))
    task: asyncio.Task | None = None
    exit_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Fires each time _run_once completes (reset for next run); used by wait_until_exit.
    run_done_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Between a subprocess exit and its restart (backoff): not a dead stream.
    restarting: bool = False


class Supervisor:
    def __init__(self, cfg: Config, router: EventRouter, status_path: Path | None = None) -> None:
        self._cfg = cfg
        self._router = router
        self._states: dict[str, _State] = {}
        self._status_path = status_path
        self._last_status_write = 0.0

    async def start(self, subdomain: str) -> int:
        """Spawn supervisor task for `subdomain`. Returns subprocess PID once available."""
        if subdomain in self._states:
            s = self._states[subdomain]
            if s.task and not s.task.done():
                return s.proc.pid if s.proc else 0

        state = _State(subdomain=subdomain)
        self._states[subdomain] = state
        state.task = asyncio.create_task(self._supervise(state), name=f"sherwood-{subdomain}")

        # Yield to let the task start and assign state.proc; avoid module-level asyncio.sleep
        # so tests that monkeypatch asyncio.sleep don't break this poll.
        for _ in range(50):
            if state.proc is not None:
                return state.proc.pid
            await _real_sleep(0.01)
        return 0

    async def stop(self, subdomain: str) -> None:
        state = self._states.get(subdomain)
        if not state:
            return
        state.stop_requested = True
        proc = state.proc
        if proc is not None:
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
        if state.task is not None and not state.task.done():
            state.task.cancel()
            try:
                await state.task
            except (asyncio.CancelledError, Exception):
                pass
        self.write_status(force=True)

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(self.stop(sub) for sub in list(self._states.keys())),
            return_exceptions=True,
        )

    def kill_all_sync(self) -> None:
        """Last-resort teardown without an event loop: SIGTERM every stream child."""
        for state in list(self._states.values()):
            state.stop_requested = True
            if state.proc is not None:
                kill_quietly(getattr(state.proc, "pid", None))
        self.write_status(force=True)

    async def wait_until_exit(self, subdomain: str) -> None:
        """Wait until the current subprocess run finishes (fires after each _run_once)."""
        state = self._states.get(subdomain)
        if state is None:
            return
        await state.run_done_event.wait()

    def status(self) -> dict:
        out = []
        now = time.time()
        for sub, state in self._states.items():
            pid = state.proc.pid if state.proc is not None else 0
            uptime = int(now - state.started_at) if state.started_at else 0
            out.append(
                {
                    "subdomain": sub,
                    "pid": pid,
                    "uptime_seconds": uptime,
                    "events_seen": self._router.events_seen(sub),
                    "last_event_at": self._router.last_event_at(sub),
                    "stderr_tail": list(state.stderr_tail)[-10:],
                }
            )
        return {"funds": out}

    def clear_stale_status(self) -> bool:
        """At boot: blank a status file left by a Hermes that died without stopping
        its streams (kill -9, OOM, reboot), so the stream watchdog does not report
        "Hermes is not running" while this one is. A file written by a live
        process (another Hermes on this machine) is left alone. Returns True when
        it cleared the file."""
        path = self._status_path or default_status_path()
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        host_pid = data.get("host_pid") if isinstance(data, dict) else None
        funds = data.get("funds") if isinstance(data, dict) else None
        if not funds or host_pid == os.getpid() or _pid_alive(host_pid):
            return False
        self.write_status(force=True)
        return True

    def write_status(self, *, force: bool = False) -> None:
        """Persist the live funds' stream status for the stream watchdog (atomic, best effort)."""
        now = time.time()
        if not force and now - self._last_status_write < STATUS_WRITE_MIN_INTERVAL_S:
            return
        self._last_status_write = now
        funds = []
        for item in self.status()["funds"]:
            state = self._states.get(item["subdomain"])
            if state is None or state.stop_requested:
                continue
            row = {k: v for k, v in item.items() if k != "stderr_tail"}
            row["restarting"] = state.restarting
            funds.append(row)
        payload = {"written_at": int(now), "host_pid": os.getpid(), "funds": funds}
        try:
            path = self._status_path or default_status_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".stream_status.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(payload, f)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:  # best effort: a status write must never stop a stream
            _log.debug("stream status not written: %s", type(exc).__name__)

    def stderr_tail(self, subdomain: str) -> list[str]:
        s = self._states.get(subdomain)
        return list(s.stderr_tail) if s else []

    async def _supervise(self, state: _State) -> None:
        backoff = 1
        try:
            while not state.stop_requested:
                run_start = time.time()
                try:
                    await self._run_once(state)
                except Exception as exc:
                    _log.exception("subprocess run failed on %s: %s", state.subdomain, exc)
                finally:
                    # Signal that this run iteration completed; reset for the next.
                    state.run_done_event.set()
                    state.run_done_event = asyncio.Event()

                if state.stop_requested:
                    break

                if time.time() - run_start > 60:
                    backoff = 1  # stable run, reset

                state.restarting = True
                self.write_status(force=True)

                _log.info(
                    "sherwood session for %s exited; restarting in %ds",
                    state.subdomain,
                    backoff,
                )
                await asyncio.sleep(backoff)
                # Always yield one real tick so the event loop stays responsive
                # even when asyncio.sleep is monkeypatched in tests.
                await _real_sleep(0)
                backoff = min(self._cfg.backoff_max_seconds, backoff * 2)
        finally:
            state.exit_event.set()

    async def _run_once(self, state: _State) -> None:
        # --no-xmtp: the monitor streams on-chain events only, so the CLI
        # skips its XMTP catch-up. On glibc-2.36 hosts, where the CLI's
        # @xmtp/node-bindings fails to load, that path would also block the
        # subprocess before any on-chain events emerge. The `--no-xmtp` flag
        # landed in CLI 0.40.5; the effective floor is MIN_CLI_VERSION in
        # preflight (enforced on load).
        proc = await asyncio.create_subprocess_exec(
            self._cfg.sherwood_bin,
            "session",
            "check",
            state.subdomain,
            "--stream",
            "--no-xmtp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        state.proc = proc
        state.started_at = time.time()
        state.restarting = False
        self.write_status(force=True)

        stdout_task = asyncio.create_task(self._read_stdout(state))
        stderr_task = asyncio.create_task(self._read_stderr(state))

        # Wait for the process to exit, then drain both stream readers naturally
        # (they exit when readline() returns b"" on EOF).  The gather with a
        # timeout lets stderr flush before we bail, which fixes a race where
        # proc.wait() returns immediately (e.g. in tests) and the tasks haven't
        # had a chance to run yet.
        await proc.wait()
        try:
            await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=2)
        except asyncio.TimeoutError:
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    async def _read_stdout(self, state: _State) -> None:
        proc = state.proc
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
                raw = json.loads(text)
            except json.JSONDecodeError:
                _log.warning("malformed JSON on %s: %r", state.subdomain, text[:200])
                continue
            await self._router.route(state.subdomain, raw)
            self.write_status()

    async def _read_stderr(self, state: _State) -> None:
        proc = state.proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            state.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
