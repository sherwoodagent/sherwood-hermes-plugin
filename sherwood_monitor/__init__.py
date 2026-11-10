"""sherwood-monitor Hermes plugin entry point."""
from __future__ import annotations

import atexit
import logging
import threading
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
from .preflight import run_preflight
from .router import EventRouter
from .schemas import CRON_TICK, EXPOSURE, START, STATUS, STOP
from .state_fetcher import default_state_fetcher, stderr_memory_writer
from .supervisor import Supervisor
from .tools import make_handlers

_log = logging.getLogger(__name__)

TOOLSET = "sherwood-monitor"

# The last register() call's runtime (tests and debugging): the background loop,
# the supervisor, and the teardown callable.
_RUNTIME: dict[str, Any] = {}


def _plugin_root() -> Path:
    # Computed lazily so tests can patch Path.home() before calling register().
    return Path.home() / ".hermes" / "plugins" / "sherwood-monitor"


def _monitor_wallet_set() -> bool:
    """True when the declared secrets name a usable monitor wallet: a 0x address in
    ``SHERWOOD_AGENT_ADDRESS`` or a 32-byte hex key in ``SHERWOOD_PRIVATE_KEY`` (the gas
    watchdog derives the address from it). Read through Hermes' secret scope only; the plugin
    never reads the Sherwood CLI's config file. Values are never logged."""
    if normalize_private_key(get_secret(SHERWOOD_PRIVATE_KEY)) is not None:
        return True
    return normalize_address(get_secret(SHERWOOD_AGENT_ADDRESS)) is not None


def _log_start_failure(subdomain: str) -> Any:
    def _done(fut: Any) -> None:
        try:
            exc = fut.exception()
        except BaseException:  # cancelled at shutdown
            return
        if exc is not None:
            _log.warning("auto-start failed for %s: %s", subdomain, exc)

    return _done


def register(ctx: Any) -> None:
    """Entry point Hermes calls on plugin load."""
    home = Path.home()
    cfg_path = _plugin_root() / "config.yaml"
    cfg = load_config(cfg_path)

    buffer = EventBuffer()

    # Preflight: warn if CLI missing/misconfigured, but continue registering.
    pre = run_preflight(cfg.sherwood_bin, home=home)
    for warn in pre.warnings:
        buffer.push(f"<sherwood-monitor-warning>\n{warn}\n</sherwood-monitor-warning>")

    # Upgrades from 0.6.0 are not prompted for new requires_env entries, and
    # 0.7.0 no longer reads the CLI's config: say once that the gas watchdog
    # is off until the monitor wallet is set (declared secrets only).
    if cfg.funds and not _monitor_wallet_set():
        buffer.push(
            "<sherwood-monitor-warning>\n"
            f"The gas watchdog is off: set {SHERWOOD_AGENT_ADDRESS} (the monitor wallet's 0x address) "
            f"in the Hermes profile .env, or {SHERWOOD_PRIVATE_KEY} (its address is derived from it). "
            "Since 0.7.0 the plugin does not read the Sherwood CLI's ~/.sherwood/config.json. "
            "Never paste a key into chat."
            "\n</sherwood-monitor-warning>"
        )

    # ------------------------------------------------------------------
    # Core plugin wiring
    # ------------------------------------------------------------------
    router = EventRouter(buffer=buffer, cfg=cfg)
    supervisor = Supervisor(cfg=cfg, router=router)
    # A Hermes killed without stopping its streams leaves a status file listing
    # them; blank it so the stream watchdog does not cry "NO SUPERVISOR" forever.
    supervisor.clear_stale_status()

    # Streams live on the plugin's own loop for the life of this
    # Hermes process (Hermes fires on_session_end after every turn, so nothing
    # stops them there). They start at the first turn when auto_start is true,
    # or when the agent calls sherwood_monitor_start.
    bg = BackgroundLoop()
    lifecycle = {"streams_started": False, "closed": False}
    lifecycle_lock = threading.Lock()

    def ensure_streams() -> None:
        """Start the auto_start streams once per process (non-blocking)."""
        with lifecycle_lock:
            if lifecycle["streams_started"] or lifecycle["closed"]:
                return
            lifecycle["streams_started"] = True
        if cfg.auto_start:
            for sub in cfg.funds:
                bg.submit(supervisor.start(sub)).add_done_callback(_log_start_failure(sub))

    def teardown() -> None:
        """Stop every stream, then the loop (process exit; idempotent)."""
        with lifecycle_lock:
            if lifecycle["closed"]:
                return
            lifecycle["closed"] = True
        if bg.closed or not bg.started:
            # Nothing ever ran on the plugin loop (e.g. a short `hermes` CLI command).
            bg.close()
            return

        try:
            bg.run(supervisor.stop_all(), timeout=15)
        except Exception as exc:  # loop wedged or already gone: kill the children directly
            _log.warning("monitor teardown fell back to SIGTERM: %s", type(exc).__name__)
            supervisor.kill_all_sync()
        bg.close()

    atexit.register(teardown)

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
        fund_rt = register_fund(ctx, cfg_path, sherwood_bin=cfg.sherwood_bin)
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
            from .fund.scope import make_fund_scope_source

            context_sources.append(make_fund_scope_source(fund_rt))
        except Exception as exc:
            _log.warning("fund scope disabled: %s", exc)
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
        {"bg": bg, "supervisor": supervisor, "teardown": teardown, "buffer": buffer}
    )

    # register_skill takes the SKILL.md path, not the directory.
    skill_path = Path(__file__).parent.parent / "skills" / "sherwood-agent" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill("sherwood-agent", str(skill_path))
    else:
        _log.warning("skill pack missing at %s — skipping", skill_path)
