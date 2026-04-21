"""EventRouter: decode a raw record and dispatch to the right handler.

NOTE: The Supervisor continues to consume both chain and xmtp lines from
``sherwood session check --stream``.  The CLI's ``session check --stream``
has no ``--no-xmtp`` flag (adding one is a follow-up CLI change).  On hosts
where XMTP works in the CLI the Supervisor gets XMTP events from the CLI
stream; the sidecar may emit the same events via its own stream.  The LRU
dedupe below drops duplicates at the Router level so they are never
double-delivered to the EventBuffer / handler layer.  Chain events are not
deduped here because they use (block, tx) as a natural identifier and the
CLI stream is the sole chain source.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict, defaultdict
from typing import Any

from .config import Config
from .event_buffer import EventBuffer
from .handlers import PostFn, handle_chain_event, handle_xmtp_message
from .models import ChainEvent, SessionMessage, decode_record

_log = logging.getLogger(__name__)

_DEDUPE_CAPACITY = 128  # per subdomain


class EventRouter:
    def __init__(self, buffer: EventBuffer, cfg: Config, post_fn: PostFn) -> None:
        self._buffer = buffer
        self._cfg = cfg
        self._post_fn = post_fn
        self._events_seen: dict[str, int] = defaultdict(int)
        self._last_event_at: dict[str, float] = {}
        # Bounded LRU per subdomain: dict[subdomain, OrderedDict[message_id, None]]
        self._seen_xmtp: dict[str, OrderedDict[str, None]] = defaultdict(OrderedDict)

    async def route(self, subdomain: str, raw: Any) -> None:
        try:
            rec = decode_record(raw)
        except ValueError as exc:
            _log.warning("decode error on %s: %s", subdomain, exc)
            return

        # Dedupe XMTP messages by message_id across dual-path delivery
        # (CLI stream + sidecar stream).  Drop the duplicate without counting it.
        if isinstance(rec, SessionMessage):
            seen = self._seen_xmtp[subdomain]
            if rec.id in seen:
                seen.move_to_end(rec.id)  # refresh recency
                return
            seen[rec.id] = None
            if len(seen) > _DEDUPE_CAPACITY:
                seen.popitem(last=False)  # evict oldest

        self._events_seen[subdomain] += 1
        self._last_event_at[subdomain] = time.time()

        try:
            if isinstance(rec, ChainEvent):
                await handle_chain_event(subdomain, rec, self._buffer, self._cfg, self._post_fn)
            elif isinstance(rec, SessionMessage):
                await handle_xmtp_message(subdomain, rec, self._buffer, self._cfg, self._post_fn)
        except Exception as exc:
            _log.exception("handler error on %s: %s", subdomain, exc)

    def events_seen(self, subdomain: str) -> int:
        return self._events_seen.get(subdomain, 0)

    def last_event_at(self, subdomain: str) -> float | None:
        return self._last_event_at.get(subdomain)
