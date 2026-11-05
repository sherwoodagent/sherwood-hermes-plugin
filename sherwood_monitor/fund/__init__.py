"""Group-chat fund: host + join (bring-your-own-Hermes) modes.

Public surface for other lanes:

- `service.FundService`: every fund action (members, agents, claims, drafts,
  votes, settles) as one atomic state update. Rule failures raise
  `rules.RuleViolation`.
- `store.FundStateBackend` / `LocalJsonBackend` / `HttpFundStateBackend`.
- `rules`: the coordination rules as pure functions.
- `txbuilder.TxBuilderClient`: unsigned txs from the hosted prepare API.
- `cards.render_fund_card`: the status card text + buttons.
- `register_fund(ctx, cfg_path)`: called from the plugin's `register()`.

`telegram.py` in this package belongs to the Telegram lane.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .commands import COMMANDS, make_fund_commands
from .guard import make_fund_guard
from .runtime import FundRuntime, build_runtime
from .settings import load_fund_settings
from .tools import SCHEMAS, TOOLSET, make_fund_tool_handlers

_log = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills" / "fund"
FUND_SKILLS: dict[str, str] = {"fund-pitch": "pitch", "fund-propose": "propose", "fund-settle": "settle"}

__all__ = ["FUND_SKILLS", "FundRuntime", "build_runtime", "make_fund_guard", "register_fund"]


def register_fund(ctx: Any, cfg_path: Path | None) -> FundRuntime:
    """Register fund tools, slash commands and skills. Returns the runtime (for the guard)."""
    rt = build_runtime(load_fund_settings(cfg_path, ctx))

    handlers = make_fund_tool_handlers(rt)
    for name, schema in SCHEMAS.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handlers[name], is_async=False)

    commands = make_fund_commands(rt)
    for name, (description, hint) in COMMANDS.items():
        ctx.register_command(name, commands[name], description=description, args_hint=hint)

    for skill_name, sub in FUND_SKILLS.items():
        skill_md = SKILLS_DIR / sub / "SKILL.md"
        if skill_md.exists():
            ctx.register_skill(skill_name, str(skill_md))
        else:
            _log.warning("fund skill missing at %s; skipping", skill_md)
    return rt
