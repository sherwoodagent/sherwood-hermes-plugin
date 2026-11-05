"""Secret lookup that follows the Hermes rule: never read `.env` directly.

Inside Hermes, `agent.secret_scope.get_secret(NAME)` resolves the secret for
the active profile (and refuses global env in multiplex mode). Outside Hermes
(tests, `python -m`), there is no secret scope, so we fall back to the process
environment.
"""
from __future__ import annotations

import os

# Declared in plugin.yaml `requires_env`.
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
PRIVY_APP_ID = "PRIVY_APP_ID"
PRIVY_APP_SECRET = "PRIVY_APP_SECRET"
SHERWOOD_TG_LINK_SECRET = "SHERWOOD_TG_LINK_SECRET"
# Bearer for the app's /api/tg/link lookup; NOT the link-signing key.
SHERWOOD_TG_API_TOKEN = "SHERWOOD_TG_API_TOKEN"
TX_BUILDER_URL = "TX_BUILDER_URL"
APP_BASE_URL = "APP_BASE_URL"


def get_secret(name: str, default: str | None = None) -> str | None:
    try:
        from agent.secret_scope import get_secret as _scoped  # type: ignore[import-not-found]
    except ImportError:
        return os.environ.get(name, default)
    try:
        value = _scoped(name, default)
    except Exception:
        # UnscopedSecretError and friends: the scope refused. Do NOT fall back
        # to os.environ here, that would leak another profile's secret.
        return default
    return value if value is not None else default
