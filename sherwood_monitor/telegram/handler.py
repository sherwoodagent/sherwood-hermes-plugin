"""PTB handlers for the fund group, wired through Hermes' platform-handler factory.

Hermes calls ``factory(native, adapter)`` when the Telegram adapter connects,
with ``native`` = the PTB ``Application`` (``ctx.register_platform_handler``).
PTB runs the FIRST matching handler per group, and Hermes' core registers
catch-alls in group 0 (``filters.TEXT``, ``filters.COMMAND``, an unscoped
``CallbackQueryHandler``). So:

* group 0 holds only handlers we want to OWN: the ``^shfund:`` callback
  pattern, ``/fund``, ``/leaderboard``, ``/join_fund`` (+ the ``/join-fund``
  spelling) and ``/start sf…`` deep-link payloads in DMs. Hermes runs plugin
  factories before core registers (and hoists late ones ahead of core), so
  these win; everything else falls through to Hermes.
* group ``OBSERVER_GROUP`` (-10) holds observers (pitch / 🔥 reply / "ship it"
  messages and ``message_reaction`` updates). A separate group never
  displaces core, and running before group 0 queues the pitch context before
  the agent turn starts. Observers never raise ``ApplicationHandlerStop``.

Hermes polls with ``allowed_updates=Update.ALL_TYPES``, which includes
``message_reaction``; Telegram still only sends reactions to group admins.
"""
from __future__ import annotations

import functools
import logging
import re
from typing import Any, Awaitable, Callable

from telegram import Message, MessageEntity, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from .cards import (
    HOST_ONLY_ACTIONS,
    parse_cb,
    render_join_prompt,
    render_leaderboard,
    render_personal_link,
)
from .cards_live import NO_PREVIEW, CardGone, CardRef
from .flow import IncomingMessage, ReactionChange
from .links import build_link, decode_start_payload, encode_start_payload
from .runtime import TelegramRuntime
from .types import FundActionError, FundView, Person, maybe_await

_log = logging.getLogger(__name__)

COMMAND_GROUP = 0
OBSERVER_GROUP = -10
CALLBACK_PATTERN = r"^shfund:"
JOIN_FUND_HYPHEN_RE = re.compile(r"^/join-fund(?:@(\w+))?(?:\s|$)", re.IGNORECASE)
START_PAYLOAD_RE = re.compile(r"^/start(?:@\w+)?\s+(sf[a-z]_\S+)\s*$")
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


def _safe(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Log and swallow handler errors (Hermes registers no PTB error handler)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception:
            _log.exception("sherwood telegram handler %s failed", fn.__name__)
            return None

    return wrapper


def person_from_user(user: Any) -> Person | None:
    if user is None:
        return None
    return Person(
        tg_id=int(user.id),
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
    )


def incoming_from_message(message: Message, *, bot_id: int | None, bot_username: str) -> IncomingMessage:
    """Convert a PTB Message into the flow's plain record."""
    text = message.text or message.caption or ""
    entities = message.parse_entities() if message.text else message.parse_caption_entities()
    uname = ("@" + bot_username.lower()) if bot_username else None
    mentions_bot = False
    is_command = False
    for ent, ent_text in entities.items():
        if ent.type == MessageEntity.MENTION and uname and ent_text.lower() == uname:
            mentions_bot = True
        elif ent.type == MessageEntity.TEXT_MENTION and ent.user is not None and bot_id and ent.user.id == bot_id:
            mentions_bot = True
        elif ent.type == MessageEntity.BOT_COMMAND and ent.offset == 0:
            is_command = True
    user = message.from_user
    reply = message.reply_to_message
    reply_from = getattr(reply, "from_user", None) if reply is not None else None
    return IncomingMessage(
        chat_id=int(message.chat.id),
        message_id=int(message.message_id),
        user_id=int(user.id) if user is not None else None,
        text=text,
        is_bot=bool(getattr(user, "is_bot", False)),
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        mentions_bot=mentions_bot,
        reply_to_message_id=int(reply.message_id) if reply is not None else None,
        reply_to_is_bot=bool(reply_from is not None and bot_id is not None and int(reply_from.id) == int(bot_id)),
        thread_id=message.message_thread_id if getattr(message, "is_topic_message", False) else None,
        is_command=is_command,
    )


def _start_payload(kind: str, vault: str, item_id: object | None = None) -> str | None:
    """``/start`` payload for the DM fallback, or None when it would not fit 64 chars."""
    try:
        return encode_start_payload(kind, vault, item_id)
    except ValueError:
        return None


async def _answer(query: Any, text: str | None = None, *, show_alert: bool = False) -> None:
    try:
        await query.answer(text=text[:190] if text else None, show_alert=show_alert)
    except TelegramError as exc:  # too old / already answered
        _log.debug("answer_callback_query failed: %s", exc)


class FundHandlers:
    """The PTB callbacks. Everything reads/writes fund state through the runtime's gateway."""

    def __init__(self, runtime: TelegramRuntime) -> None:
        self.rt = runtime

    # -- helpers -----------------------------------------------------------

    @property
    def settings(self):
        return self.rt.settings

    def _bot_username(self, context: Any) -> str:
        return self.rt.settings.bot_username or str(getattr(context.bot, "username", "") or "")

    def _bot_id(self, context: Any) -> int | None:
        try:
            return int(context.bot.id)
        except Exception:
            return None

    async def _fund_for_chat(self, chat_id: int) -> FundView | None:
        if not self.settings.chat_allowed(chat_id):
            return None
        return await maybe_await(self.rt.gateway.fund_for_chat(chat_id))

    def _secret_or_none(self) -> str | None:
        secret = self.rt.link_secret()
        if not secret:
            _log.warning("SHERWOOD_TG_LINK_SECRET is not set; personal links are disabled")
        return secret

    # -- callbacks -----------------------------------------------------------

    @_safe
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        parsed = parse_cb(query.data or "")
        if parsed is None:
            await _answer(query)
            return
        action, arg = parsed
        user = query.from_user
        msg = query.message
        chat = getattr(msg, "chat", None)
        chat_id = int(chat.id) if chat is not None else None
        if chat_id is None:
            await _answer(query, "This button has expired.")
            return
        self.rt.note_person(chat_id, person_from_user(user))
        fund = await self._fund_for_chat(chat_id)
        if fund is None:
            await _answer(query, "No Sherwood fund is linked to this chat.")
            return
        if action in HOST_ONLY_ACTIONS and (fund.host_tg_id is None or int(user.id) != int(fund.host_tg_id)):
            await _answer(query, "Only the fund host can do that.", show_alert=True)
            return

        handler = {
            "back": self._cb_back,
            "vote": self._cb_vote,
            "deposit": self._cb_fund_link,
            "withdraw": self._cb_fund_link,
            "join": self._cb_fund_link,
            "shipit": self._cb_shipit,
            "propose": self._cb_propose,
            "approve_member": self._cb_host_action,
            "approve_agent": self._cb_host_action,
            "remove_agent": self._cb_host_action,
        }.get(action)
        if handler is None:
            await _answer(query, "Unknown action.")
            return
        await handler(query, context, fund=fund, chat_id=chat_id, action=action, arg=arg)

    async def _cb_back(self, query: Any, context: Any, *, fund: FundView, chat_id: int, action: str, arg: str) -> None:
        user_id = int(query.from_user.id)
        view = await maybe_await(self.rt.gateway.toggle_backer(chat_id, arg, user_id))
        if view is None:
            await _answer(query, "Proposal not found.")
            return
        backing = user_id in set(int(b) for b in view.backers)
        await _answer(
            query,
            "👍 You're backing this. Social only: it doesn't vote." if backing else "Removed your 👍.",
        )
        if self.rt.proposals is None:
            return
        msg_id = getattr(query.message, "message_id", None)
        if msg_id is not None:
            self.rt.live.refs.setdefault(
                self.rt.proposals.key(chat_id, arg), CardRef(chat_id, int(msg_id), view.image_url)
            )
        try:
            await self.rt.proposals.refresh(chat_id, arg, view=view)
        except CardGone:
            pass

    async def _cb_vote(self, query: Any, context: Any, *, fund: FundView, chat_id: int, action: str, arg: str) -> None:
        view = await maybe_await(self.rt.gateway.proposal(chat_id, arg))
        if view is None or (view.status or "").lower() != "voting":
            await _answer(query, "Voting is closed for this proposal.")
            return
        secret = self._secret_or_none()
        if not secret:
            await _answer(query, "Links are not configured yet. Ask the host.", show_alert=True)
            return
        url = build_link(
            "vote",
            tg_user_id=query.from_user.id,
            secret=secret,
            base_url=self.settings.app_base_url,
            governor=view.governor or fund.governor,
            proposal_id=view.id,
        )
        await self.deliver_personal_link(
            query, context, kind="vote", fund=fund, url=url, start_payload=_start_payload("vote", fund.vault, view.id)
        )

    async def _cb_fund_link(self, query: Any, context: Any, *, fund: FundView, chat_id: int, action: str, arg: str) -> None:
        if arg.lower() != fund.vault.lower():
            await _answer(query, "This button belongs to another fund.")
            return
        secret = self._secret_or_none()
        if not secret:
            await _answer(query, "Links are not configured yet. Ask the host.", show_alert=True)
            return
        url = build_link(
            action,
            tg_user_id=query.from_user.id,
            secret=secret,
            base_url=self.settings.app_base_url,
            fund=fund.vault,
        )
        await self.deliver_personal_link(
            query, context, kind=action, fund=fund, url=url, start_payload=_start_payload(action, fund.vault)
        )

    async def _cb_shipit(self, query: Any, context: Any, *, fund: FundView, chat_id: int, action: str, arg: str) -> None:
        try:
            toast = await maybe_await(self.rt.gateway.ship_draft(chat_id, arg, int(query.from_user.id)))
        except FundActionError as exc:
            await _answer(query, str(exc) or "Could not ship this draft.", show_alert=True)
            return
        await _answer(query, toast or "🚀 Shipped.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError as exc:
            _log.debug("could not clear draft buttons: %s", exc)

    async def _cb_propose(self, query: Any, context: Any, *, fund: FundView, chat_id: int, action: str, arg: str) -> None:
        url = await maybe_await(self.rt.gateway.host_signing_url(chat_id, arg, int(query.from_user.id)))
        if not url:
            await _answer(query, "The signing page isn't ready for this draft yet.", show_alert=True)
            return
        await self.deliver_personal_link(
            query, context, kind="propose", fund=fund, url=url, start_payload=_start_payload("propose", fund.vault, arg)
        )

    async def _cb_host_action(self, query: Any, context: Any, *, fund: FundView, chat_id: int, action: str, arg: str) -> None:
        by = int(query.from_user.id)
        gw = self.rt.gateway
        try:
            if action == "approve_member":
                toast = await maybe_await(gw.approve_member(chat_id, int(arg), by))
                stamp = "✅ Approved"
            elif action == "approve_agent":
                toast = await maybe_await(gw.approve_agent(chat_id, arg, by))
                stamp = "✅ Agent approved"
            else:
                toast = await maybe_await(gw.remove_agent(chat_id, arg, by))
                stamp = "🗑 Agent removed"
        except FundActionError as exc:
            await _answer(query, str(exc) or "That didn't work.", show_alert=True)
            return
        except ValueError:
            await _answer(query, "Bad button data.")
            return
        await _answer(query, toast or stamp)
        host = person_from_user(query.from_user)
        base = getattr(query.message, "text_html", None) or ""
        try:
            await query.edit_message_text(
                text=f"{base}\n\n<i>{stamp} by {host.handle if host else 'host'}</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramError as exc:
            _log.debug("could not stamp host action: %s", exc)

    async def deliver_personal_link(
        self, query: Any, context: Any, *, kind: str, fund: FundView, url: str, start_payload: str | None
    ) -> str:
        """Hand one user their signed URL button without showing it to the group.

        1. ``ephemeral``: an ephemeral group message (Bot API 10.3
           ``ephemeral_message_parameters``, within 15 s of the tap; PTB passes
           it via ``api_kwargs``) that only this user sees;
        2. a DM, when the user has started the bot before;
        3. ``answer_callback_query(url=t.me/<bot>?start=<payload>)``, which opens
           the bot's DM; ``/start <payload>`` then sends the link there.
        Returns which path delivered it.
        """
        text, markup = render_personal_link(kind, url, fund.name)
        bot = context.bot
        user_id = int(query.from_user.id)
        chat = getattr(query.message, "chat", None)
        username = self._bot_username(context)
        if self.settings.personal_link_mode == "ephemeral" and chat is not None and chat.type in GROUP_TYPES:
            thread_id = None
            if getattr(query.message, "is_topic_message", False):
                thread_id = getattr(query.message, "message_thread_id", None)
            try:
                await bot.send_message(
                    chat_id=chat.id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    message_thread_id=thread_id,
                    link_preview_options=NO_PREVIEW,
                    api_kwargs={
                        "ephemeral_message_parameters": {
                            "receiver_user_id": user_id,
                            "callback_query_id": query.id,
                        }
                    },
                )
                await _answer(query)
                return "ephemeral"
            except TelegramError as exc:
                _log.info("ephemeral link failed (%s); trying DM", exc)
        try:
            await bot.send_message(
                chat_id=user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NO_PREVIEW
            )
            await _answer(query, f"Sent your private link in our DM chat (@{username}).")
            return "dm"
        except (Forbidden, BadRequest) as exc:
            _log.debug("DM to %s failed: %s", user_id, exc)
        if username and start_payload:
            try:
                await query.answer(url=f"https://t.me/{username}?start={start_payload}")
                return "start_link"
            except TelegramError as exc:
                _log.debug("start-link answer failed: %s", exc)
        await _answer(query, f"Open a chat with @{username} and press Start, then tap again.", show_alert=True)
        return "failed"

    # -- commands ------------------------------------------------------------

    @_safe
    async def cmd_fund(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        chat_id = int(msg.chat.id)
        fund = await self._fund_for_chat(chat_id)
        if fund is None:
            if self.settings.chat_allowed(chat_id):
                await msg.reply_text("No Sherwood fund is linked to this chat yet.")
            return
        from .cards import render_fund_card

        caps = await self.rt.capabilities(chat_id) if self.rt.connected else None
        text, markup = render_fund_card(
            fund,
            people=await self.rt.people(chat_id),
            base_url=self.settings.app_base_url,
            now=self.rt.clock(),
            notice=caps.notice if caps else None,
        )
        sent = await msg.reply_html(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        if sent is not None and getattr(sent, "message_id", None) is not None:
            self.rt.flow.note_bot_card(chat_id, int(sent.message_id))

    @_safe
    async def cmd_leaderboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        chat_id = int(msg.chat.id)
        fund = await self._fund_for_chat(chat_id)
        if fund is None:
            return
        rows = await maybe_await(self.rt.gateway.leaderboard(chat_id)) or []
        text, markup = render_leaderboard(
            rows, fund=fund, people=await self.rt.people(chat_id), base_url=self.settings.app_base_url
        )
        sent = await msg.reply_html(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        if sent is not None and getattr(sent, "message_id", None) is not None:
            self.rt.flow.note_bot_card(chat_id, int(sent.message_id))

    @_safe
    async def cmd_join_fund(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        m = JOIN_FUND_HYPHEN_RE.match(msg.text or "")
        if m and m.group(1) and m.group(1).lower() != self._bot_username(context).lower():
            return  # /join-fund@someotherbot
        chat_id = int(msg.chat.id)
        fund = await self._fund_for_chat(chat_id)
        if fund is None:
            return
        text, markup = render_join_prompt(fund, base_url=self.settings.app_base_url)
        sent = await msg.reply_html(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        if sent is not None and getattr(sent, "message_id", None) is not None:
            self.rt.flow.note_bot_card(chat_id, int(sent.message_id))

    @_safe
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """``/start sf<k>_<vault>[_<id>]`` in a DM: send the personal link there."""
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return
        m = START_PAYLOAD_RE.match(msg.text or "")
        decoded = decode_start_payload(m.group(1)) if m else None
        if decoded is None:
            return
        kind, vault, item = decoded
        fund = await maybe_await(self.rt.gateway.fund_by_vault(vault))
        if fund is None or (fund.telegram_chat_id is not None and not self.settings.chat_allowed(fund.telegram_chat_id)):
            await msg.reply_text("That fund isn't available here.")
            return
        if kind == "propose":
            if fund.host_tg_id is None or int(user.id) != int(fund.host_tg_id):
                await msg.reply_text("Only the fund host can co-sign.")
                return
            url = await maybe_await(
                self.rt.gateway.host_signing_url(int(fund.telegram_chat_id or 0), str(item), int(user.id))
            )
            if not url:
                await msg.reply_text("The signing page isn't ready for this draft yet.")
                return
        else:
            secret = self._secret_or_none()
            if not secret:
                await msg.reply_text("Links are not configured yet. Ask the host.")
                return
            if kind == "vote":
                url = build_link(
                    "vote",
                    tg_user_id=user.id,
                    secret=secret,
                    base_url=self.settings.app_base_url,
                    governor=fund.governor,
                    proposal_id=item,
                )
            else:
                url = build_link(
                    kind, tg_user_id=user.id, secret=secret, base_url=self.settings.app_base_url, fund=fund.vault
                )
        text, markup = render_personal_link(kind, url, fund.name)
        await msg.reply_html(text, reply_markup=markup, link_preview_options=NO_PREVIEW)

    # -- observers ---------------------------------------------------------------

    @_safe
    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None or message.chat.type not in GROUP_TYPES:
            return
        chat_id = int(message.chat.id)
        if not self.settings.chat_allowed(chat_id):
            return
        self.rt.note_person(chat_id, person_from_user(message.from_user))
        incoming = incoming_from_message(
            message, bot_id=self._bot_id(context), bot_username=self._bot_username(context)
        )
        await self.rt.flow.on_message(incoming)

    @_safe
    async def on_reaction(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        mr = update.message_reaction
        if mr is None or mr.chat.type not in GROUP_TYPES:
            return
        chat_id = int(mr.chat.id)
        if not self.settings.chat_allowed(chat_id):
            return
        user = mr.user
        self.rt.note_person(chat_id, person_from_user(user))
        change = ReactionChange(
            chat_id=chat_id,
            message_id=int(mr.message_id),
            user_id=int(user.id) if user is not None else None,
            old=tuple(getattr(r, "emoji", "") or "" for r in (mr.old_reaction or ())),
            new=tuple(getattr(r, "emoji", "") or "" for r in (mr.new_reaction or ())),
            user_is_bot=bool(getattr(user, "is_bot", False)),
        )
        await self.rt.flow.on_reaction(change)


def build_handlers(runtime: TelegramRuntime) -> list[tuple[BaseHandler, int]]:
    """All handlers with their PTB groups (exposed for tests)."""
    h = FundHandlers(runtime)
    groups = filters.ChatType.GROUPS
    return [
        (CallbackQueryHandler(h.on_callback, pattern=CALLBACK_PATTERN), COMMAND_GROUP),
        (CommandHandler("fund", h.cmd_fund, filters=groups), COMMAND_GROUP),
        (CommandHandler("leaderboard", h.cmd_leaderboard, filters=groups), COMMAND_GROUP),
        (CommandHandler("join_fund", h.cmd_join_fund, filters=groups), COMMAND_GROUP),
        # Telegram commands cannot contain '-', so "/join-fund" arrives as "/join"
        # plus text; catch the literal spelling before Hermes' COMMAND catch-all.
        (MessageHandler(groups & filters.Regex(JOIN_FUND_HYPHEN_RE), h.cmd_join_fund), COMMAND_GROUP),
        (
            CommandHandler("start", h.cmd_start, filters=filters.ChatType.PRIVATE & filters.Regex(START_PAYLOAD_RE)),
            COMMAND_GROUP,
        ),
        (
            MessageReactionHandler(
                h.on_reaction, message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED
            ),
            OBSERVER_GROUP,
        ),
        (
            MessageHandler(groups & (filters.TEXT | filters.CAPTION) & filters.UpdateType.MESSAGE, h.on_group_message),
            OBSERVER_GROUP,
        ),
    ]


def wire(application: Any, runtime: TelegramRuntime) -> list[tuple[BaseHandler, int]]:
    """Attach the runtime and register every handler on the PTB Application."""
    runtime.attach(application)
    handlers = build_handlers(runtime)
    for handler, group in handlers:
        application.add_handler(handler, group=group)
    return handlers
