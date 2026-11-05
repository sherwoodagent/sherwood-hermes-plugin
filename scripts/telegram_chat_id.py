#!/usr/bin/env python3
"""Print the chat ids (and titles) the bot has seen, via Bot API ``getUpdates``.

Usage::

    TELEGRAM_BOT_TOKEN=... python scripts/telegram_chat_id.py

Prints one line per chat: ``<chat_id>\t<type>\t<title or name>``. It never
prints the token and never consumes updates (no ``offset`` is sent), so the
Hermes gateway still receives them later.

If nothing is printed:
  * add the bot to the group (the join itself produces a ``my_chat_member``
    update), or send ``/start@<bot_username>`` in the group, then re-run;
  * a 409 Conflict means a webhook is set or another process (for example the
    Hermes gateway) is already polling this token: stop it first, or read the
    id from the gateway log instead.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.telegram.org"

# Update fields that carry a chat, and where the chat sits inside each.
_CHAT_PATHS: tuple[tuple[str, ...], ...] = (
    ("message", "chat"),
    ("edited_message", "chat"),
    ("channel_post", "chat"),
    ("edited_channel_post", "chat"),
    ("my_chat_member", "chat"),
    ("chat_member", "chat"),
    ("chat_join_request", "chat"),
    ("message_reaction", "chat"),
    ("message_reaction_count", "chat"),
    ("callback_query", "message", "chat"),
)


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


def extract_chats(updates: list[dict]) -> dict[int, tuple[str, str]]:
    """Return ``{chat_id: (type, title)}`` for every chat found in ``updates``."""
    chats: dict[int, tuple[str, str]] = {}
    for upd in updates:
        for path in _CHAT_PATHS:
            node: object = upd
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if not isinstance(node, dict) or "id" not in node:
                continue
            title = node.get("title") or " ".join(
                p for p in (node.get("first_name"), node.get("last_name")) if p
            ) or node.get("username") or ""
            chats[int(node["id"])] = (str(node.get("type", "")), str(title))
    return chats


def main() -> int:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 2
    url = f"{API}/bot{token}/getUpdates?timeout=0"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - fixed https host
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = json.loads(exc.read().decode("utf-8")).get("description", "")
        except Exception:
            pass
        print(_redact(f"getUpdates failed: HTTP {exc.code} {body}", token), file=sys.stderr)
        return 1
    except Exception as exc:  # network errors can embed the URL (and so the token)
        print(_redact(f"getUpdates failed: {type(exc).__name__}: {exc}", token), file=sys.stderr)
        return 1

    if not payload.get("ok"):
        print(_redact(f"getUpdates not ok: {payload.get('description', '')}", token), file=sys.stderr)
        return 1

    chats = extract_chats(payload.get("result") or [])
    if not chats:
        print(
            "no chats in pending updates: add the bot to the group or send "
            "/start@<bot_username> there, then re-run",
            file=sys.stderr,
        )
        return 3
    for chat_id, (chat_type, title) in sorted(chats.items()):
        print(f"{chat_id}\t{chat_type}\t{title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
