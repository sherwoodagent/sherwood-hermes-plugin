"""Hermes profile secret scope for the plugin's own Telegram handlers.

Hermes keeps secrets context-local (``agent.secret_scope``). With
``multiplex_profiles`` on (Hermes' default on a multi-profile install), a
``get_secret`` with no scope installed raises ``UnscopedSecretError`` instead
of reading the process environment. The plugin's native python-telegram-bot
handlers (buttons, ``/fund``, ``/leaderboard``, ``/start``, group messages and
reactions, registered by ``handler.wire``) run in PTB's own tasks, outside any
Hermes turn, so no scope is installed there: ``SHERWOOD_TG_LINK_SECRET`` and
``SHERWOOD_TG_API_TOKEN`` reads failed and every personal link was refused.

Hermes' own guidance (the ``UnscopedSecretError`` docstring and the
multiplexing guide, Workstream A; the gateway does the same around a turn) is
to wrap the call path in ``set_secret_scope``: capture this profile's scope
when the handler is wired (the bound scope when one is installed, else
``build_profile_secret_scope`` for this profile's Hermes home) and install it
around every handler call, resetting it afterwards. Never widen an allowlist,
never read ``os.environ``, never log a value. Outside Hermes (tests, scripts)
there is nothing to capture and the handlers read the environment as before.
"""
from __future__ import annotations

import contextlib
import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

_log = logging.getLogger(__name__)


def _hermes_scope() -> Any:
    """``agent.secret_scope`` when running inside Hermes, else None."""
    try:
        module = importlib.import_module("agent.secret_scope")
    except Exception:
        return None
    needed = ("set_secret_scope", "reset_secret_scope", "current_secret_scope", "build_profile_secret_scope")
    return module if all(hasattr(module, name) for name in needed) else None


@dataclass(frozen=True)
class SecretScope:
    """A profile's secret mapping and the home it was built for, installable around a handler call."""

    mapping: Mapping[str, str]
    profile_home: str | None = None

    @contextlib.contextmanager
    def installed(self) -> Iterator[None]:
        hermes = _hermes_scope()
        if hermes is None:
            yield
            return
        token = hermes.set_secret_scope(self.mapping, profile_home=self.profile_home)
        try:
            yield
        finally:
            hermes.reset_secret_scope(token)


def capture_secret_scope() -> SecretScope | None:
    """This profile's secret scope, captured once when the Telegram handler is wired.

    The scope bound right now when Hermes installed one (the adapter connect may
    run inside the profile scope; the mapping object is shared, so Hermes'
    in-place refreshes reach it), else one built from this profile's Hermes home.
    None outside Hermes, or when it cannot be built (logged without values).
    """
    hermes = _hermes_scope()
    if hermes is None:
        return None
    try:
        bound = hermes.current_secret_scope()
        if bound is not None:
            home_of = getattr(hermes, "current_secret_scope_home", None)
            return SecretScope(bound, home_of() if callable(home_of) else None)
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        home = Path(get_hermes_home())
        return SecretScope(hermes.build_profile_secret_scope(home), str(home))
    except Exception as exc:  # never a value in the message
        _log.warning(
            "telegram fund handlers: could not capture the profile secret scope (%s); "
            "secret reads in them can fail while multiplex_profiles is on", type(exc).__name__,
        )
        return None


__all__ = ["SecretScope", "capture_secret_scope"]
