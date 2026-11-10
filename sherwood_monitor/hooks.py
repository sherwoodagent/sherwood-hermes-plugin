"""Hermes lifecycle hooks."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from typing import Any, Awaitable, Callable

from .config import Config
from .childenv import child_env
from .event_buffer import EventBuffer
from .memory import MemoryWriter, build_record
from .propose_cmd import FUND_RULES_APPLIED, TERMINAL_TOOLS, is_address, parse_propose_invocations
from .risk import ProposeParams, evaluate_propose
from .supervisor import Supervisor

_log = logging.getLogger(__name__)


async def _catchup_one(sherwood_bin: str, subdomain: str) -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            sherwood_bin,
            "session",
            "check",
            subdomain,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        stdout, _stderr = await proc.communicate()
        rc = await proc.wait()
        if rc != 0:
            _log.warning("catch-up for %s exited rc=%s", subdomain, rc)
            return None
        return json.loads(stdout.decode("utf-8", "replace") or "{}")
    except Exception as exc:
        _log.warning("catch-up for %s failed: %s", subdomain, exc)
        return None


def _format_catchup_injection(subdomain: str, payload: dict) -> str:
    meta = payload.get("meta", {})
    new_msgs = meta.get("newMessages", 0)
    new_events = meta.get("newEvents", 0)
    return (
        f'<sherwood-catchup fund="{subdomain}">\n'
        f"{new_msgs} new messages, {new_events} new events since last check.\n"
        f"{json.dumps(payload, indent=2)}\n"
        f"</sherwood-catchup>"
    )


def make_session_hooks(
    cfg: Config,
    buffer: EventBuffer,
    supervisor: Supervisor,
    ensure_streams: Callable[[], None] | None = None,
) -> dict[str, Callable[..., Awaitable[None]]]:
    """`on_session_start`: catch-up for every fund, then make sure the streams run.

    `ensure_streams` (from register()) starts the auto_start streams on the
    plugin's own loop once per process; without it (unit tests) auto_start
    awaits `supervisor.start` directly.
    """

    # Hermes passes hook payloads as keyword arguments; every callback takes **kwargs.
    async def on_session_start(**_: Any) -> None:
        for sub in cfg.funds:
            payload = await _catchup_one(cfg.sherwood_bin, sub)
            if payload is not None:
                buffer.push(_format_catchup_injection(sub, payload))
        if ensure_streams is not None:
            ensure_streams()
            return
        if cfg.auto_start:
            for sub in cfg.funds:
                try:
                    await supervisor.start(sub)
                except Exception as exc:
                    _log.warning("auto-start failed for %s: %s", sub, exc)

    return {"on_session_start": on_session_start}


SHUTDOWN_REASON = "shutdown"


def make_session_finalize_hook(teardown: Callable[[], None]) -> Callable[..., None]:
    """`on_session_finalize` -> teardown, only when Hermes is shutting down.

    Hermes 0.22 fires `on_session_end` at every turn's end and
    `on_session_finalize` on /new as well (reason "session_boundary", or the
    gateway's reset); only CLI quit and gateway shutdown pass
    reason="shutdown". The streams belong to the Hermes process, so nothing
    else stops them.
    """

    def on_session_finalize(**kwargs: Any) -> None:
        if kwargs.get("reason") == SHUTDOWN_REASON:
            teardown()

    return on_session_finalize


ContextSource = Callable[..., Any]  # (**hook kwargs) -> {"context": str} | None (sync or async)


def make_pre_llm_call_hook(
    buffer: EventBuffer,
    extra_sources: list[ContextSource] | tuple[ContextSource, ...] = (),
    before: Callable[[], None] | None = None,
) -> Callable[..., Awaitable[dict | None]]:
    """The plugin's ONE pre_llm_call hook.

    It drains the event buffer (everything the streams routed since the last
    turn: chain events, monitor warnings, settlements, fund draft requests)
    and folds in every `extra_sources` callback (the Telegram pitch context),
    so the plugin never registers a second pre_llm_call hook. A failing source
    is logged and skipped. `before` runs first (register() uses it to start the
    auto_start streams on the first turn of the process).
    """

    async def hook(**kwargs: Any) -> dict | None:
        if before is not None:
            try:
                before()
            except Exception as exc:
                _log.warning("pre_llm_call setup failed: %s", exc)
        blocks = buffer.drain()
        for source in extra_sources:
            try:
                out = source(**kwargs)
                if inspect.isawaitable(out):
                    out = await out
            except Exception as exc:
                _log.warning("pre_llm_call context source %r failed: %s", source, exc)
                continue
            text = out.get("context") if isinstance(out, dict) else None
            if text:
                blocks.append(str(text))
        if not blocks:
            return None
        return {"context": "\n\n".join(blocks)}

    return hook


_TERMINAL_TOOLS = TERMINAL_TOOLS

StateFetcher = Callable[[str], Awaitable[dict]]


def _parse_propose_command(command: str) -> tuple[str, float, str] | None:
    """Return (fund, size, protocol) for the first propose in `command`, or None if there is none.

    The fund is the positional argument, except when the command names its vault
    with `--vault <0x address>` (or `--vault=<address>`): then the positional is a
    strategy template (`sherwood strategy propose portfolio --vault 0x..`), not a
    fund, and the vault address is what the state fetcher reads. The size is
    `--size-usd`, else the capital flag the CLI really takes (`--amount`, or
    `--max-capital` on `proposal create`), in the vault asset's units.
    """
    invocations = [inv for inv in parse_propose_invocations(command) or [] if not inv.help]  # --help proposes nothing
    if not invocations:
        return None
    inv = invocations[0]
    subdomain = inv.vault if is_address(inv.vault) else (inv.positional or inv.vault)
    size = 0.0
    for raw in ([inv.size_usd] if inv.size_usd is not None else []) + [v for _, v in inv.capital]:
        try:
            size = float(raw)
            break
        except (TypeError, ValueError):
            continue
    return subdomain, size, inv.protocol


# A guard returns a block reason, a ready Hermes directive (e.g. the approval
# shape {"action": "approve", ...}), or None to allow
# (sherwood_monitor.fund.guard.make_fund_guard).
ToolGuard = Callable[[str, dict], "str | dict | None"]


def block_directive(reason: str) -> dict:
    """Hermes vetoes a tool call on `{"action": "block", "message": ...}`.

    `blocked` / `reason` are kept for callers that read the plugin's older shape.
    """
    return {"action": "block", "message": reason, "blocked": True, "reason": reason}


def make_pre_tool_call_hook(state_fetcher: StateFetcher, fund_guard: ToolGuard | None = None):
    async def hook(
        tool_name: str = "",
        args: dict | None = None,
        params: dict | None = None,
        **_: Any,
    ):
        # Hermes passes the tool arguments as `args`; `params` is the old name.
        payload = args if isinstance(args, dict) else (params or {})
        if fund_guard is not None:
            try:
                reason = fund_guard(tool_name, payload)
            except Exception as exc:  # the guard must never crash the turn
                _log.warning("fund guard failed for %s: %s", tool_name, exc)
                from .fund.store import scrub_paths

                reason = f"fund guard error: {scrub_paths(exc)}"
            if reason is FUND_RULES_APPLIED:
                return None  # a fund vault's propose, inside the fund's own ceilings: no general rule on top
            if isinstance(reason, dict):
                return reason
            if reason:
                return block_directive(reason)
        if tool_name not in _TERMINAL_TOOLS:
            return None
        command = payload.get("command", "")
        parsed = _parse_propose_command(command)
        if parsed is None:
            return None
        subdomain, size_usd, protocol = parsed

        try:
            state = await state_fetcher(subdomain)
        except Exception as exc:
            _log.warning("state fetch failed for %s: %s — allowing", subdomain, exc)
            return None

        # Fail-open contract: state_fetcher returns None when vault state is
        # unknown (CLI 0.103.0's `sherwood vault info` has no machine-readable
        # output). Do NOT synthesize zero-defaults — that would make every
        # proposal trip the AUM==0 guard and fail-closed. This general rule is
        # advisory; a fund vault's own ceilings are enforced by the fund guard.
        if state is None:
            _log.warning(
                "risk check for %s skipped: vault state unavailable (the installed Sherwood CLI gives no "
                "machine-readable vault state) — allowing; the general sizing rule is advisory and fails open",
                subdomain,
            )
            return None

        verdict = evaluate_propose(
            ProposeParams(
                subdomain=subdomain,
                proposed_size_usd=size_usd,
                current_exposure_usd=float(state.get("current_exposure_usd", 0)),
                vault_aum_usd=float(state.get("vault_aum_usd", 0)),
                protocol=protocol,
                allowed_protocols=list(state.get("allowed_protocols", [])),
            )
        )
        if not verdict.ok:
            return block_directive(verdict.reason)
        return None

    return hook


_SHERWOOD_SETTLE_RE = re.compile(
    r"\bsherwood\s+proposal\s+(execute|settle)\s+(\S+)"
)


# Hermes registers the bundled pack as "<plugin>:<skill>"; remember-settlement is a file inside it,
# read with skill_view(name, file_path) (tools/skills_tool_plugin.py serves any file in the skill dir).
SETTLEMENT_SKILL = "sherwood-monitor:sherwood-agent"
SETTLEMENT_SKILL_FILE = "skills/remember-settlement/SKILL.md"


def _format_settlement_block(record: dict) -> str:
    return (
        f'<sherwood-settlement fund="{record["fund"]}" '
        f'action="{record["action"]}" '
        f'proposal_id="{record.get("proposal_id", "?")}" '
        f'pnl_usd="{record.get("pnl_usd", "n/a")}" '
        f'tx="{record.get("tx_hash", "?")}">\n'
        f"REMEMBER THIS: persist it to memory as the remember-settlement steps in the "
        f'{SETTLEMENT_SKILL} skill say (skill_view name="{SETTLEMENT_SKILL}", '
        f'file_path="{SETTLEMENT_SKILL_FILE}").\n'
        f"</sherwood-settlement>"
    )


def make_post_tool_call_hook(memory_writer: MemoryWriter, buffer: EventBuffer):
    async def hook(
        tool_name: str = "",
        args: dict | None = None,
        params: dict | None = None,
        result: Any = None,
        **_: Any,
    ):
        if tool_name not in _TERMINAL_TOOLS:
            return None
        payload = args if isinstance(args, dict) else (params or {})
        command = payload.get("command", "")
        m = _SHERWOOD_SETTLE_RE.search(command)
        if not m:
            return None
        action = m.group(1)
        subdomain = m.group(2)
        result_str = result if isinstance(result, str) else json.dumps(result)
        record = build_record(
            subdomain=subdomain,
            action=action,
            command=command,
            result_json=result_str,
        )
        try:
            memory_writer(record)
        except Exception as exc:
            _log.warning(
                "settlement memory write failed for %s #%s: %s",
                subdomain,
                record.get("proposal_id"),
                exc,
            )
        try:
            buffer.push(_format_settlement_block(record))
        except Exception as exc:
            _log.warning(
                "settlement buffer push failed for %s #%s: %s — "
                "agent will not see the REMEMBER THIS marker on next turn",
                subdomain,
                record.get("proposal_id"),
                exc,
            )
        return None

    return hook
