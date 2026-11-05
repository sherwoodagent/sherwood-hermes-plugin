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
from telegram.error import BadRequest, RetryAfter, TelegramError

from .cards import render_proposal_card
from .types import FundGateway, Person, ProposalView, maybe_await

_log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
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
        if image_url:
            msg = await self.bot.send_photo(photo=image_url, caption=_clip(text, CAPTION_LIMIT), **kwargs)
        else:
            msg = await self.bot.send_message(
                text=_clip(text, TEXT_LIMIT), link_preview_options=NO_PREVIEW, **kwargs
            )
        ref = CardRef(chat_id=int(chat_id), message_id=int(msg.message_id), image_url=image_url)
        self._last[(ref.chat_id, ref.message_id)] = _fingerprint(text, markup, image_url)
        return ref

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
        try:
            if image:
                if image != ref.image_url:
                    await self.bot.edit_message_media(
                        chat_id=ref.chat_id,
                        message_id=ref.message_id,
                        media=InputMediaPhoto(
                            media=image, caption=_clip(text, CAPTION_LIMIT), parse_mode=ParseMode.HTML
                        ),
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
        except BadRequest as exc:
            if _is_not_modified(exc):
                self._last[key] = fp
                return False
            if _is_gone(exc):
                self._last.pop(key, None)
                raise CardGone(str(exc)) from exc
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
