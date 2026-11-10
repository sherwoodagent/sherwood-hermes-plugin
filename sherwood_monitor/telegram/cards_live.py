"""Posting cards and editing them in place.

* ``LiveCards.post`` sends a card (``send_photo`` when an image is attached).
* ``LiveCards.edit`` is idempotent: it skips the API call when the rendered
  text, keyboard and image are unchanged, and treats Telegram's
  "message is not modified" as success. Image changes use
  ``editMessageMedia``; caption-only changes use ``editMessageCaption``.
* ``LiveCards.start_refresh`` re-renders a card every 60 s through PTB's
  ``JobQueue`` (or an asyncio task when the ``job-queue`` extra is missing)
  until the tick reports the card is final.
* ``ProposalCards`` wires that to fund state: the countdown and veto meter
  refresh while a proposal is voting, one final edit lands when the window
  closes or the status leaves ``voting``, and the job stops.
* A card with greyed-out buttons (Bot API 10.3 ``disabled``) that Telegram
  refuses is posted / edited again without them, as before 10.3.
* ``PositionWidgets`` sends a member's 👤 My position widget (Bot API 10.4)
  to their DM, falling back to plain text, and rewrites its data when a card
  re-read changes what it shows.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from telegram import InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .cards import has_disabled, render_proposal_card, strip_disabled, view_fund_markup
from .types import FundGateway, FundView, Person, ProposalView, maybe_await
from .widget import (
    WIDGET_HTML,
    build_position,
    data_fingerprint,
    position_text,
    widget_data,
)

_log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
# 👤 My position widgets kept per fund chat for refreshes (the latest tappers; older ones stay as sent).
WIDGETS_PER_CHAT = 50
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

Tick = Callable[[], Awaitable[bool]]  # returns False when the card is final


class CardGone(Exception):
    """The message cannot be edited any more (deleted, too old, not ours)."""


@dataclass(frozen=True)
class CardRef:
    chat_id: int
    message_id: int
    image_url: str | None = None


def _fingerprint(text: str, markup: InlineKeyboardMarkup | None, image_url: str | None) -> str:
    payload = json.dumps(
        {"t": text, "m": markup.to_dict() if markup is not None else None, "i": image_url},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_not_modified(exc: BaseException) -> bool:
    return "not modified" in str(exc).lower()


def _is_gone(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        s in msg
        for s in ("message to edit not found", "message can't be edited", "message_id_invalid", "chat not found")
    )


class LiveCards:
    """Post / edit cards; run per-card refresh jobs."""

    def __init__(
        self,
        bot: Any,
        *,
        job_queue: Any = None,
        interval_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.bot = bot
        self.job_queue = job_queue
        self.interval_s = float(interval_s)
        self.clock = clock
        self._last: dict[tuple[int, int], str] = {}
        self.refs: dict[str, CardRef] = {}
        self._jobs: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # -- posting / editing -------------------------------------------------

    async def post(
        self,
        chat_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None,
        *,
        image_url: str | None = None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> CardRef:
        kwargs: dict[str, Any] = {"chat_id": chat_id, "reply_markup": markup, "parse_mode": ParseMode.HTML}
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        try:
            msg = await self._send(text, image_url, kwargs)
        except BadRequest as exc:
            if not has_disabled(markup):
                raise
            # A server that refuses Bot API 10.3 disabled buttons gets the card without them.
            _log.info("card post with disabled buttons refused (%s); posting without them", exc)
            msg = await self._send(text, image_url, {**kwargs, "reply_markup": strip_disabled(markup)})
        ref = CardRef(chat_id=int(chat_id), message_id=int(msg.message_id), image_url=image_url)
        self._last[(ref.chat_id, ref.message_id)] = _fingerprint(text, markup, image_url)
        return ref

    async def _send(self, text: str, image_url: str | None, kwargs: dict[str, Any]) -> Any:
        if image_url:
            return await self.bot.send_photo(photo=image_url, caption=_clip(text, CAPTION_LIMIT), **kwargs)
        return await self.bot.send_message(text=_clip(text, TEXT_LIMIT), link_preview_options=NO_PREVIEW, **kwargs)

    async def _edit_once(
        self, ref: CardRef, text: str, markup: InlineKeyboardMarkup | None, image: str | None
    ) -> None:
        if image:
            if image != ref.image_url:
                await self.bot.edit_message_media(
                    chat_id=ref.chat_id,
                    message_id=ref.message_id,
                    media=InputMediaPhoto(media=image, caption=_clip(text, CAPTION_LIMIT), parse_mode=ParseMode.HTML),
                    reply_markup=markup,
                )
            else:
                await self.bot.edit_message_caption(
                    chat_id=ref.chat_id,
                    message_id=ref.message_id,
                    caption=_clip(text, CAPTION_LIMIT),
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
        else:
            await self.bot.edit_message_text(
                chat_id=ref.chat_id,
                message_id=ref.message_id,
                text=_clip(text, TEXT_LIMIT),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )

    async def edit(
        self,
        ref: CardRef,
        text: str,
        markup: InlineKeyboardMarkup | None,
        *,
        image_url: str | None = None,
    ) -> bool:
        """Edit in place. Returns True when Telegram changed the message.

        Raises :class:`CardGone` when the message can no longer be edited.
        """
        key = (int(ref.chat_id), int(ref.message_id))
        image = image_url if image_url is not None else ref.image_url
        fp = _fingerprint(text, markup, image)
        if self._last.get(key) == fp:
            return False
        # A server that refuses Bot API 10.3 disabled buttons gets the card without them (as before 10.3).
        candidates = [markup, strip_disabled(markup)] if has_disabled(markup) else [markup]
        for i, candidate in enumerate(candidates):
            try:
                await self._edit_once(ref, text, candidate, image)
            except BadRequest as exc:
                if _is_not_modified(exc):
                    self._last[key] = fp
                    return False
                if _is_gone(exc):
                    self._last.pop(key, None)
                    raise CardGone(str(exc)) from exc
                if i + 1 < len(candidates):
                    _log.info("card edit for %s with disabled buttons refused (%s); retrying without them", key, exc)
                    continue
                _log.warning("card edit failed for %s: %s", key, exc)
                return False
            except RetryAfter as exc:
                _log.info("card edit rate-limited for %s; retry in %ss", key, exc.retry_after)
                return False
            except TelegramError as exc:
                _log.warning("card edit failed for %s: %s", key, exc)
                return False
            self._last[key] = fp
            return True
        return False

    def forget(self, ref: CardRef) -> None:
        self._last.pop((int(ref.chat_id), int(ref.message_id)), None)

    # -- refresh jobs ------------------------------------------------------

    def is_refreshing(self, key: str) -> bool:
        return key in self._jobs or key in self._tasks

    def start_refresh(self, key: str, tick: Tick) -> None:
        """Run ``tick`` every ``interval_s`` until it returns False (idempotent per key)."""
        if self.is_refreshing(key):
            return
        if self.job_queue is not None:

            async def _job(context: Any) -> None:
                keep = await self._safe_tick(key, tick)
                if not keep:
                    self.stop_refresh(key)

            self._jobs[key] = self.job_queue.run_repeating(
                _job, interval=self.interval_s, first=self.interval_s, name=f"shfund:refresh:{key}"
            )
            return

        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(self.interval_s)
                    if not await self._safe_tick(key, tick):
                        break
            finally:
                self._tasks.pop(key, None)

        self._tasks[key] = asyncio.get_running_loop().create_task(_loop(), name=f"shfund:refresh:{key}")

    def stop_refresh(self, key: str) -> None:
        job = self._jobs.pop(key, None)
        if job is not None:
            try:
                job.schedule_removal()
            except Exception:  # already removed
                pass
        task = self._tasks.pop(key, None)
        if task is not None and not task.done() and task is not _current_task():
            task.cancel()

    async def _safe_tick(self, key: str, tick: Tick) -> bool:
        try:
            return bool(await tick())
        except CardGone:
            _log.info("card %s is gone; stopping refresh", key)
            return False
        except Exception as exc:  # keep the job alive across transient errors
            _log.warning("card refresh %s failed: %s", key, exc)
            return True


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class ProposalCards:
    """Proposal cards bound to fund state through a :class:`FundGateway`."""

    def __init__(
        self,
        live: LiveCards,
        gateway: FundGateway,
        *,
        base_url: str | None = None,
        people: Callable[[int], Mapping[int, Person]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.live = live
        self.gateway = gateway
        self.base_url = base_url
        self._people = people
        self.clock = clock or live.clock

    @staticmethod
    def key(chat_id: int, proposal_id: str) -> str:
        return f"{int(chat_id)}:{proposal_id}"

    async def _render(self, chat_id: int, view: ProposalView) -> tuple[str, InlineKeyboardMarkup]:
        fund = await maybe_await(self.gateway.fund_for_chat(chat_id))
        people = dict(await maybe_await(self.gateway.people(chat_id)) or {})
        if self._people is not None:
            for k, v in (self._people(chat_id) or {}).items():
                people.setdefault(k, v)
        wood = await maybe_await(self.gateway.wood_usd())
        return render_proposal_card(
            view, fund=fund, people=people, wood_usd=wood, base_url=self.base_url, now=self.clock()
        )

    async def post(
        self, chat_id: int, proposal_id: str, *, reply_to_message_id: int | None = None
    ) -> CardRef | None:
        view = await maybe_await(self.gateway.proposal(chat_id, proposal_id))
        if view is None:
            return None
        text, markup = await self._render(chat_id, view)
        ref = await self.live.post(
            chat_id, text, markup, image_url=view.image_url, reply_to_message_id=reply_to_message_id
        )
        self.live.refs[self.key(chat_id, proposal_id)] = ref
        await maybe_await(self.gateway.set_card_message_id(chat_id, proposal_id, ref.message_id))
        self.watch(chat_id, proposal_id, view)
        return ref

    def _ref_for(self, chat_id: int, view: ProposalView) -> CardRef | None:
        ref = self.live.refs.get(self.key(chat_id, view.id))
        if ref is None and view.card_message_id:
            ref = CardRef(int(chat_id), int(view.card_message_id), view.image_url)
            self.live.refs[self.key(chat_id, view.id)] = ref
        return ref

    async def refresh(self, chat_id: int, proposal_id: str, *, view: ProposalView | None = None) -> bool:
        """Re-render and edit the card now. Returns True when Telegram changed it."""
        if view is None:
            view = await maybe_await(self.gateway.proposal(chat_id, proposal_id))
        if view is None:
            return False
        ref = self._ref_for(chat_id, view)
        if ref is None:
            return False
        text, markup = await self._render(chat_id, view)
        return await self.live.edit(ref, text, markup, image_url=view.image_url)

    def watch(self, chat_id: int, proposal_id: str, view: ProposalView | None = None) -> None:
        """Start the 60 s refresh while the proposal is voting (no-op otherwise)."""
        if view is not None and (view.status or "").lower() != "voting":
            return
        key = self.key(chat_id, proposal_id)

        async def _tick() -> bool:
            current = await maybe_await(self.gateway.proposal(chat_id, proposal_id))
            if current is None:
                return False
            await self.refresh(chat_id, proposal_id, view=current)
            still_voting = (current.status or "").lower() == "voting"
            ends = current.voting_ends_at
            if not still_voting or (ends is not None and self.clock() >= int(ends)):
                return False  # the edit above was the final one
            return True

        self.live.start_refresh(key, _tick)

    def unwatch(self, chat_id: int, proposal_id: str) -> None:
        self.live.stop_refresh(self.key(chat_id, proposal_id))


class PositionWidgets:
    """👤 My position: one member's position as a widget in their DM with the bot.

    ``send`` delivers it; ``refresh`` rewrites the data of the widgets already sent for a fund
    chat when what they show changed. The latest widget per member is kept in memory
    (``sent``: fund chat -> member -> (DM message id, data fingerprint)), so after a restart a
    member's next tap sends a fresh one.
    """

    def __init__(self, gateway: FundGateway, *, base_url: str | None = None, clock: Callable[[], float] = time.time) -> None:
        self.gateway = gateway
        self.base_url = base_url
        self.clock = clock
        self.sent: dict[int, dict[int, tuple[int, str]]] = {}

    async def send(self, bot: Any, dm_chat_id: int, fund: FundView, user_id: int) -> str:
        """Send ``user_id``'s position in ``fund`` to their private chat with the bot.

        A widget first (Bot API 10.4 ``sendWidget`` through ``do_api_request``: PTB 22.8 speaks
        10.0). Telegram shows widgets only in private chats in production, so ``dm_chat_id`` is
        always the member's DM. Any refusal of the widget other than a refused DM (an older Bot
        API answers 404, which PTB raises as ``InvalidToken``; a widget over a limit) sends the
        same plain text as a DM message instead. Returns ``"widget"`` or ``"text"``; raises
        ``Forbidden`` when the bot may not write to this user, or the text send's error.
        """
        now = self.clock()
        pos = build_position(fund, user_id)
        text = position_text(pos, now=now)
        markup = view_fund_markup(fund.vault, self.base_url)
        try:
            sent = await bot.do_api_request(
                "sendWidget",
                api_kwargs={
                    "chat_id": int(dm_chat_id),
                    "html": WIDGET_HTML,
                    "chat_data": widget_data(pos, now=now),
                    "caption": text,  # what clients without widgets show, and the chat-list preview
                    "reply_markup": markup,
                },
            )
        except Forbidden:
            raise
        except Exception as exc:  # noqa: BLE001 - any other refusal of the widget falls back to the text
            _log.info("sendWidget to %s failed (%s); sending the position as text", dm_chat_id, exc)
        else:
            message_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
            if message_id is not None and fund.telegram_chat_id is not None:
                self.note(int(fund.telegram_chat_id), user_id, int(message_id), data_fingerprint(pos))
            return "widget"
        # parse_mode=None: plain text whatever PTB Defaults say, so names and titles show literally.
        await bot.send_message(
            chat_id=int(dm_chat_id), text=text, parse_mode=None, reply_markup=markup, link_preview_options=NO_PREVIEW
        )
        return "text"

    def note(self, chat_id: int, user_id: int, message_id: int, fingerprint: str) -> None:
        """Remember a member's latest widget (one per member; the oldest drops past the cap)."""
        widgets = self.sent.setdefault(int(chat_id), {})
        widgets.pop(int(user_id), None)
        widgets[int(user_id)] = (int(message_id), fingerprint)
        while len(widgets) > WIDGETS_PER_CHAT:
            widgets.pop(next(iter(widgets)))

    async def refresh(self, bot: Any, chat_id: int, fund: FundView | None = None) -> int:
        """Rewrite the data of this fund chat's widgets whose content changed.

        Called where the plugin has just re-read the fund anyway (a proposal card refresh, the
        ``/fund`` card), never on a timer. ``editMessageWidgetData``, then the caption, so clients
        without widgets see the same numbers. A widget that can no longer be edited (deleted, the
        bot blocked) is forgotten. Returns how many changed; never raises.
        """
        widgets = self.sent.get(int(chat_id))
        if not widgets:
            return 0
        try:
            fund = fund if fund is not None else await maybe_await(self.gateway.fund_for_chat(chat_id))
            if fund is None:
                return 0
            markup = view_fund_markup(fund.vault, self.base_url)
            now = self.clock()
            changed = 0
            for user_id, (message_id, last) in list(widgets.items()):
                pos = build_position(fund, user_id)
                fp = data_fingerprint(pos)
                if fp == last:
                    continue
                try:
                    await bot.do_api_request(
                        "editMessageWidgetData",
                        api_kwargs={"chat_id": user_id, "message_id": message_id, "chat_data": widget_data(pos, now=now)},
                    )
                except RetryAfter:
                    break  # the next refresh moment tries again
                except TelegramError as exc:
                    if not _is_not_modified(exc):
                        _log.debug("position widget %s/%s dropped: %s", user_id, message_id, exc)
                        widgets.pop(user_id, None)
                        continue
                widgets[user_id] = (message_id, fp)
                changed += 1
                try:
                    await bot.edit_message_caption(
                        chat_id=user_id, message_id=message_id, caption=position_text(pos, now=now), parse_mode=None,
                        reply_markup=markup,
                    )
                except TelegramError as exc:
                    _log.debug("position widget caption %s/%s not edited: %s", user_id, message_id, exc)
            return changed
        except Exception as exc:  # noqa: BLE001 - a widget refresh never breaks the card that triggered it
            _log.warning("position widget refresh for %s failed: %s", chat_id, exc)
            return 0
