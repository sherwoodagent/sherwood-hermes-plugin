"""Per-process wiring: which agent this Hermes is, and where fund state lives."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .applink import AppLinkClient
from .service import FundService, FundServiceError
from .settings import FundSettings
from .store import FundStateBackend, default_state_dir, open_backend


@dataclass
class FundRuntime:
    service: FundService
    settings: FundSettings
    # Telegram id -> linked wallet from the app (GET /api/tg/link); None disables the lookup.
    applink: AppLinkClient | None = None

    def agent_name(self) -> str:
        name = self.settings.agent_name.strip()
        if not name:
            raise FundServiceError(
                "this Hermes has no fund agent name: set `fund.agent_name` (plus agent_identity_id "
                "and agent_address) in ~/.hermes/plugins/sherwood-monitor/config.yaml"
            )
        return name

    def fund_for(self, fund: str | None = None, *, chat_id: str | None = None) -> str:
        return self.service.resolve_fund(fund, chat_id=chat_id, default=self.settings.default_fund)


def build_runtime(
    settings: FundSettings,
    *,
    backend: FundStateBackend | None = None,
    clock: Callable[[], float] = time.time,
) -> FundRuntime:
    # Pin the state directory now: every later read and write in this process
    # must hit the same files, whatever happens to $HOME afterwards.
    backend = backend or open_backend(
        settings.fund_state_backend,
        state_dir=settings.fund_state_dir or default_state_dir(),
        state_url=settings.fund_state_url,
    )
    return FundRuntime(
        FundService(backend, clock=clock, tx_builder_url=settings.tx_builder_url),
        settings,
        AppLinkClient(settings.app_base_url, allowed_hosts=settings.app_link_hosts),
    )


def session_ids(source: Any) -> tuple[str | None, str | None]:
    """(caller user id, chat id) from a Hermes SessionSource, when the platform says."""
    if source is None:
        return None, None
    user = getattr(source, "user_id", None)
    chat = getattr(source, "chat_id", None)
    if isinstance(source, dict):
        user, chat = source.get("user_id"), source.get("chat_id")
    return (str(user) if user else None), (str(chat) if chat else None)
