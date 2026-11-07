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
when the handler is wired (the bound scope when one is installed, else the
mapping Hermes itself binds for this home: ``owner_secret_scope``, which keeps
the launch profile's process-env-only secrets, or ``build_profile_secret_scope``
when that cannot be imported) and install it around every handler call,
resetting it afterwards. The captured scope is frozen until the gateway
restarts. Never widen an allowlist, never log a value. Outside Hermes (tests,
scripts) there is nothing to capture and the handlers read the environment as
before.
"""
from __future__ import annotations

import contextlib
import importlib
import logging
from dataclasses import dataclass, field
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

    mapping: Mapping[str, str] = field(repr=False)  # secrets: never in a repr or a log line
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
    run inside the profile scope), else the mapping Hermes binds for this
    profile's home (``owner_secret_scope``: for the launch home, process env plus
    ``.env``, so a secret injected by launchd, systemd or ``op run`` is kept),
    falling back to ``build_profile_secret_scope`` when that cannot be imported.
    Frozen until the gateway restarts. None outside Hermes, or when it cannot be
    built (logged without values).
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
        try:
            from tui_gateway.launch_profile_policy import owner_secret_scope  # type: ignore[import-not-found]
        except ImportError:
            return SecretScope(hermes.build_profile_secret_scope(home), str(home))
        return SecretScope(owner_secret_scope(home), str(home))
    except Exception as exc:  # never a value in the message
        _log.warning(
            "telegram fund handlers: could not capture the profile secret scope (%s); "
            "secret reads in them can fail while multiplex_profiles is on", type(exc).__name__,
        )
        return None


__all__ = ["SecretScope", "capture_secret_scope"]
