"""A recording stand-in for the Hermes plugin context.

Unlike a MagicMock, the register_* signatures here mirror Hermes v0.21.5
(`hermes_cli/plugins.py`) exactly and enforce the same preconditions, so a
registration Hermes would reject fails here too:

- `register_tool` needs `toolset`; async handlers need `is_async=True`.
- `register_hook` only accepts names in VALID_HOOKS.
- `register_skill` takes the SKILL.md path; names match [a-zA-Z0-9_-]+.
- `register_command` handlers get the raw argument string plus the session
  keywords they declare.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

VALID_HOOKS = {
    "pre_tool_call", "post_tool_call", "transform_terminal_output", "transform_tool_result",
    "transform_llm_output", "pre_llm_call", "post_llm_call", "on_session_start", "on_session_end",
    "on_session_finalize", "on_session_reset",
}
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_SESSION_KWARGS = ("session_id", "session_key", "platform", "source")


class FakeCtx:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self.hooks: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self.commands: dict[str, dict[str, Any]] = {}
        self.cli_commands: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, Path] = {}
        self.injected: list[dict[str, Any]] = []
        self._config = dict(config or {})

    # -- the Hermes surface ------------------------------------------------------------
    def register_tool(
        self, name: str, toolset: str, schema: dict, handler: Callable,
        check_fn: Callable | None = None, requires_env: list | None = None, is_async: bool = False,
        description: str = "", emoji: str = "", override: bool = False,
    ) -> None:
        if name in self.tools:
            raise ValueError(f"tool {name} registered twice")
        if not toolset:
            raise ValueError(f"tool {name} has no toolset")
        if inspect.iscoroutinefunction(handler) and not is_async:
            raise ValueError(f"tool {name} has an async handler but is_async=False (Hermes would not await it)")
        if schema.get("name") != name:
            raise ValueError(f"tool {name} schema name is {schema.get('name')!r}")
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "is_async": is_async}

    def register_hook(self, hook_name: str, callback: Callable) -> None:
        if hook_name not in VALID_HOOKS:
            raise ValueError(f"unknown hook {hook_name}")
        self.hooks[hook_name].append(callback)

    def register_command(
        self, name: str, handler: Callable, description: str = "", args_hint: str = "",
        argument_mode: str | None = None,
    ) -> None:
        clean = name.lower().strip().lstrip("/").replace(" ", "-")
        if not clean or clean in self.commands:
            raise ValueError(f"bad or duplicate command {name!r}")
        self.commands[clean] = {"handler": handler, "description": description, "args_hint": args_hint}

    def register_cli_command(
        self, name: str, help: str, setup_fn: Callable, handler_fn: Callable | None = None,
        description: str = "",
    ) -> None:
        self.cli_commands[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}

    def register_skill(
        self, name: str, path: Path | str, description: str = "",
        frontmatter: Optional[dict[str, Any]] = None,
    ) -> None:
        if ":" in name or not _SKILL_NAME_RE.match(name):
            raise ValueError(f"invalid skill name {name!r}")
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"SKILL.md not found at {p}")
        self.skills[name] = p

    def get_config(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def inject_message(self, content: str, role: str = "user", *, session_key: str | None = None) -> bool:
        self.injected.append({"content": content, "role": role, "session_key": session_key})
        return True

    # -- test helpers ------------------------------------------------------------------------
    def call_tool(self, name: str, args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        entry = self.tools[name]
        result = entry["handler"](args or {}, **kwargs)
        if entry["is_async"]:
            result = asyncio.run(result)
        return json.loads(result)

    def run_command(self, name: str, raw_args: str = "", **session: Any) -> str:
        handler = self.commands[name]["handler"]
        params = inspect.signature(handler).parameters
        takes_all = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs = {k: v for k, v in session.items() if k in _SESSION_KWARGS and (takes_all or k in params)}
        out = handler(raw_args, **kwargs)
        return asyncio.run(out) if inspect.isawaitable(out) else out

    async def fire(self, hook_name: str, **kwargs: Any) -> list[Any]:
        results = []
        for cb in self.hooks[hook_name]:
            out = cb(**kwargs)
            if inspect.isawaitable(out):
                out = await out
            if out is not None:
                results.append(out)
        return results
