#!/usr/bin/env python3
"""Live smoke test for the fund cards against a real Telegram group.

    TELEGRAM_BOT_TOKEN=... TELEGRAM_TEST_CHAT_ID=-100... python scripts/telegram_smoke.py

Posts a sample proposal card, edits it twice in place (veto meter, then
countdown), posts a fund card and a settle receipt, prints the message ids and
the bot's group capabilities, and exits. ``--dry-run`` renders the same cards
to stdout without touching the network. The token is never printed.

Needs ``pip install 'python-telegram-bot>=22,<23'`` (or this repo's ``[dev]``
extra; inside Hermes, its Telegram extra provides it). Sample addresses are placeholders, so the View buttons
open a fund page that does not exist.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sherwood_monitor.telegram.cards import (  # noqa: E402
    render_fund_card,
    render_proposal_card,
    render_settle_receipt,
)
from sherwood_monitor.telegram.types import (  # noqa: E402
    FundView,
    Leg,
    Person,
    Position,
    ProposalView,
    Receipt,
)

SAMPLE_VAULT = "0x1111111111111111111111111111111111111111"
SAMPLE_GOV = "0x2222222222222222222222222222222222222222"
WOOD_USD = 0.25
PEOPLE = {101: Person(101, "alice"), 102: Person(102, "bob"), 103: Person(103, None, "Carol", "Smith")}


def sample(now: float) -> tuple[FundView, ProposalView, Receipt]:
    proposal = ProposalView(
        id="1",
        governor=SAMPLE_GOV,
        proposer="atlas",
        thesis="Smoke test: long NVDA into earnings, sized small, stop at -15%.",
        legs=(Leg("NVDA", 6000), Leg("AMD", 4000)),
        max_capital=3000,
        max_drawdown_bps=1500,
        bond_wood=12.4,
        status="voting",
        backers=(102, 103),
        idea_by=101,
        voting_ends_at=int(now) + 30 * 60,
        against_bps=200,
    )
    fund = FundView(
        vault=SAMPLE_VAULT,
        governor=SAMPLE_GOV,
        name="Smoke Test Fund",
        host_tg_id=101,
        agents=("atlas",),
        nav=10000,
        pnl_bps=820,
        positions=(Position("NVDA", 4000, 310), Position("USDC", 6000)),
        open_proposals=(proposal,),
    )
    receipt = Receipt(proposal_id="1", pnl_bps=820, symbol="NVDA", idea_by=101, backers=5)
    return fund, proposal, receipt


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


async def run(token: str, chat_id: int, base_url: str | None, pause: float) -> int:
    from dataclasses import replace

    from telegram import Bot

    from sherwood_monitor.telegram.cards_live import LiveCards

    now = time.time()
    fund, proposal, receipt = sample(now)
    async with Bot(token) as bot:
        me = bot.bot
        print(f"bot @{me.username} id={me.id} can_read_all_group_messages={me.can_read_all_group_messages}")
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
            print(f"chat {chat_id}: bot status={member.status}")
        except Exception as exc:
            print(f"chat {chat_id}: get_chat_member failed: {type(exc).__name__}: {_redact(str(exc), token)}")

        live = LiveCards(bot)
        text, kb = render_proposal_card(proposal, fund=fund, people=PEOPLE, wood_usd=WOOD_USD, base_url=base_url, now=now)
        ref = await live.post(chat_id, text, kb)
        print(f"posted proposal card message_id={ref.message_id}")

        await asyncio.sleep(pause)
        p1 = replace(proposal, against_bps=500)
        text, kb = render_proposal_card(p1, fund=fund, people=PEOPLE, wood_usd=WOOD_USD, base_url=base_url, now=now)
        print(f"edit 1 (veto meter 5.0%): changed={await live.edit(ref, text, kb)}")

        await asyncio.sleep(pause)
        p2 = replace(p1, against_bps=750, backers=p1.backers + (101,))
        later = now + 6 * 60
        text, kb = render_proposal_card(p2, fund=fund, people=PEOPLE, wood_usd=WOOD_USD, base_url=base_url, now=later)
        print(f"edit 2 (countdown 24m, 7.5%): changed={await live.edit(ref, text, kb)}")
        print(f"edit 2 again (idempotent): changed={await live.edit(ref, text, kb)}")

        text, kb = render_fund_card(fund, people=PEOPLE, base_url=base_url, now=later)
        fund_ref = await live.post(chat_id, text, kb)
        print(f"posted fund card message_id={fund_ref.message_id}")

        text, kb = render_settle_receipt(receipt, people=PEOPLE, fund=fund, base_url=base_url)
        receipt_ref = await live.post(chat_id, text, kb)
        print(f"posted receipt message_id={receipt_ref.message_id}")
    return 0


def dry_run(base_url: str | None) -> int:
    now = time.time()
    fund, proposal, receipt = sample(now)
    for title, (text, kb) in (
        ("proposal", render_proposal_card(proposal, fund=fund, people=PEOPLE, wood_usd=WOOD_USD, base_url=base_url, now=now)),
        ("fund", render_fund_card(fund, people=PEOPLE, base_url=base_url, now=now)),
        ("receipt", render_settle_receipt(receipt, people=PEOPLE, fund=fund, base_url=base_url)),
    ):
        print(f"--- {title} ---\n{text}\n{kb.to_dict()}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="render to stdout, no network")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between edits")
    args = ap.parse_args(argv)
    base_url = (os.environ.get("APP_BASE_URL") or "").strip() or None
    if args.dry_run:
        return dry_run(base_url)

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_raw = (os.environ.get("TELEGRAM_TEST_CHAT_ID") or "").strip()
    if not token or not chat_raw:
        print("set TELEGRAM_BOT_TOKEN and TELEGRAM_TEST_CHAT_ID (or pass --dry-run)", file=sys.stderr)
        return 2
    try:
        chat_id = int(chat_raw)
    except ValueError:
        print("TELEGRAM_TEST_CHAT_ID must be a number like -1001234567890", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(token, chat_id, base_url, args.pause))
    except Exception as exc:  # httpx errors can embed the request URL, which holds the token
        print(f"smoke failed: {type(exc).__name__}: {_redact(str(exc), token)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
