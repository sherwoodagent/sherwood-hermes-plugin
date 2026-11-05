"""sherwood-monitor Hermes plugin entry point."""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import threading
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from .bgloop import BackgroundLoop
from .cli import register_cli
from .config import load_config
from .event_buffer import EventBuffer
from .fund import make_fund_guard, register_fund
from .fund.secrets import SHERWOOD_AGENT_ADDRESS, SHERWOOD_PRIVATE_KEY, get_secret
from .key_derivation import normalize_address, normalize_private_key
from .hooks import (
    make_post_tool_call_hook,
    make_pre_llm_call_hook,
    make_pre_tool_call_hook,
    make_session_finalize_hook,
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

TOOLSET = "sherwood-monitor"

# The last register() call's runtime (tests and debugging): the background loop,
# the supervisor, the sidecar (or None), and the teardown callable.
_RUNTIME: dict[str, Any] = {}


def _plugin_root() -> Path:
    # Computed lazily so tests can patch Path.home() before calling register().
    return Path.home() / ".hermes" / "plugins" / "sherwood-monitor"


def _sidecar_dir() -> Path:
    """Default sidecar location: alongside this package."""
    return Path(__file__).parent.parent / "xmtp_sidecar"


def _monitor_private_key() -> str | None:
    """The monitor wallet's key, from the declared secret ``SHERWOOD_PRIVATE_KEY``.

    Read through Hermes' secret scope only; the plugin never reads the Sherwood
    CLI's config file. Unset or malformed -> ``None`` and the XMTP sidecar is
    not started. The value is never logged.
    """
    raw = get_secret(SHERWOOD_PRIVATE_KEY)
    if not raw:
        _log.info("XMTP sidecar not started: %s is not set", SHERWOOD_PRIVATE_KEY)
        return None
    key = normalize_private_key(raw)
    if key is None:
        _log.warning("XMTP sidecar not started: %s is not a 32-byte hex key", SHERWOOD_PRIVATE_KEY)
    return key


def _log_start_failure(subdomain: str) -> Any:
    def _done(fut: Any) -> None:
        try:
            exc = fut.exception()
        except BaseException:  # cancelled at shutdown
            return
        if exc is not None:
            _log.warning("auto-start failed for %s: %s", subdomain, exc)

    return _done


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

    # The monitor wallet (declared secrets only; never the Sherwood CLI's config).
    private_key = _monitor_private_key()

    # Preflight: warn if CLI missing/misconfigured, but continue registering.
    # Also checks synchronously whether the sidecar is built; that warning is
    # shown only when XMTP is configured (SHERWOOD_PRIVATE_KEY set).
    pre = run_preflight(
        cfg.sherwood_bin, sidecar_dir=sidecar_dir, home=home, xmtp_configured=private_key is not None
    )
    for warn in pre.warnings:
        buffer.push(f"<sherwood-monitor-warning>\n{warn}\n</sherwood-monitor-warning>")

    # Upgrades from 0.6.0 are not prompted for new requires_env entries, and
    # 0.7.0 no longer reads the CLI's config: say once that the gas watchdog
    # (and XMTP) are off until the monitor wallet is set.
    if cfg.funds and private_key is None and normalize_address(get_secret(SHERWOOD_AGENT_ADDRESS)) is None:
        buffer.push(
            "<sherwood-monitor-warning>\n"
            f"The gas watchdog is off: set {SHERWOOD_AGENT_ADDRESS} (the monitor wallet's 0x address) "
            f"in the Hermes profile .env, or {SHERWOOD_PRIVATE_KEY} to also enable XMTP for funds whose "
            "XMTP group is in Base's ENS record. Since 0.7.0 the plugin does not read the Sherwood "
            "CLI's ~/.sherwood/config.json. Never paste a key into chat."
            "\n</sherwood-monitor-warning>"
        )

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
    # Filled when the sidecar branch below runs; ensure_streams() starts it lazily.
    sidecar_state_ref: list[dict[str, Any]] = []
    sidecar_start_ref: list[Any] = []

    if private_key is None:
        # XMTP is optional: one log line (in _monitor_private_key); no sidecar warning.
        pass
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

        # The sidecar runs on the plugin's own loop (bgloop): Hermes' loops do not
        # run between turns. It starts with the streams, at the first turn of
        # the process (register() also runs for every `hermes` CLI command, which
        # must not spawn node). The post_fn wrapper waits for it lazily.
        sidecar_state: dict[str, Any] = {"started": False, "failed": False, "future": None}

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
                lines.append(
                    "XMTP streaming works only for funds whose XMTP group id is in their ENS "
                    "record on Base. Robinhood Chain funds have no such record, so adding the "
                    "sidecar will not help there; their on-chain events still arrive."
                )
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

        sidecar_start_fn = _start_sidecar_and_wire
        sidecar_state_ref.append(sidecar_state)
        sidecar_start_ref.append(sidecar_start_fn)

        async def _async_post(subdomain: str, markdown: str) -> None:
            """Post summary via sidecar; waits for sidecar to be ready on first call."""
            future = sidecar_state["future"]
            if future is None:
                _log.debug("XMTP post skipped (sidecar not started) for %s", subdomain)
                return
            if not sidecar_state["started"] and not sidecar_state["failed"]:
                try:
                    await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=30.0)
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
    # A Hermes killed without stopping its streams leaves a status file listing
    # them; blank it so the stream watchdog does not cry "NO SUPERVISOR" forever.
    supervisor.clear_stale_status()

    # Streams and the sidecar live on the plugin's own loop for the life of this
    # Hermes process (Hermes fires on_session_end after every turn, so nothing
    # stops them there). They start at the first turn when auto_start is true,
    # or when the agent calls sherwood_monitor_start.
    bg = BackgroundLoop()
    lifecycle = {"streams_started": False, "closed": False}
    lifecycle_lock = threading.Lock()

    def ensure_streams() -> None:
        """Start the sidecar and the auto_start streams once per process (non-blocking)."""
        with lifecycle_lock:
            if lifecycle["streams_started"] or lifecycle["closed"]:
                return
            lifecycle["streams_started"] = True
        if sidecar is not None and sidecar_state_ref and sidecar_state_ref[0]["future"] is None:
            sidecar_state_ref[0]["future"] = bg.submit(sidecar_start_ref[0]())
        if cfg.auto_start:
            for sub in cfg.funds:
                bg.submit(supervisor.start(sub)).add_done_callback(_log_start_failure(sub))

    def teardown() -> None:
        """Stop every stream and the sidecar, then the loop (process exit; idempotent)."""
        with lifecycle_lock:
            if lifecycle["closed"]:
                return
            lifecycle["closed"] = True
        if bg.closed or not bg.started:
            # Nothing ever ran on the plugin loop (e.g. a short `hermes` CLI command).
            bg.close()
            return

        async def _stop() -> None:
            await supervisor.stop_all()
            if sidecar is not None:
                try:
                    await sidecar.shutdown()
                except Exception as exc:
                    _log.warning("sidecar shutdown error: %s", exc)

        try:
            bg.run(_stop(), timeout=15)
        except Exception as exc:  # loop wedged or already gone: kill the children directly
            _log.warning("monitor teardown fell back to SIGTERM: %s", type(exc).__name__)
            supervisor.kill_all_sync()
        bg.close()

    atexit.register(teardown)

    # Give the background task closure a reference to the final router
    if "router_holder" in dir():  # only defined when sidecar branch was taken
        try:
            router_holder.append(router)  # type: ignore[possibly-undefined]
        except NameError:
            pass

    # Hermes' register_tool needs a toolset, and only awaits handlers registered
    # with is_async=True; these five handlers are coroutines.
    tool_handlers = make_handlers(supervisor, cfg, runner=bg.call)
    for schema in (START, STOP, STATUS, EXPOSURE, CRON_TICK):
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=tool_handlers[schema["name"]],
            is_async=True,
        )

    session_hooks = make_session_hooks(
        cfg=cfg, buffer=buffer, supervisor=supervisor, ensure_streams=ensure_streams
    )
    ctx.register_hook("on_session_start", session_hooks["on_session_start"])
    # Teardown at Hermes shutdown only: on_session_finalize with reason
    # "shutdown" (CLI quit, gateway shutdown), atexit as the fallback. Not
    # on_session_end, which Hermes 0.22 fires at the end of every turn.
    ctx.register_hook("on_session_finalize", make_session_finalize_hook(teardown))

    # Group-chat fund (host + join modes): tools, slash commands, skills, and
    # the draft guard that rides on the pre_tool_call hook below. A failure
    # here must not take the monitor down with it.
    fund_guard = None
    fund_rt = None
    try:
        fund_rt = register_fund(ctx, cfg_path)
        fund_guard = make_fund_guard(fund_rt)
    except Exception as exc:
        _log.warning("fund features disabled: %s", exc)
        buffer.push(
            "<sherwood-monitor-warning>\n"
            f"Group-chat fund features failed to load: {exc}"
            "\n</sherwood-monitor-warning>"
        )

    # Telegram group cards: a lazy platform handler (Hermes calls its factory only
    # when its Telegram adapter connects, i.e. with a bot token), so a missing
    # token or a missing python-telegram-bot never fails plugin load. Its pitch
    # context folds into the single pre_llm_call hook below.
    context_sources: list[Any] = []
    if fund_rt is not None:
        try:
            from .fund.telegram import wire_fund_telegram

            bridge = wire_fund_telegram(ctx, fund_rt, push_context=buffer.push)
            if bridge is not None:
                context_sources.append(bridge.pre_llm_context)
        except Exception as exc:
            _log.warning("telegram fund cards disabled: %s", exc)

    state_fetcher = partial(default_state_fetcher, cfg.sherwood_bin)
    ctx.register_hook(
        "pre_tool_call",
        make_pre_tool_call_hook(state_fetcher=state_fetcher, fund_guard=fund_guard),
    )
    ctx.register_hook("post_tool_call", make_post_tool_call_hook(memory_writer=stderr_memory_writer, buffer=buffer))
    ctx.register_hook("pre_llm_call", make_pre_llm_call_hook(buffer, context_sources, before=ensure_streams))

    register_cli(ctx, supervisor, fund_rt)

    _RUNTIME.clear()
    _RUNTIME.update(
        {"bg": bg, "supervisor": supervisor, "sidecar": sidecar, "teardown": teardown, "buffer": buffer}
    )

    # register_skill takes the SKILL.md path, not the directory.
    skill_path = Path(__file__).parent.parent / "skills" / "sherwood-agent" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill("sherwood-agent", str(skill_path))
    else:
        _log.warning("skill pack missing at %s — skipping", skill_path)
