"""Settings and secret access for the Telegram side of the fund.

Secrets (``TELEGRAM_BOT_TOKEN``, ``SHERWOOD_TG_LINK_SECRET``) are read through
Hermes' profile-scoped ``agent.secret_scope.get_secret`` when Hermes is
importable, and from the process environment otherwise (tests, the smoke
script). Values are never logged.

Pure module: no Telegram imports; the Hermes import is guarded.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Mapping

from .links import DEFAULT_APP_BASE_URL

_log = logging.getLogger(__name__)

BOT_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
LINK_SECRET_KEY = "SHERWOOD_TG_LINK_SECRET"
APP_BASE_URL_KEY = "APP_BASE_URL"
GROUP_ALLOWED_CHATS_KEY = "TELEGRAM_GROUP_ALLOWED_CHATS"
PERSONAL_LINKS_KEY = "SHERWOOD_TG_PERSONAL_LINKS"
BOT_USERNAME_KEY = "TELEGRAM_BOT_USERNAME"

PERSONAL_LINK_MODES = ("ephemeral", "dm")


def _hermes_get_secret():
    try:
        from agent.secret_scope import get_secret  # type: ignore[import-not-found]
    except Exception:
        return None
    return get_secret


def get_secret(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Return a secret by name, or ``None``.

    Inside Hermes the profile-scoped store is authoritative (no fallback to the
    raw process env, so one profile never borrows another profile's token).
    Outside Hermes (tests, scripts) the environment is used.
    """
    hermes = _hermes_get_secret() if env is None else None
    if hermes is not None:
        try:
            value = hermes(name, "")
        except TypeError:
            value = hermes(name)
        except Exception as exc:  # never include the value
            _log.warning("secret lookup for %s failed: %s", name, type(exc).__name__)
            return None
        value = (value or "").strip() if isinstance(value, str) else ""
        return value or None
    source = os.environ if env is None else env
    value = (source.get(name) or "").strip()
    return value or None


def _parse_chat_ids(raw: str | None) -> frozenset[int]:
    ids: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            _log.warning("ignoring non-numeric chat id in %s", GROUP_ALLOWED_CHATS_KEY)
    return frozenset(ids)


@dataclass(frozen=True)
class TelegramSettings:
    """Runtime knobs. Defaults match the hackathon demo."""

    app_base_url: str = DEFAULT_APP_BASE_URL
    bot_username: str = ""  # filled from getMe at runtime when empty
    allowed_chat_ids: frozenset[int] = field(default_factory=frozenset)
    fire_threshold: int = 3
    fire_emoji: str = "🔥"
    refresh_interval_s: float = 60.0
    veto_threshold_bps: int = 2000
    personal_link_mode: str = "ephemeral"
    pitch_ttl_s: float = 6 * 3600.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TelegramSettings":
        def read(key: str) -> str | None:
            return get_secret(key, env)

        mode = (read(PERSONAL_LINKS_KEY) or "ephemeral").lower()
        if mode not in PERSONAL_LINK_MODES:
            _log.warning("unknown %s=%r; using 'ephemeral'", PERSONAL_LINKS_KEY, mode)
            mode = "ephemeral"
        return cls(
            app_base_url=(read(APP_BASE_URL_KEY) or DEFAULT_APP_BASE_URL).rstrip("/"),
            bot_username=(read(BOT_USERNAME_KEY) or "").lstrip("@"),
            allowed_chat_ids=_parse_chat_ids(read(GROUP_ALLOWED_CHATS_KEY)),
            personal_link_mode=mode,
        )

    def chat_allowed(self, chat_id: int) -> bool:
        """Empty allowlist = any chat that is linked to a fund."""
        return not self.allowed_chat_ids or int(chat_id) in self.allowed_chat_ids


def link_secret(env: Mapping[str, str] | None = None) -> str | None:
    return get_secret(LINK_SECRET_KEY, env)


def bot_token(env: Mapping[str, str] | None = None) -> str | None:
    return get_secret(BOT_TOKEN_KEY, env)
