"""Environment for the processes this plugin spawns (the `sherwood` CLI).

The plugin's own secrets stay in the plugin's process. Children get a copy of
the environment without them; the `sherwood` CLI reads its own config.
Everything else (PATH, HOME, the CLI's own PRIVATE_KEY /
ROBINHOOD_RPC_URL inputs, proxies, locale) passes through unchanged.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

# Exact names, plus every SHERWOOD_TG_* (link-signing secret, app API token, link mode).
STRIPPED_NAMES = frozenset({"SHERWOOD_PRIVATE_KEY", "TELEGRAM_BOT_TOKEN"})
STRIPPED_PREFIXES = ("SHERWOOD_TG_",)


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``base`` (default ``os.environ``) without the plugin's secrets."""
    source = os.environ if base is None else base
    return {
        k: v
        for k, v in source.items()
        if k.upper() not in STRIPPED_NAMES and not k.upper().startswith(STRIPPED_PREFIXES)
    }
