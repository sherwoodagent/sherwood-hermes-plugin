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
from datetime import timedelta
from typing import Any, Awaitable, Callable, Mapping

from telegram import InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError, TimedOut

from .cards import has_disabled, render_proposal_card, strip_disabled, view_fund_markup
from .types import FundGateway, FundView, Person, ProposalView, maybe_await
from .widget import (
    WIDGET_HTML,
    build_position,
    data_fingerprint,
    is_member_or_host,
    position_text,
    widget_data,
)

_log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
# 👤 My position widgets kept per fund chat for refreshes (the latest tappers; older ones stay as sent).
WIDGETS_PER_CHAT = 50
# Widget data edits per refresh pass. A pass the cap cuts short is continued by a follow-up pass
# after WIDGET_FOLLOW_UP_S; one cut short by RetryAfter after Telegram's wait (at most
# RETRY_AFTER_MAX_S). At most WIDGET_FOLLOW_UPS_MAX delayed follow-ups run in a row.
WIDGET_EDITS_PER_PASS = 20
WIDGET_FOLLOW_UP_S = 1.0
RETRY_AFTER_MAX_S = 60.0
WIDGET_FOLLOW_UPS_MAX = 10
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

    ``send`` delivers it. ``request`` asks for a refresh pass over the widgets already sent for a
    fund chat and returns at once: the pass runs as its own task, one at a time per chat (requests
    made while one runs collapse into a single follow-up), so it never holds a card lock or delays
    a handler. A pass rewrites only widgets whose values changed, at most ``max_calls`` of them,
    with ``editMessageWidgetData`` (the caption is a dated snapshot and is not edited). Every widget
    a pass tries goes to the back of the queue, so the next pass starts with the ones not yet
    served; a pass the cap or ``RetryAfter`` cut short is continued after a short wait. The latest
    widget per member or host is kept in memory (``sent``: fund chat -> member -> (DM message id,
    data fingerprint)), so after a restart a member's next tap sends a fresh one.
    """

    def __init__(
        self,
        gateway: FundGateway,
        *,
        base_url: str | None = None,
        clock: Callable[[], float] = time.time,
        max_calls: int = WIDGET_EDITS_PER_PASS,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.gateway = gateway
        self.base_url = base_url
        self.clock = clock
        self.max_calls = max_calls
        self.sleep = sleep  # the wait before a follow-up pass (tests replace it)
        self.sent: dict[int, dict[int, tuple[int, str]]] = {}
        self._passes: dict[int, asyncio.Task] = {}
        self._again: set[int] = set()

    async def send(self, bot: Any, dm_chat_id: int, fund: FundView, user_id: int) -> str:
        """Send ``user_id``'s position in ``fund`` to their private chat with the bot.

        A widget first (Bot API 10.4 ``sendWidget`` through ``do_api_request``: PTB 22.8 speaks
        10.0). Telegram shows widgets only in private chats in production, so ``dm_chat_id`` is
        always the member's DM. A refusal of the widget (an older Bot API answers 404, which PTB
        raises as ``EndPointNotFound``; a widget over a limit) sends the same plain text as a DM
        message instead. Returns ``"widget"`` or ``"text"``.

        Raises without stepping down: ``Forbidden`` (the bot may not write to this user yet),
        ``TimedOut`` (the widget may have arrived: a text copy could be a duplicate) and
        ``RetryAfter`` (Telegram asks to wait), and the text send's error. Only a member or the
        host is remembered for refreshes.
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
        except (Forbidden, TimedOut, RetryAfter):
            raise
        except Exception as exc:  # noqa: BLE001 - any other refusal of the widget falls back to the text
            _log.info("sendWidget to %s failed (%s); sending the position as text", dm_chat_id, exc)
        else:
            message_id = sent.get("message_id") if isinstance(sent, dict) else getattr(sent, "message_id", None)
            if message_id is not None and fund.telegram_chat_id is not None and is_member_or_host(fund, user_id):
                self.note(int(fund.telegram_chat_id), user_id, int(message_id), data_fingerprint(pos))
            return "widget"
        # parse_mode=None: plain text whatever PTB Defaults say, so names and titles show literally.
        await bot.send_message(
            chat_id=int(dm_chat_id), text=text, parse_mode=None, reply_markup=markup, link_preview_options=NO_PREVIEW
        )
        return "text"

    def note(self, chat_id: int, user_id: int, message_id: int, fingerprint: str) -> None:
        """Remember a member's latest widget (one per member). Past the cap, the widget least recently
        tapped or tried by a refresh drops."""
        widgets = self.sent.setdefault(int(chat_id), {})
        widgets.pop(int(user_id), None)
        widgets[int(user_id)] = (int(message_id), fingerprint)
        while len(widgets) > WIDGETS_PER_CHAT:
            widgets.pop(next(iter(widgets)))

    def request(self, bot: Any, chat_id: int, fund: FundView | None = None) -> asyncio.Task | None:
        """Ask for a refresh pass of this fund chat's widgets; never waits for it.

        Returns the task running the passes (None when there is nothing to refresh). A request made
        while a pass runs (or waits to continue) collapses into one follow-up pass, which reads the
        stored fund; a ``fund`` passed with it is not used.
        """
        chat = int(chat_id)
        if not self.sent.get(chat):
            return None
        running = self._passes.get(chat)
        if running is not None and not running.done():
            self._again.add(chat)
            return running
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self._run_passes(bot, chat, fund), name=f"shfund:widgets:{chat}")
        self._passes[chat] = task
        return task

    async def idle(self) -> None:
        """Wait until no refresh pass is running or waiting (tests)."""
        while self._passes:
            await asyncio.gather(*list(self._passes.values()), return_exceptions=True)

    def cancel(self) -> None:
        """Cancel every pending refresh pass (plugin shutdown). Safe from any thread."""
        self._again.clear()
        for task in list(self._passes.values()):
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:  # its loop is already closed: the task died with it
                pass
        self._passes.clear()

    async def _run_passes(self, bot: Any, chat: int, fund: FundView | None) -> None:
        """Passes for one chat, one at a time: a follow-up after a pass that was cut short (after
        its wait) or that a request arrived during; then done."""
        try:
            _, wait = await self._pass(bot, chat, fund)
            delayed = 0
            while True:
                if wait is not None:
                    if delayed >= WIDGET_FOLLOW_UPS_MAX:
                        self._again.discard(chat)  # the next request starts over
                        break
                    delayed += 1
                    await self.sleep(wait)
                elif chat not in self._again:
                    break
                self._again.discard(chat)
                _, wait = await self._pass(bot, chat)
        except Exception:  # noqa: BLE001 - a broken pass is logged, and the chat is not left queued
            _log.exception("position widget refresh for %s failed", chat)
            self._again.discard(chat)
        finally:
            if self._passes.get(chat) is asyncio.current_task():
                self._passes.pop(chat, None)

    def _restore(self, widgets: dict[int, tuple[int, str]], user_id: int, entry: tuple[int, str], last: str) -> None:
        """Put the old fingerprint back so a later pass retries (unless a new tap replaced the widget)."""
        if widgets.get(user_id) == entry:
            widgets[user_id] = (entry[0], last)

    def _forget(self, widgets: dict[int, tuple[int, str]], user_id: int, entry: tuple[int, str]) -> None:
        if widgets.get(user_id) == entry:
            widgets.pop(user_id, None)

    async def refresh(self, bot: Any, chat_id: int, fund: FundView | None = None) -> int:
        """One pass, without a follow-up: how many widgets were rewritten (see ``_pass``)."""
        changed, _ = await self._pass(bot, chat_id, fund)
        return changed

    async def _pass(self, bot: Any, chat_id: int, fund: FundView | None = None) -> tuple[int, float | None]:
        """Rewrite the data of this fund chat's widgets whose values changed, oldest-served first.

        At most ``max_calls`` edits. Each widget tried goes to the back of the queue, so the next
        pass starts with the ones this pass did not reach. The new fingerprint is recorded before
        the edit. A widget is forgotten only when it is gone (deleted, not editable, the chat gone,
        the bot blocked); on ``TimedOut`` / ``NetworkError`` it keeps its old fingerprint for a
        later pass. Returns how many widgets were rewritten, and None when the pass got through or
        else how long to wait before a follow-up (the cap: ``WIDGET_FOLLOW_UP_S``; ``RetryAfter``:
        Telegram's wait, bounded). Raises only on an unexpected error (``_run_passes`` logs it).
        """
        widgets = self.sent.get(int(chat_id))
        if not widgets:
            return 0, None
        fund = fund if fund is not None else await maybe_await(self.gateway.fund_for_chat(chat_id))
        if fund is None:
            return 0, None
        now = self.clock()
        calls = changed = 0
        for user_id, (message_id, last) in list(widgets.items()):
            if widgets.get(user_id) != (message_id, last):
                continue  # re-tapped or forgotten while this pass ran
            try:
                pos = build_position(fund, user_id)
                fp = data_fingerprint(pos)
                data = widget_data(pos, now=now)
            except Exception as exc:  # noqa: BLE001 - one member's data never stops the others
                _log.warning("position widget %s/%s not built: %s", user_id, message_id, exc)
                continue
            if fp == last:
                continue
            if calls >= self.max_calls:
                return changed, WIDGET_FOLLOW_UP_S  # changed widgets left: a follow-up continues here
            entry = (message_id, fp)
            widgets.pop(user_id, None)
            widgets[user_id] = entry  # to the back; fingerprint before the await (no repeat edits)
            calls += 1
            try:
                await bot.do_api_request(
                    "editMessageWidgetData", api_kwargs={"chat_id": user_id, "message_id": message_id, "chat_data": data}
                )
            except RetryAfter as exc:
                self._restore(widgets, user_id, entry, last)
                wait = _retry_seconds(exc)
                _log.info("position widget refresh for %s rate-limited; next pass in %.0fs", chat_id, wait)
                return changed, wait
            except Forbidden as exc:  # the bot was blocked or the user is gone
                _log.debug("position widget %s/%s forgotten: %s", user_id, message_id, exc)
                self._forget(widgets, user_id, entry)
                continue
            except BadRequest as exc:
                if _is_not_modified(exc):
                    changed += 1
                elif _is_gone(exc):
                    _log.debug("position widget %s/%s forgotten: %s", user_id, message_id, exc)
                    self._forget(widgets, user_id, entry)
                else:  # refused data: keep the widget, try again when the values change
                    _log.info("position widget %s/%s edit refused: %s", user_id, message_id, exc)
                continue
            except TelegramError as exc:  # TimedOut, NetworkError, a 5xx: keep it for a later pass
                _log.info("position widget %s/%s edit failed, kept: %s", user_id, message_id, exc)
                self._restore(widgets, user_id, entry, last)
                continue
            changed += 1
        return changed, None


def _retry_seconds(exc: RetryAfter) -> float:
    """Telegram's ``RetryAfter`` wait in seconds, bounded to [1, ``RETRY_AFTER_MAX_S``]. PTB 22.2+ keeps
    it as a timedelta in ``_retry_after``; the public attribute warns about its type change."""
    value: Any = getattr(exc, "_retry_after", None)
    if not isinstance(value, timedelta):
        value = exc.retry_after
    try:
        seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    except (TypeError, ValueError):
        seconds = RETRY_AFTER_MAX_S
    return min(max(seconds, 1.0), RETRY_AFTER_MAX_S)
