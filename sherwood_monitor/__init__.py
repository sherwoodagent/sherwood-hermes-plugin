"""sherwood-monitor Hermes plugin entry point."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from .cli import register_cli
from .config import load_config
from .event_buffer import EventBuffer
from .hooks import (
    make_post_tool_call_hook,
    make_pre_llm_call_hook,
    make_pre_tool_call_hook,
    make_session_hooks,
)
from .preflight import (
    check_memberships,
    check_sidecar_health,
    run_preflight,
)
from .router import EventRouter
from .schemas import CRON_TICK, EXPOSURE, START, STATUS, STOP
from .sidecar import Sidecar
from .state_fetcher import default_state_fetcher, stderr_memory_writer
from .supervisor import Supervisor
from .tools import make_handlers
from .xmtp_post import post_summary

_log = logging.getLogger(__name__)


def _plugin_root() -> Path:
    # Computed lazily so tests can patch Path.home() before calling register().
    return Path.home() / ".hermes" / "plugins" / "sherwood-monitor"


def _sidecar_dir() -> Path:
    """Default sidecar location: alongside this package."""
    return Path(__file__).parent.parent / "xmtp_sidecar"


def _read_sherwood_private_key(home: Path) -> str | None:
    """Read privateKey from ~/.sherwood/config.json. Returns None if missing."""
    config_path = home / ".sherwood" / "config.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text())
        key = data.get("privateKey") or data.get("private_key")
        if key and isinstance(key, str):
            return key
    except Exception as exc:
        _log.warning("failed to read sherwood config.json: %s", exc)
    return None


def _iso_from_ns(sent_at_ns: Any) -> str:
    """Convert nanosecond timestamp to ISO-8601 string."""
    try:
        ts_sec = int(sent_at_ns) / 1e9
        return datetime.fromtimestamp(ts_sec, tz=timezone.utc).isoformat()
    except Exception:
        return str(sent_at_ns)


def register(ctx: Any) -> None:
    """Entry point Hermes calls on plugin load."""
    home = Path.home()
    cfg_path = _plugin_root() / "config.yaml"
    cfg = load_config(cfg_path)

    sidecar_dir = _sidecar_dir()

    buffer = EventBuffer()

    # Preflight: warn if CLI missing/misconfigured, but continue registering.
    # Also checks synchronously whether the sidecar is built.
    pre = run_preflight(cfg.sherwood_bin, sidecar_dir=sidecar_dir, home=home)
    for warn in pre.warnings:
        buffer.push(f"<sherwood-monitor-warning>\n{warn}\n</sherwood-monitor-warning>")

    # ------------------------------------------------------------------
    # Sidecar setup — all three paths converge on effective_post_fn
    # ------------------------------------------------------------------
    sidecar: Sidecar | None = None
    # Map group_id -> subdomain for stream event routing (populated by background task)
    group_to_subdomain: dict[str, str] = {}

    # Default: no-op post function used when sidecar is unavailable
    async def _noop_post(subdomain: str, markdown: str) -> None:
        _log.debug("XMTP post skipped (no sidecar configured) for %s", subdomain)

    effective_post_fn = _noop_post

    private_key = _read_sherwood_private_key(home)
    if private_key is None:
        buffer.push(
            "<sherwood-monitor-warning>\n"
            "XMTP disabled: ~/.sherwood/config.json not found. "
            "Run `sherwood config set` to enable."
            "\n</sherwood-monitor-warning>"
        )
    elif not pre.sidecar_built:
        # Warning already injected by run_preflight; skip spawn, keep _noop_post
        pass
    else:
        db_path = home / ".hermes" / "xmtp" / "sidecar.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        sidecar = Sidecar(
            sidecar_dir=sidecar_dir,
            primary_private_key_hex=private_key,
            db_path=db_path,
        )

        # Kick off sidecar.start() as a background task. register() is sync so
        # we get the current running loop (Hermes runs inside an async runtime)
        # and schedule the start task on it. The post_fn wrapper awaits the task
        # lazily so the first XMTP post blocks until the sidecar is ready.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        sidecar_state: dict[str, Any] = {"started": False, "failed": False}

        # router reference captured by closure below; defined after sidecar wiring
        # We use a holder so the closure sees the final router object.
        router_holder: list[EventRouter] = []

        async def _start_sidecar_and_wire() -> None:
            """Start the sidecar, run async preflight, wire streams."""
            try:
                await sidecar.start()  # type: ignore[union-attr]
                sidecar_state["started"] = True
            except Exception as exc:
                _log.warning("sidecar start failed: %s", exc)
                sidecar_state["failed"] = True
                buffer.push(
                    "<sherwood-monitor-warning>\n"
                    f"XMTP sidecar failed to start: {exc}. "
                    "XMTP streams are disabled for this session."
                    "\n</sherwood-monitor-warning>"
                )
                return

            # Async health check
            health_ok, sidecar_address = await check_sidecar_health(sidecar)  # type: ignore[arg-type]
            if not health_ok:
                sidecar_state["failed"] = True
                buffer.push(
                    "<sherwood-monitor-warning>\n"
                    "XMTP sidecar health check failed — ping did not respond."
                    "\n</sherwood-monitor-warning>"
                )
                return

            # Membership check
            missing = await check_memberships(sidecar, cfg.funds)  # type: ignore[arg-type]

            if sidecar_address and missing:
                lines = [
                    "sherwood-monitor sidecar wallet " + sidecar_address,
                    "needs to be added to these funds' XMTP groups:",
                ]
                for sub in missing:
                    lines.append(f"  sherwood chat {sub} add {sidecar_address}")
                lines.append("")
                lines.append("Run these as the fund creator.")
                buffer.push(
                    "<sherwood-monitor-warning>\n"
                    + "\n".join(lines)
                    + "\n</sherwood-monitor-warning>"
                )

            # Wire stream_event callback into the EventRouter
            async def _on_stream_event(params: dict) -> None:
                raw: dict[str, Any] = {
                    "source": "xmtp",
                    "id": params.get("message_id", ""),
                    "from": params.get("sender_inbox_id", ""),
                    "type": "MESSAGE",
                    "text": params.get("content", ""),
                    "sentAt": _iso_from_ns(params.get("sent_at_ns", 0)),
                }
                # Try to parse as ChatEnvelope and upgrade type/text/from
                try:
                    env = json.loads(params.get("content", ""))
                    if isinstance(env, dict) and "type" in env:
                        raw["type"] = env["type"]
                        raw["text"] = env.get("text", raw["text"])
                        raw["from"] = env.get("from", raw["from"])
                except (json.JSONDecodeError, ValueError):
                    pass

                sub = group_to_subdomain.get(params.get("group_id", ""))
                if sub is not None and router_holder:
                    await router_holder[0].route(sub, raw)

            sidecar.on_stream_event(_on_stream_event)  # type: ignore[union-attr]

            # Start streams for funds where the sidecar IS a member
            member_funds = [s for s in cfg.funds if s not in missing]
            for sub in member_funds:
                try:
                    # Resolve first so we can capture group_id for the reverse map.
                    group_id_result = await sidecar.call(  # type: ignore[union-attr]
                        "resolve_subdomain", {"subdomain": sub}
                    )
                    group_id = group_id_result["group_id"]
                    group_to_subdomain[group_id] = sub
                    await sidecar.stream_start(sub)  # type: ignore[union-attr]
                    _log.info("XMTP stream started for fund %s", sub)
                except Exception as exc:
                    _log.warning("stream_start failed for %s: %s", sub, exc)

        sidecar_start_task = loop.create_task(_start_sidecar_and_wire())

        async def _async_post(subdomain: str, markdown: str) -> None:
            """Post summary via sidecar; waits for sidecar to be ready on first call."""
            if not sidecar_state["started"] and not sidecar_state["failed"]:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(sidecar_start_task), timeout=30.0
                    )
                except (asyncio.TimeoutError, Exception) as exc:
                    _log.warning("waiting for sidecar start timed out: %s", exc)
            if sidecar_state["failed"] or sidecar is None:
                _log.debug("XMTP post skipped (sidecar not available) for %s", subdomain)
                return
            await post_summary(sidecar, subdomain, markdown)

        effective_post_fn = _async_post

    # ------------------------------------------------------------------
    # Core plugin wiring
    # ------------------------------------------------------------------
    router = EventRouter(buffer=buffer, cfg=cfg, post_fn=effective_post_fn)
    supervisor = Supervisor(cfg=cfg, router=router)

    # Give the background task closure a reference to the final router
    if "router_holder" in dir():  # only defined when sidecar branch was taken
        try:
            router_holder.append(router)  # type: ignore[possibly-undefined]
        except NameError:
            pass

    tool_handlers = make_handlers(supervisor, cfg)
    ctx.register_tool(name=START["name"], schema=START, handler=tool_handlers["sherwood_monitor_start"])
    ctx.register_tool(name=STOP["name"], schema=STOP, handler=tool_handlers["sherwood_monitor_stop"])
    ctx.register_tool(name=STATUS["name"], schema=STATUS, handler=tool_handlers["sherwood_monitor_status"])
    ctx.register_tool(name=EXPOSURE["name"], schema=EXPOSURE, handler=tool_handlers["sherwood_monitor_exposure"])
    ctx.register_tool(name=CRON_TICK["name"], schema=CRON_TICK, handler=tool_handlers["sherwood_monitor_cron_tick"])

    session_hooks = make_session_hooks(cfg=cfg, buffer=buffer, supervisor=supervisor)
    ctx.register_hook("on_session_start", session_hooks["on_session_start"])

    # Teardown: stop supervisor AND sidecar
    _captured_sidecar = sidecar

    async def _on_session_end() -> None:
        await supervisor.stop_all()
        if _captured_sidecar is not None:
            try:
                await _captured_sidecar.shutdown()
            except Exception as exc:
                _log.warning("sidecar shutdown error: %s", exc)

    ctx.register_hook("on_session_end", _on_session_end)

    state_fetcher = partial(default_state_fetcher, cfg.sherwood_bin)
    ctx.register_hook("pre_tool_call", make_pre_tool_call_hook(state_fetcher=state_fetcher))
    ctx.register_hook("post_tool_call", make_post_tool_call_hook(memory_writer=stderr_memory_writer, buffer=buffer))
    ctx.register_hook("pre_llm_call", make_pre_llm_call_hook(buffer))

    register_cli(ctx, supervisor)

    skill_path = Path(__file__).parent.parent / "skills" / "sherwood-agent"
    if skill_path.exists():
        ctx.register_skill("sherwood-agent", str(skill_path))
    else:
        _log.warning("skill pack missing at %s — skipping", skill_path)
