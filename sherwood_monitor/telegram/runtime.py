"""TelegramRuntime: shared state between the PTB handlers and plugin-core.

Created at plugin ``register()`` time (no Telegram import needed), attached to
the PTB ``Application`` when Hermes' Telegram adapter connects and calls the
platform-handler factory. Plugin-core uses it to post and refresh cards:

    runtime.submit(runtime.post_proposal_card(chat_id, proposal_id))

``submit`` is safe from any thread (Hermes tools may run off the event loop).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine

from .flow import PitchFlow
from .settings import TelegramSettings, link_secret
from .types import DraftRequested, FundGateway, MemberView, Person, Receipt, maybe_await

_log = logging.getLogger(__name__)

CAPS_TTL_S = 600.0


@dataclass(frozen=True)
class ChatCaps:
    """What Telegram will deliver to the bot in a group."""

    is_admin: bool
    reads_all: bool  # privacy mode off (getMe.can_read_all_group_messages) or admin
    checked_at: float = 0.0

    @property
    def mode(self) -> str:
        if self.is_admin:
            return "full"  # every message + message_reaction updates
        if self.reads_all:
            return "all_messages"  # privacy off: messages yes, reactions no
        return "mention_only"  # commands, @mentions, replies to the bot

    @property
    def notice(self) -> str | None:
        if self.mode == "all_messages":
            return "Make me a group admin so 🔥 reactions count (🔥 replies count meanwhile)."
        if self.mode == "mention_only":
            return (
                "I only see commands, mentions and replies to me. Make me a group admin so "
                "pitches and 🔥 reactions work."
            )
        return None


class TelegramRuntime:
    def __init__(
        self,
        gateway: FundGateway,
        settings: TelegramSettings | None = None,
        *,
        clock: Callable[[], float] = time.time,
        secret: Callable[[], str | None] = link_secret,
    ) -> None:
        self.gateway = gateway
        self.settings = settings or TelegramSettings.from_env()
        self.clock = clock
        self._secret = secret
        self.flow = PitchFlow(
            host_for_chat=self.host_for_chat,
            on_draft_requested=self._on_draft_requested,
            fire_threshold=self.settings.fire_threshold,
            fire_emoji=self.settings.fire_emoji,
            pitch_ttl_s=self.settings.pitch_ttl_s,
            clock=clock,
        )
        self.app: Any = None
        self.bot: Any = None
        self.live: Any = None
        self.proposals: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._people: dict[int, dict[int, Person]] = {}
        self._caps: dict[int, ChatCaps] = {}
        self._warned: set[int] = set()

    # -- wiring ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.bot is not None

    def attach(self, application: Any) -> None:
        """Bind to the PTB Application (called from the platform-handler factory)."""
        from .cards_live import LiveCards, ProposalCards

        self.app = application
        self.bot = application.bot
        self.live = LiveCards(
            self.bot,
            job_queue=getattr(application, "job_queue", None),
            interval_s=self.settings.refresh_interval_s,
            clock=self.clock,
        )
        self.proposals = ProposalCards(
            self.live,
            self.gateway,
            base_url=self.settings.app_base_url,
            people=self.people_cache,
            clock=self.clock,
        )
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    def link_secret(self) -> str | None:
        return self._secret()

    @property
    def bot_username(self) -> str:
        if self.settings.bot_username:
            return self.settings.bot_username
        try:
            return str(self.bot.username or "")
        except Exception:
            return ""

    @property
    def bot_id(self) -> int | None:
        try:
            return int(self.bot.id)
        except Exception:
            return None

    def submit(self, coro: Coroutine[Any, Any, Any]) -> "asyncio.Future[Any] | concurrent.futures.Future[Any]":
        """Schedule ``coro`` on the bot's event loop from any thread."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and (self.loop is None or running is self.loop):
            return running.create_task(coro)
        if self.loop is None:
            coro.close()
            raise RuntimeError("telegram runtime is not attached to a running loop yet")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # -- fund-state helpers --------------------------------------------------

    async def host_for_chat(self, chat_id: int) -> int | None:
        fund = await maybe_await(self.gateway.fund_for_chat(chat_id))
        return None if fund is None else fund.host_tg_id

    async def _on_draft_requested(self, event: DraftRequested) -> None:
        await maybe_await(self.gateway.on_draft_requested(event))

    def note_person(self, chat_id: int | None, person: Person) -> None:
        if chat_id is None or person is None:
            return
        self._people.setdefault(int(chat_id), {})[int(person.tg_id)] = person

    def people_cache(self, chat_id: int) -> dict[int, Person]:
        return dict(self._people.get(int(chat_id), {}))

    async def people(self, chat_id: int) -> dict[int, Person]:
        merged = dict(await maybe_await(self.gateway.people(chat_id)) or {})
        for k, v in self.people_cache(chat_id).items():
            merged.setdefault(k, v)
        return merged

    async def capabilities(self, chat_id: int) -> ChatCaps:
        """Admin / privacy status in ``chat_id`` (cached 10 min). Never raises."""
        cached = self._caps.get(int(chat_id))
        now = self.clock()
        if cached is not None and now - cached.checked_at < CAPS_TTL_S:
            return cached
        is_admin = False
        reads_all = False
        try:
            reads_all = bool(getattr(self.bot, "can_read_all_group_messages", False))
        except Exception:
            reads_all = False
        try:
            member = await self.bot.get_chat_member(chat_id=chat_id, user_id=self.bot.id)
            is_admin = str(getattr(member, "status", "")) in ("administrator", "creator")
        except Exception as exc:
            _log.debug("get_chat_member failed for %s: %s", chat_id, exc)
        caps = ChatCaps(is_admin=is_admin, reads_all=reads_all or is_admin, checked_at=now)
        self._caps[int(chat_id)] = caps
        if caps.mode != "full" and int(chat_id) not in self._warned:
            self._warned.add(int(chat_id))
            _log.warning(
                "sherwood fund chat %s runs in %s mode: %s", chat_id, caps.mode, caps.notice or ""
            )
        return caps

    # -- plugin-core API: post / refresh cards ------------------------------

    def _require(self) -> None:
        if not self.connected:
            raise RuntimeError("telegram runtime is not connected (Hermes Telegram adapter not up)")

    async def post_proposal_card(
        self, chat_id: int, proposal_id: str, *, reply_to_message_id: int | None = None
    ) -> int | None:
        """Post the proposal card, store its message id, start the live refresh if voting."""
        self._require()
        ref = await self.proposals.post(chat_id, proposal_id, reply_to_message_id=reply_to_message_id)
        if ref is None:
            return None
        self.flow.note_bot_card(chat_id, ref.message_id)
        return ref.message_id

    async def refresh_proposal_card(self, chat_id: int, proposal_id: str) -> bool:
        """Re-render the card now (after a vote, a 👍, a status change). Starts/keeps the refresh."""
        self._require()
        from .cards_live import CardGone

        view = await maybe_await(self.gateway.proposal(chat_id, proposal_id))
        if view is None:
            return False
        try:
            changed = await self.proposals.refresh(chat_id, proposal_id, view=view)
        except CardGone:
            self.proposals.unwatch(chat_id, proposal_id)
            return False
        if (view.status or "").lower() == "voting":
            self.proposals.watch(chat_id, proposal_id, view)
        else:
            self.proposals.unwatch(chat_id, proposal_id)
        return changed

    async def post_draft_card(
        self,
        chat_id: int,
        draft_id: str,
        *,
        bond_shortfall_wood: float | None = None,
        requires_host_cosign: bool = False,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        self._require()
        from .cards import render_draft_card

        view = await maybe_await(self.gateway.proposal(chat_id, draft_id))
        if view is None:
            return None
        fund = await maybe_await(self.gateway.fund_for_chat(chat_id))
        text, markup = render_draft_card(
            view,
            fund=fund,
            people=await self.people(chat_id),
            wood_usd=await maybe_await(self.gateway.wood_usd()),
            bond_shortfall_wood=bond_shortfall_wood,
            requires_host_cosign=requires_host_cosign,
        )
        ref = await self.live.post(
            chat_id, text, markup, image_url=view.image_url, reply_to_message_id=reply_to_message_id
        )
        self.flow.note_bot_card(chat_id, ref.message_id)
        return ref.message_id

    async def post_fund_card(self, chat_id: int) -> int | None:
        self._require()
        from .cards import render_fund_card

        fund = await maybe_await(self.gateway.fund_for_chat(chat_id))
        if fund is None:
            return None
        caps = await self.capabilities(chat_id)
        text, markup = render_fund_card(
            fund,
            people=await self.people(chat_id),
            base_url=self.settings.app_base_url,
            now=self.clock(),
            notice=caps.notice,
        )
        ref = await self.live.post(chat_id, text, markup)
        self.flow.note_bot_card(chat_id, ref.message_id)
        return ref.message_id

    async def post_receipt(self, chat_id: int, receipt: Receipt) -> int | None:
        self._require()
        from .cards import render_settle_receipt

        fund = await maybe_await(self.gateway.fund_for_chat(chat_id))
        text, markup = render_settle_receipt(
            receipt, people=await self.people(chat_id), fund=fund, base_url=self.settings.app_base_url
        )
        ref = await self.live.post(chat_id, text, markup)
        self.flow.note_bot_card(chat_id, ref.message_id)
        return ref.message_id

    async def post_member_card(self, chat_id: int, member: MemberView) -> int | None:
        self._require()
        from .cards import render_member_card

        fund = await maybe_await(self.gateway.fund_for_chat(chat_id))
        if fund is None:
            return None
        text, markup = render_member_card(member, fund=fund, base_url=self.settings.app_base_url)
        ref = await self.live.post(chat_id, text, markup)
        self.flow.note_bot_card(chat_id, ref.message_id)
        return ref.message_id

    async def post_member_request(self, chat_id: int, person: Person) -> int | None:
        self._require()
        from .cards import render_member_request

        fund = await maybe_await(self.gateway.fund_for_chat(chat_id))
        if fund is None:
            return None
        text, markup = render_member_request(fund, person)
        ref = await self.live.post(chat_id, text, markup)
        self.flow.note_bot_card(chat_id, ref.message_id)
        return ref.message_id

    async def post_agent_card(self, chat_id: int, agent_name: str, *, pending: bool = True) -> int | None:
        self._require()
        from .cards import render_agent_card

        fund = await maybe_await(self.gateway.fund_for_chat(chat_id))
        if fund is None:
            return None
        text, markup = render_agent_card(fund, agent_name, pending=pending)
        ref = await self.live.post(chat_id, text, markup)
        self.flow.note_bot_card(chat_id, ref.message_id)
        return ref.message_id


def run_sync(runtime: TelegramRuntime, coro: Awaitable[Any], timeout: float = 30.0) -> Any:
    """Blocking helper for sync callers on another thread: submit and wait."""
    fut = runtime.submit(coro)  # type: ignore[arg-type]
    if isinstance(fut, concurrent.futures.Future):
        return fut.result(timeout=timeout)
    raise RuntimeError("run_sync called from the bot's own event loop; await the coroutine instead")
