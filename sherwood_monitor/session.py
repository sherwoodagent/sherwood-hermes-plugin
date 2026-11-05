"""Who is this turn for: the Hermes session the current tool call / hook runs in.

Hermes binds the gateway session to ContextVars (``gateway.session_context``:
``HERMES_SESSION_PLATFORM``, ``HERMES_SESSION_CHAT_ID``, ``HERMES_SESSION_USER_ID``)
for the whole agent turn, so tool handlers and hooks can read who sent the
message that started it. Hook kwargs (``platform``, ``sender_id``) win when
given. Outside Hermes (tests, scripts) everything is empty.

The plugin uses this to keep group chat from driving host actions: a host-only
tool called in a turn that a Telegram user started runs only when that user is
the fund's host, and queued chat context is delivered only to a turn from the
same chat.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Surfaces that are not a human chat channel (mirrors Hermes'
# NON_MESSAGING_SESSION_SURFACES, plus cron).
NON_MESSAGING = frozenset({
    "", "api_server", "cli", "codex", "cron", "desktop", "gateway", "kanban", "local",
    "msgraph_webhook", "tool", "tui", "webhook",
})


def _session_env(name: str) -> str:
    """One Hermes session var. Tests patch this.

    Inside Hermes: the session ContextVar (``get_session_env`` falls back to the
    process env itself). Elsewhere: the process env, which is how a terminal child
    (e.g. ``hermes sherwood fund-bind`` run by the agent) inherits the chat turn's
    ``HERMES_SESSION_*`` (tools/environments/local.py).
    """
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    except Exception:
        return os.environ.get(name, "") or ""
    try:
        return str(get_session_env(name, "") or "")
    except Exception:
        return os.environ.get(name, "") or ""


@dataclass(frozen=True)
class Caller:
    platform: str = ""
    user_id: str = ""
    chat_id: str = ""

    @property
    def messaging(self) -> bool:
        """A human chat channel (a group member could have typed this turn)."""
        return self.platform not in NON_MESSAGING

    @property
    def telegram_user(self) -> str | None:
        return self.user_id or None if self.platform == "telegram" else None


def current_caller(**hook_kwargs: Any) -> Caller:
    platform = str(hook_kwargs.get("platform") or _session_env("HERMES_SESSION_PLATFORM") or "").strip().lower()
    user = str(hook_kwargs.get("sender_id") or _session_env("HERMES_SESSION_USER_ID") or "").strip()
    chat = str(hook_kwargs.get("chat_id") or _session_env("HERMES_SESSION_CHAT_ID") or "").strip()
    return Caller(platform=platform, user_id=user, chat_id=chat)
