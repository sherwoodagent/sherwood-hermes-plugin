"""Thin adapter between fund state (plugin-core) and the Telegram layer.

Plugin-core owns ``ProposalRecord`` / ``Fund`` and the JSON store. This module
only (1) maps those records onto the Telegram view types, (2) implements the
``FundGateway`` port from plain callables plugin-core wires, and (3) registers
the Telegram platform handler and the pitch-context hook with Hermes.

Integration, from ``sherwood_monitor.register(ctx)``::

    from .fund.telegram import CallbackFundGateway, register_telegram

    gateway = CallbackFundGateway(
        get_fund_for_chat=store.fund_for_chat,          # (chat_id) -> Fund | None
        get_fund_by_vault=store.fund_by_vault,          # (vault) -> Fund | None
        get_proposal=store.get_proposal,                # (fund, proposal_id) -> ProposalRecord | None
        toggle_backer_fn=store.toggle_backer,           # (fund, proposal_id, tg_id) -> ProposalRecord
        set_card_message_id_fn=store.set_card_message_id,  # (fund, proposal_id, message_id)
        ship_draft_fn=rules.ship_draft,                 # (fund, draft_id, by_tg_id) -> str toast
        host_signing_url_fn=...,                        # (fund, draft_id, host_tg_id) -> str | None
        approve_member_fn=..., approve_agent_fn=..., remove_agent_fn=...,
        leaderboard_fn=...,                             # (fund) -> [ {tg_id, pnl_bps, ideas, wins} ]
        on_draft_requested_fn=rules.on_draft_requested, # (DraftRequested) -> None
        wood_usd_fn=...,                                # () -> float | None
    )
    runtime = register_telegram(ctx, gateway)           # None when Hermes has no platform handlers

Every callable may be sync or async. Actions raise ``FundActionError`` with a
user-facing reason on failure.

This module imports no Telegram code at import time; PTB is imported inside
the factory Hermes calls when its Telegram adapter connects.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from sherwood_monitor.telegram.runtime import TelegramRuntime
from sherwood_monitor.telegram.settings import TelegramSettings
from sherwood_monitor.telegram.types import (
    DEFAULT_THRESHOLD_BPS,
    DraftRequested,
    FundActionError,
    FundView,
    LeaderRow,
    Leg,
    MemberView,
    Person,
    Position,
    ProposalView,
    maybe_await,
)

_log = logging.getLogger(__name__)

_MISSING = object()


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present attribute / key among ``names``."""
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, _MISSING)
            if value is not _MISSING and value is not None:
                return value
    return default


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


def _ids(values: Iterable[Any] | None) -> tuple[int, ...]:
    out: list[int] = []
    for v in values or ():
        i = _int_or_none(_get(v, "tg_id", "telegram_id", "id", default=v) if not isinstance(v, (int, str)) else v)
        if i is not None and i not in out:
            out.append(i)
    return tuple(out)


def proposal_to_view(record: Any) -> ProposalView:
    """Map a plugin-core ``ProposalRecord`` (object or dict) onto :class:`ProposalView`."""
    legs = tuple(
        Leg(symbol=str(_get(leg, "symbol", default="?")), weight_bps=int(_get(leg, "weight_bps", default=0)))
        for leg in (_get(record, "legs", default=()) or ())
    )
    return ProposalView(
        id=str(_get(record, "id", "proposal_id")),
        governor=str(_get(record, "governor", default="") or ""),
        proposer=str(_get(record, "proposer", "proposer_agent", "proposer_agent_name", "agent", default="") or ""),
        thesis=str(_get(record, "thesis", default="") or ""),
        legs=legs,
        max_capital=float(_get(record, "max_capital", default=0) or 0),
        max_drawdown_bps=int(_get(record, "max_drawdown_bps", default=0) or 0),
        bond_wood=float(_get(record, "bond_wood", default=0) or 0),
        status=str(_get(record, "status", default="draft")),
        backers=_ids(_get(record, "backers", default=())),
        objectors=_ids(_get(record, "objectors", default=())),
        idea_by=_int_or_none(_get(record, "idea_by")),
        card_message_id=_int_or_none(_get(record, "card_message_id")),
        voting_ends_at=_int_or_none(_get(record, "voting_ends_at")),
        against_bps=int(_get(record, "against_bps", default=0) or 0),
        threshold_bps=int(_get(record, "threshold_bps", default=DEFAULT_THRESHOLD_BPS) or DEFAULT_THRESHOLD_BPS),
        image_url=_get(record, "image_url"),
        capital_symbol=str(_get(record, "capital_symbol", "asset_symbol", default="USDC")),
    )


def _member_views(members: Iterable[Any] | None) -> tuple[MemberView, ...]:
    out: list[MemberView] = []
    for i, m in enumerate(members or (), start=1):
        if isinstance(m, (int, str)):
            tg = _int_or_none(m)
            if tg is not None:
                out.append(MemberView(tg_id=tg, number=i, share_bps=0))
            continue
        tg = _int_or_none(_get(m, "tg_id", "telegram_id", "telegram_user_id", "id"))
        if tg is None:
            continue
        out.append(
            MemberView(
                tg_id=tg,
                number=int(_get(m, "number", "member_number", default=i)),
                share_bps=int(_get(m, "share_bps", default=0) or 0),
                joined_at=_int_or_none(_get(m, "joined_at")),
                username=_get(m, "username", "telegram_username"),
                first_name=_get(m, "first_name"),
                last_name=_get(m, "last_name"),
            )
        )
    return tuple(out)


def fund_to_view(fund: Any, proposals: Iterable[Any] | None = None) -> FundView:
    """Map a plugin-core ``Fund`` onto :class:`FundView`.

    ``open_proposals`` may hold records or ids; pass resolved ``proposals``
    when the fund only stores ids.
    """
    open_items = list(proposals if proposals is not None else (_get(fund, "open_proposals", default=()) or ()))
    open_views = tuple(proposal_to_view(p) for p in open_items if not isinstance(p, (int, str)))
    agents = tuple(
        str(a if isinstance(a, str) else _get(a, "name", "agent_name", default=a))
        for a in (_get(fund, "agents", default=()) or ())
    )
    positions = tuple(
        Position(
            symbol=str(_get(p, "symbol", default="?")),
            value=float(_get(p, "value", "value_usd", "usd", default=0) or 0),
            pnl_bps=_int_or_none(_get(p, "pnl_bps")),
        )
        for p in (_get(fund, "positions", default=()) or ())
    )
    return FundView(
        vault=str(_get(fund, "vault", "vault_address")),
        governor=str(_get(fund, "governor", "governor_address", default="") or ""),
        name=str(_get(fund, "name", default="Fund")),
        telegram_chat_id=_int_or_none(_get(fund, "telegram_chat_id", "chat_id")),
        host_tg_id=_int_or_none(_get(fund, "host_telegram_id", "host_tg_id", "host_telegram_user_id", "host")),
        members=_member_views(_get(fund, "members", default=())),
        agents=agents,
        nav=float(_get(fund, "nav", default=0) or 0),
        pnl_bps=int(_get(fund, "pnl_bps", default=0) or 0),
        positions=positions,
        open_proposals=open_views,
        nav_symbol=str(_get(fund, "nav_symbol", "asset_symbol", default="USDC")),
    )


def _person(tg_id: int, value: Any) -> Person:
    if isinstance(value, Person):
        return value
    if isinstance(value, str):
        return Person(tg_id=tg_id, username=value.lstrip("@") or None)
    return Person(
        tg_id=tg_id,
        username=_get(value, "username"),
        first_name=_get(value, "first_name"),
        last_name=_get(value, "last_name"),
    )


@dataclass
class CallbackFundGateway:
    """``FundGateway`` built from plain (sync or async) callables over plugin-core records."""

    get_fund_for_chat: Callable[[int], Any]
    get_fund_by_vault: Callable[[str], Any] | None = None
    get_proposal: Callable[[Any, str], Any] | None = None
    toggle_backer_fn: Callable[[Any, str, int], Any] | None = None
    set_card_message_id_fn: Callable[[Any, str, int], Any] | None = None
    ship_draft_fn: Callable[[Any, str, int], Any] | None = None
    host_signing_url_fn: Callable[[Any, str, int], Any] | None = None
    approve_member_fn: Callable[[Any, int, int], Any] | None = None
    approve_agent_fn: Callable[[Any, str, int], Any] | None = None
    remove_agent_fn: Callable[[Any, str, int], Any] | None = None
    leaderboard_fn: Callable[[Any], Any] | None = None
    on_draft_requested_fn: Callable[[DraftRequested], Any] | None = None
    wood_usd_fn: Callable[[], Any] | None = None
    people_fn: Callable[[Any], Any] | None = None
    resolve_proposals_fn: Callable[[Any], Any] | None = None  # (fund) -> open ProposalRecords

    async def _fund_record(self, chat_id: int) -> Any:
        return await maybe_await(self.get_fund_for_chat(int(chat_id)))

    async def _view(self, fund: Any) -> FundView | None:
        if fund is None:
            return None
        proposals = None
        if self.resolve_proposals_fn is not None:
            proposals = await maybe_await(self.resolve_proposals_fn(fund))
        return fund_to_view(fund, proposals)

    async def fund_for_chat(self, chat_id: int) -> FundView | None:
        return await self._view(await self._fund_record(chat_id))

    async def fund_by_vault(self, vault: str) -> FundView | None:
        if self.get_fund_by_vault is None:
            return None
        return await self._view(await maybe_await(self.get_fund_by_vault(vault)))

    async def proposal(self, chat_id: int, proposal_id: str) -> ProposalView | None:
        if self.get_proposal is None:
            return None
        fund = await self._fund_record(chat_id)
        if fund is None:
            return None
        record = await maybe_await(self.get_proposal(fund, str(proposal_id)))
        return None if record is None else proposal_to_view(record)

    async def people(self, chat_id: int) -> dict[int, Person]:
        fund = await self._fund_record(chat_id)
        if fund is None:
            return {}
        out: dict[int, Person] = {}
        for m in fund_to_view(fund, ()).members:
            if m.username or m.first_name:
                out[m.tg_id] = m.person
        if self.people_fn is not None:
            extra = await maybe_await(self.people_fn(fund)) or {}
            for tg, value in dict(extra).items():
                tg_i = _int_or_none(tg)
                if tg_i is not None:
                    out[tg_i] = _person(tg_i, value)
        return out

    async def wood_usd(self) -> float | None:
        if self.wood_usd_fn is None:
            return None
        value = await maybe_await(self.wood_usd_fn())
        return None if value is None else float(value)

    async def _action(self, fn: Callable[..., Any] | None, chat_id: int, *args: Any, what: str) -> Any:
        if fn is None:
            raise FundActionError(f"{what} isn't available yet.")
        fund = await self._fund_record(chat_id)
        if fund is None:
            raise FundActionError("No fund is linked to this chat.")
        return await maybe_await(fn(fund, *args))

    async def toggle_backer(self, chat_id: int, proposal_id: str, tg_id: int) -> ProposalView | None:
        if self.toggle_backer_fn is None:
            return None
        fund = await self._fund_record(chat_id)
        if fund is None:
            return None
        record = await maybe_await(self.toggle_backer_fn(fund, str(proposal_id), int(tg_id)))
        return None if record is None else proposal_to_view(record)

    async def set_card_message_id(self, chat_id: int, proposal_id: str, message_id: int) -> None:
        if self.set_card_message_id_fn is None:
            return
        fund = await self._fund_record(chat_id)
        if fund is not None:
            await maybe_await(self.set_card_message_id_fn(fund, str(proposal_id), int(message_id)))

    async def ship_draft(self, chat_id: int, draft_id: str, by_tg_id: int) -> str:
        return str(await self._action(self.ship_draft_fn, chat_id, str(draft_id), int(by_tg_id), what="Shipping drafts") or "")

    async def host_signing_url(self, chat_id: int, draft_id: str, host_tg_id: int) -> str | None:
        if self.host_signing_url_fn is None:
            return None
        fund = await self._fund_record(chat_id)
        if fund is None:
            return None
        url = await maybe_await(self.host_signing_url_fn(fund, str(draft_id), int(host_tg_id)))
        return str(url) if url else None

    async def approve_member(self, chat_id: int, tg_id: int, by_tg_id: int) -> str:
        return str(await self._action(self.approve_member_fn, chat_id, int(tg_id), int(by_tg_id), what="Member approval") or "")

    async def approve_agent(self, chat_id: int, agent_name: str, by_tg_id: int) -> str:
        return str(await self._action(self.approve_agent_fn, chat_id, agent_name, int(by_tg_id), what="Agent approval") or "")

    async def remove_agent(self, chat_id: int, agent_name: str, by_tg_id: int) -> str:
        return str(await self._action(self.remove_agent_fn, chat_id, agent_name, int(by_tg_id), what="Agent removal") or "")

    async def leaderboard(self, chat_id: int) -> list[LeaderRow]:
        if self.leaderboard_fn is None:
            return []
        fund = await self._fund_record(chat_id)
        if fund is None:
            return []
        rows = await maybe_await(self.leaderboard_fn(fund)) or []
        out: list[LeaderRow] = []
        for r in rows:
            tg = _int_or_none(_get(r, "tg_id", "telegram_id", "idea_by"))
            if tg is None:
                continue
            out.append(
                LeaderRow(
                    tg_id=tg,
                    pnl_bps=int(_get(r, "pnl_bps", default=0) or 0),
                    ideas=int(_get(r, "ideas", default=0) or 0),
                    wins=int(_get(r, "wins", default=0) or 0),
                )
            )
        return out

    async def on_draft_requested(self, event: DraftRequested) -> None:
        if self.on_draft_requested_fn is None:
            _log.info("DraftRequested for message %s but no handler is wired", event.source_message_id)
            return
        await maybe_await(self.on_draft_requested_fn(event))


def register_telegram(ctx: Any, gateway: Any, *, settings: TelegramSettings | None = None) -> TelegramRuntime | None:
    """Register the Telegram platform handler + pitch-context hook with Hermes.

    Returns the :class:`TelegramRuntime` plugin-core uses to post/refresh
    cards, or ``None`` when this Hermes has no ``register_platform_handler``.
    """
    register_platform_handler = getattr(ctx, "register_platform_handler", None)
    if not callable(register_platform_handler):
        _log.info("Hermes has no register_platform_handler; Telegram fund cards disabled")
        return None
    runtime = TelegramRuntime(gateway, settings)

    def _wire_sherwood_fund_telegram(native: Any, adapter: Any = None) -> None:
        if native is None:
            _log.warning("Telegram adapter handed no PTB Application; fund handlers not wired")
            return
        from sherwood_monitor.telegram.handler import wire  # PTB import happens here

        wire(native, runtime)

    register_platform_handler("telegram", _wire_sherwood_fund_telegram)
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("pre_llm_call", runtime.flow.make_pre_llm_call_hook())
    return runtime
