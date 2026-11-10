"""Pure card renderers: each returns ``(text_html, InlineKeyboardMarkup)``.

No I/O. Every user-supplied string goes through ``html.escape``. Money actions
are plain URL buttons (or callbacks that hand back a personal URL button);
there is no Mini App anywhere (Telegram Bot Developer Terms 7.1-7.4).

Callback data (``shfund:<action>:<arg>``, at most 64 bytes):

    back:<pid>            👍 Back it, social only (anyone)
    vote:<pid>            ❌ Object, hands back a personal /tg/vote link (anyone)
    deposit:<vault>       personal /tg/deposit link (anyone)
    withdraw:<vault>      personal /tg/withdraw link (anyone)
    join:<vault>          personal /tg/join link (anyone); with open deposits the /tg/deposit link too
    position:<vault>      the tapper's "My position" widget in their DM with the bot (anyone)
    shipit:<draft>        host only (proposal_signer: agent: the card then shows the CLI propose command)
    propose:<draft>       host only: hands back the host signing page link
    approve_member:<tg>   host only (then hands the host a /tg/sign link; open deposits: nothing to approve)
    approve_agent:<name>  host only (then hands the host a /tg/sign link; agent signer: no link)
    remove_agent:<name>   host only
"""
from __future__ import annotations

import html
import math
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .links import fund_page_url, link_ttl_s
from .types import (
    FundView,
    LeaderRow,
    MemberView,
    Person,
    ProposalView,
    Receipt,
)

CALLBACK_PREFIX = "shfund"
CALLBACK_MAX_BYTES = 64
HOST_ONLY_ACTIONS = frozenset({"shipit", "propose", "approve_member", "approve_agent", "remove_agent"})
PERSONAL_LINK_ACTIONS = frozenset({"vote", "deposit", "withdraw", "join"})

# A proposal card past voting keeps one greyed-out button where 👍 / ❌ were (Bot API 10.3
# DisabledButton), so the row does not vanish and shift 📊 View fund under a tapping finger.
CLOSED_PROPOSAL_LABELS = {
    "passed": "🗳 Voting closed",
    "review": "🗳 Voting closed",
    "approved": "🗳 Voting closed",
    "executed": "🗳 Voting closed",
    "settled": "🗳 Voting closed",
    "vetoed": "⛔ Vetoed",
    "cancelled": "✖️ Cancelled",
}
SHIPPED_LABEL = "🚀 Shipped"

METER_CELLS = 8
GOAL_CELLS = 10
FILLED = "▮"
EMPTY = "░"
MAX_AVATARS = 5
THESIS_MAX = 600

STATUS_CHIPS = {
    "draft": "📝 DRAFT",
    "proposed": "📨 PROPOSED",
    "voting": "🗳 VOTING",
    "passed": "✅ PASSED",
    "review": "🛡 GUARDIAN REVIEW",
    "approved": "🛡 GUARDIAN APPROVED",
    "vetoed": "⛔ VETOED",
    "executed": "⚡ EXECUTED",
    "settled": "🏁 SETTLED",
    "cancelled": "✖️ CANCELLED",
}

BOND_CHALLENGE_DAYS = 14

LINK_LABELS = {
    "join": ("join", "🎟 Join in browser"),
    "deposit": ("deposit", "💰 Deposit in browser"),
    "withdraw": ("withdraw", "↩️ Withdraw in browser"),
    "vote": ("vote", "❌ Cast Against in browser"),
    "propose": ("host co-sign", "✍️ Co-sign in browser"),
    "sign": ("host signing", "✍️ Sign in browser"),
}

People = Mapping[int, Person]


# ---------------------------------------------------------------------------
# Formatting helpers (pure, unit-tested)
# ---------------------------------------------------------------------------


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def cb(action: str, arg: object) -> str:
    """Build ``shfund:<action>:<arg>``; raise ``ValueError`` over 64 bytes."""
    data = f"{CALLBACK_PREFIX}:{action}:{arg}"
    if len(data.encode("utf-8")) > CALLBACK_MAX_BYTES:
        raise ValueError(f"callback_data over {CALLBACK_MAX_BYTES} bytes: {data!r}")
    return data


def parse_cb(data: str) -> tuple[str, str] | None:
    """Inverse of :func:`cb`; ``None`` when not ours."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or not parts[1]:
        return None
    return parts[1], parts[2]


def fmt_amount(value: float) -> str:
    """``3000`` -> ``3,000``; ``12.4`` -> ``12.4``; ``1234.567`` -> ``1,234.57``; not finite -> ``?``."""
    v = float(value or 0)
    if not math.isfinite(v):
        return "?"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def fmt_usd(value: float) -> str:
    return f"${float(value):,.2f}"


def fmt_pct(bps: int, *, signed: bool = False, decimals: int = 1) -> str:
    """Basis points as a percent: ``500`` -> ``5.0%``; signed adds ``+``."""
    pct = int(bps) / 100.0
    text = f"{pct:+.{decimals}f}%" if signed else f"{pct:.{decimals}f}%"
    return text


def fmt_weight(bps: int) -> str:
    """Weights without a trailing ``.0``: ``6000`` -> ``60%``, ``3333`` -> ``33.33%``."""
    pct = int(bps) / 100.0
    if abs(pct - round(pct)) < 1e-9:
        return f"{int(round(pct))}%"
    return f"{pct:.2f}".rstrip("0").rstrip(".") + "%"


def bar(fraction: float, cells: int) -> str:
    frac = max(0.0, min(1.0, float(fraction)))
    filled = int(math.floor(frac * cells + 1e-9))
    if frac > 0 and filled == 0:
        filled = 1
    return FILLED * filled + EMPTY * (cells - filled)


def veto_meter(against_bps: int, threshold_bps: int, cells: int = METER_CELLS) -> str:
    """``Against 5.0% ▮▮░░░░░░ 20%`` — the bar fills toward the veto threshold."""
    threshold = max(1, int(threshold_bps))
    return f"Against {fmt_pct(against_bps)} {bar(int(against_bps) / threshold, cells)} {fmt_weight(threshold)}"


def countdown(ends_at: int | None, now: float) -> str:
    """``ends in 24m`` / ``ends in 1h 05m`` / ``ends in 2d 3h`` / ``voting closed``."""
    if ends_at is None:
        return ""
    remaining = int(ends_at) - now
    if remaining <= 0:
        return "voting closed"
    if remaining < 60:
        return "ends in <1m"
    minutes = int(math.ceil(remaining / 60.0))
    if minutes < 60:
        return f"ends in {minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"ends in {hours}h {mins:02d}m"
    days, hours = divmod(hours, 24)
    return f"ends in {days}d {hours}h"


GOAL_MAX_PCT = 999


def goal_bar(max_capital: float, nav: float, cells: int = GOAL_CELLS) -> str:
    """Proposal size against fund NAV: ``▮▮▮░░░░░░░ 30% of NAV``; past 999% ``>999% of NAV``."""
    if not nav or not math.isfinite(float(nav)) or nav <= 0 or not math.isfinite(float(max_capital)):
        return ""
    frac = float(max_capital) / float(nav)
    pct = round(frac * 100)
    return f"{bar(frac, cells)} {f'>{GOAL_MAX_PCT}' if pct > GOAL_MAX_PCT else pct}% of NAV"


def status_chip(status: str) -> str:
    return STATUS_CHIPS.get((status or "").lower(), (status or "?").upper())


def person_handle(tg_id: int | None, people: People | None) -> str:
    if tg_id is None:
        return "someone"
    person = (people or {}).get(int(tg_id))
    return esc(person.handle if person else f"user{int(tg_id)}")


def avatars(ids: Iterable[int], people: People | None, limit: int = MAX_AVATARS) -> str:
    unique: list[int] = []
    for i in ids:
        if int(i) not in unique:
            unique.append(int(i))
    shown = [person_handle(i, people) for i in unique[:limit]]
    extra = len(unique) - len(shown)
    if extra > 0:
        shown.append(f"+{extra}")
    return " ".join(shown)


def legs_line(p: ProposalView) -> str:
    if not p.legs:
        return ""
    return " · ".join(
        f"<b>{esc(leg.symbol)}</b> {fmt_weight(leg.weight_bps)}" if leg.weight_bps > 0 else esc(leg.symbol)
        for leg in p.legs
    )


def bond_line(bond_wood: float | None, wood_usd: float | None) -> str:
    """The proposer's WOOD bond, or "" when no quote is on record (unknown or zero: under
    `proposal_signer: agent` the plugin never gets one, and "bond 0 WOOD" reads wrong)."""
    if not bond_wood or bond_wood <= 0:
        return ""
    usd = f" (≈{fmt_usd(bond_wood * wood_usd)})" if wood_usd else ""
    return f"bond {fmt_amount(round(bond_wood, 2))} WOOD{usd} · agent's skin in the game"


def _thesis(text: str, limit: int = THESIS_MAX) -> str:
    t = (text or "").strip()
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return esc(t)


def _short(text: str, limit: int = 40) -> str:
    t = " ".join((text or "").split())
    return esc(t if len(t) <= limit else t[: limit - 1].rstrip() + "…")


def _kb(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def disabled_button(label: str) -> InlineKeyboardButton:
    """A greyed-out button that does nothing (Bot API 10.3 ``disabled``: an empty DisabledButton).

    It carries no other action field (a button takes exactly one). PTB 22.8 speaks Bot API 10.0,
    so the field rides in ``api_kwargs``. Callers keep a fallback: :func:`strip_disabled`.
    """
    return InlineKeyboardButton(label, api_kwargs={"disabled": {}})


def _is_disabled(button: InlineKeyboardButton) -> bool:
    return "disabled" in (button.api_kwargs or {})


def has_disabled(markup: InlineKeyboardMarkup | None) -> bool:
    return markup is not None and any(_is_disabled(b) for row in markup.inline_keyboard for b in row)


def strip_disabled(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """The keyboard as before Bot API 10.3: disabled buttons dropped (None when nothing is left)."""
    if markup is None:
        return None
    rows = [[b for b in row if not _is_disabled(b)] for row in markup.inline_keyboard]
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(rows) if rows else None


def shipped_markup(current: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    """A draft card's keyboard after 🚀 Ship it: that button greyed out as 🚀 Shipped. The rest stays
    live: under a host co-sign the ship toast says "Tap ✍️ Co-sign", and the draft is still signable."""
    shipped = disabled_button(SHIPPED_LABEL)
    rows = [
        [shipped if str(b.callback_data or "").startswith(f"{CALLBACK_PREFIX}:shipit:") else b for b in row]
        for row in (current.inline_keyboard if current is not None else ())
    ]
    return _kb(rows) if any(b is shipped for row in rows for b in row) else _kb([[shipped]])


def _view_button(vault: str, base_url: str | None, label: str = "📊 View fund") -> InlineKeyboardButton:
    return InlineKeyboardButton(label, url=fund_page_url(vault, base_url))


def view_fund_markup(vault: str, base_url: str | None) -> InlineKeyboardMarkup:
    """One 📊 View fund button (the public, unsigned fund page): under the 👤 My position widget."""
    return _kb([[_view_button(vault, base_url)]])


def _date(ts: int | None) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return f"{dt:%b} {dt.day}, {dt.year}"


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def render_proposal_card(
    p: ProposalView,
    *,
    fund: FundView | None = None,
    people: People | None = None,
    wood_usd: float | None = None,
    base_url: str | None = None,
    now: float | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Proposal card with live veto meter and countdown while voting."""
    current = time.time() if now is None else now
    status = (p.status or "").lower()
    lines = [f"{status_chip(status)} · <b>Proposal #{esc(p.display_id)}</b>", f"<i>{_thesis(p.thesis)}</i>", ""]

    legs = legs_line(p)
    if legs:
        lines.append(legs)
    lines.append(
        f"Max capital <b>{fmt_amount(p.max_capital)} {esc(p.capital_symbol)}</b>"
        f" · max drawdown <b>{fmt_pct(p.max_drawdown_bps)}</b>"
    )
    if fund is not None:
        goal = goal_bar(p.max_capital, fund.nav)
        if goal:
            lines.append(f"Size {goal}")
    lines.append("")

    if status in ("voting", "passed", "review", "approved", "vetoed", "executed", "settled"):
        meter = veto_meter(p.against_bps, p.threshold_bps)
        if status == "voting":
            clock = countdown(p.voting_ends_at, current)
            lines.append(f"{meter} · {clock}" if clock else meter)
        else:
            lines.append(meter)
    if status == "review":
        clock = countdown(p.review_ends_at, current).replace("voting closed", "review closed")
        cover = f" · coverage {esc(p.guardian_coverage)}" if p.guardian_coverage else ""
        lines.append("🛡 Guardian review" + (f" · {clock}" if clock else "") + cover)
    elif status == "approved":
        lines.append("🛡 Guardian approved" + (f" · coverage {esc(p.guardian_coverage)}" if p.guardian_coverage else ""))

    social = []
    if p.backers:
        social.append(f"👍 {avatars(p.backers, people)}")
    if p.objectors:
        social.append(f"❌ {avatars(p.objectors, people)}")
    if social:
        lines.append(" · ".join(social))

    credit = []
    if p.idea_by is not None:
        credit.append(f"idea by {person_handle(p.idea_by, people)}")
    if p.proposer:
        credit.append(f"agent {esc(p.proposer)}")
    if credit:
        lines.append(" · ".join(credit))
    bond = bond_line(p.bond_wood, wood_usd)
    if bond:
        lines.append(bond)

    if status == "voting":
        lines.append("")
        lines.append(
            f"<i>Only Against counts. Silence = pass unless Against reaches "
            f"{fmt_weight(p.threshold_bps)} of shares before the window ends.</i>"
        )
    elif status == "vetoed":
        lines.append("<i>Vetoed. The agent's bond comes back at once.</i>")

    rows: list[list[InlineKeyboardButton]] = []
    if status in ("proposed", "voting"):
        back_label = "👍 Back it" + (f" · {len(set(p.backers))}" if p.backers else "")
        row = [InlineKeyboardButton(back_label, callback_data=cb("back", p.id))]
        if status == "voting":
            row.append(InlineKeyboardButton("❌ Object", callback_data=cb("vote", p.id)))
        rows.append(row)
    elif status in CLOSED_PROPOSAL_LABELS:
        rows.append([disabled_button(CLOSED_PROPOSAL_LABELS[status])])
    if fund is not None:
        rows.append([_view_button(fund.vault, base_url)])
    return "\n".join(lines).rstrip(), _kb(rows)


def render_fund_card(
    f: FundView,
    *,
    people: People | None = None,
    base_url: str | None = None,
    now: float | None = None,
    notice: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    current = time.time() if now is None else now
    if f.nav_stale and f.nav_as_of is None:
        # No chain read has given a NAV yet (or the stored one was unusable): never a made-up 0.
        nav_line = "NAV <b>unavailable</b> · P&amp;L <b>n/a</b>"
    elif f.nav_partial:
        # A strategy holds capital whose value could not be read: the cash is only a floor.
        nav_line = f"NAV <b>≥{fmt_amount(round(f.nav, 2))} {esc(f.nav_symbol)}</b> · P&amp;L <b>n/a</b>"
    elif f.nav_at_cost:
        # A strategy counted at what it invested: the market move is unknown, so no P&L.
        nav_line = f"NAV <b>{fmt_amount(round(f.nav, 2))} {esc(f.nav_symbol)}</b> (at cost) · P&amp;L <b>n/a</b>"
    else:
        nav_line = (
            f"NAV <b>{fmt_amount(round(f.nav, 2))} {esc(f.nav_symbol)}</b>"
            f" · P&amp;L <b>{fmt_pct(f.pnl_bps, signed=True)}</b>"
        )
    lines = [f"🏦 <b>{esc(f.name)}</b>", nav_line, "", "<b>Positions</b>"]
    if f.positions:
        for pos in f.positions:
            pnl = f" ({fmt_pct(pos.pnl_bps, signed=True)})" if pos.pnl_bps is not None else ""
            note = f" {esc(pos.note)}" if pos.note else ""
            lines.append(f"• {esc(pos.symbol)} {fmt_amount(round(pos.value, 2))} {esc(f.nav_symbol)}{note}{pnl}")
    elif f.nav_partial:
        lines.append("• strategy value unreadable")
    else:
        lines.append("• none yet")
    if f.open_proposals:
        lines += ["", "<b>Open proposals</b>"]
        for p in f.open_proposals:
            item = f"• #{esc(p.display_id)} {_short(p.thesis)} · {status_chip(p.status)}"
            if (p.status or "").lower() == "voting":
                clock = countdown(p.voting_ends_at, current)
                if clock:
                    item += f" · {clock}"
            lines.append(item)
    if f.cooldown_until and f.cooldown_until > current:
        hhmm = datetime.fromtimestamp(int(f.cooldown_until), tz=timezone.utc).strftime("%H:%M")
        lines += ["", f"⏳ Next proposal possible at {hhmm} UTC"]
    lines.append("")
    agents = ", ".join(esc(a) for a in f.agents) if f.agents else "none"
    lines.append(f"{members_label(f)} · Agents {agents}")
    partial = "NAV counts the vault's cash only: the active strategy's value could not be read." if (
        f.nav_partial and not (f.nav_stale and f.nav_as_of is None)) else None
    notices = [n for n in (nav_notice(f, current), partial, notice) if n]
    if notices:
        lines.append("")
        lines += [f"<i>{esc(n)}</i>" for n in notices]
    rows = [
        [_view_button(f.vault, base_url, "📊 View")],
        [
            InlineKeyboardButton("💰 Deposit", callback_data=cb("deposit", f.vault.lower())),
            InlineKeyboardButton("↩️ Withdraw", callback_data=cb("withdraw", f.vault.lower())),
        ],
        # Members join from here (personal /tg/join link): no /join command in
        # groups, /join-fund belongs to guest AGENTS (a Hermes slash command).
        # 👤 My position: the tapper's own position, as a widget in their DM with the bot.
        [
            InlineKeyboardButton("🎟 Join", callback_data=cb("join", f.vault.lower())),
            InlineKeyboardButton("👤 My position", callback_data=cb("position", f.vault.lower())),
        ],
    ]
    return "\n".join(lines), _kb(rows)


def members_label(f: FundView) -> str:
    """``Members N``: chat members who joined (🎟 Join). A wallet that deposited from the app
    without joining is not one, and the read has no depositor list to count them from; so
    with open deposits, shares outstanding and nobody joined, the line says why it is 0
    instead of sitting at 0 next to a non-zero NAV."""
    if not f.members and f.deposits_open and f.shares_outstanding:
        return "Members 0 (depositors haven't tapped 🎟 Join yet)"
    return f"Members {len(f.members)}"


def nav_notice(f: FundView, now: float) -> str | None:
    """The fund card's NAV notice when the chain read is behind (``nav_stale``), else None."""
    if not f.nav_stale:
        return None
    if f.nav_as_of is None:
        return "NAV unavailable: no chain read has returned it yet. Try /fund again in a minute."
    try:
        when = datetime.fromtimestamp(int(f.nav_as_of), tz=timezone.utc)
        today = datetime.fromtimestamp(int(now), tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):  # a time no calendar holds: say last known, no time
        return "Last known NAV."
    stamp = when.strftime("%H:%M") if when.date() == today else f"{when.strftime('%b')} {when.day} {when.strftime('%H:%M')}"
    return f"Last known NAV, as of {stamp} UTC."


def render_member_card(
    m: MemberView, *, fund: FundView, base_url: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    joined = _date(m.joined_at)
    # share_bps None: the share is not known here (no chain read behind it), so no share line.
    parts = [f"{fmt_pct(m.share_bps)} of shares"] if m.share_bps is not None else []
    if joined:
        parts.append(f"joined {joined}")
    # N is deposit order on chain (the share image uses the same number); until the
    # first deposit lands there is no number yet.
    title = f"🎟 <b>Member #{int(m.number)}</b>" if int(m.number or 0) > 0 else "🎟 <b>Member</b>"
    lines = [f"{title} · {esc(m.person.handle)}"] + ([" · ".join(parts)] if parts else []) + [f"<b>{esc(fund.name)}</b>"]
    if int(m.number or 0) <= 0:
        lines.append("<i>Your member number is your deposit order: it shows after your first deposit.</i>")
    if m.address:
        lines.append(f"🔗 wallet linked <code>{esc(m.address)}</code>")
    return "\n".join(lines), _kb([[_view_button(fund.vault, base_url)]])


def render_settle_receipt(
    r: Receipt,
    *,
    people: People | None = None,
    fund: FundView | None = None,
    base_url: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    parts = [f"<b>{fmt_pct(r.pnl_bps, signed=True)}</b> on {esc(r.symbol)}"]
    if r.idea_by is not None:
        parts.append(f"idea by {person_handle(r.idea_by, people)}")
    parts.append(f"backed by {int(r.backers)}")
    lines = [
        f"🏁 <b>Proposal #{esc(r.proposal_id)} settled</b>",
        " · ".join(parts),
        f"<i>The agent's bond stays in escrow for the {BOND_CHALLENGE_DAYS}-day challenge window.</i>",
    ]
    rows = [[_view_button(fund.vault, base_url)]] if fund is not None else []
    return "\n".join(lines), _kb(rows)


def receipt_plain(r: Receipt, people: People | None = None) -> str:
    """Plain-text receipt line (no HTML): ``+8.2% on NVDA · idea by @alice · backed by 5``."""
    parts = [f"{fmt_pct(r.pnl_bps, signed=True)} on {r.symbol}"]
    if r.idea_by is not None:
        person = (people or {}).get(int(r.idea_by))
        parts.append(f"idea by {person.handle if person else f'user{int(r.idea_by)}'}")
    parts.append(f"backed by {int(r.backers)}")
    return " · ".join(parts)


def render_draft_card(
    p: ProposalView,
    *,
    fund: FundView | None = None,
    people: People | None = None,
    wood_usd: float | None = None,
    bond_shortfall_wood: float | None = None,
    requires_host_cosign: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Draft built from a pitch. Shows the bond buffer line while WOOD is being swapped in."""
    head = f"{status_chip('draft')} · <b>Draft #{esc(p.id)}</b>"
    if p.idea_by is not None:
        head += f" · from {person_handle(p.idea_by, people)}'s idea"
    lines = [head, f"<i>{_thesis(p.thesis)}</i>", ""]
    legs = legs_line(p)
    if legs:
        lines.append(legs)
    lines.append(
        f"Max capital <b>{fmt_amount(p.max_capital)} {esc(p.capital_symbol)}</b>"
        f" · max drawdown <b>{fmt_pct(p.max_drawdown_bps)}</b>"
    )
    if fund is not None:
        goal = goal_bar(p.max_capital, fund.nav)
        if goal:
            lines.append(f"Size {goal}")
    if p.proposer:
        lines.append(f"agent {esc(p.proposer)}")
    bond = bond_line(p.bond_wood, wood_usd)
    if bond:
        lines.append(bond)
    if bond_shortfall_wood and bond_shortfall_wood > 0:
        usd = f" (≈{fmt_usd(bond_shortfall_wood * wood_usd)})" if wood_usd else ""
        lines.append(f"needs ~{fmt_amount(round(bond_shortfall_wood, 1))} WOOD{usd}, swapping…")
    buffer_line = bond_buffer_line(p)
    if buffer_line:
        lines.append(buffer_line)
    if p.template == "generic-call":
        lines.append(GENERIC_CALL_CARD_LINE)
    lines += ["", SHIP_IT_HINT]
    row = [InlineKeyboardButton("🚀 Ship it", callback_data=cb("shipit", p.id))]
    if requires_host_cosign:
        row.append(InlineKeyboardButton("✍️ Co-sign (host)", callback_data=cb("propose", p.id)))
    return "\n".join(lines), _kb([row])


SHIP_IT_HINT = "<i>Host: 🚀 Ship it sends this to a vote.</i>"
# The hint as Telegram hands the card back (message.text_html), with or without the italics.
_SHIP_IT_HINT_RE = re.compile(r"\n*(?:<(?:i|em)>)?Host: 🚀 Ship it sends this to a vote\.(?:</(?:i|em)>)?")


# The host decides from the card, so the card says what Generic Call means before the vote.
GENERIC_CALL_CARD_LINE = (
    "⚠️ Generic Call: an unaudited template, never certified. Guardians abstain (UNAUDITED_TEMPLATE), so "
    "execute stays blocked (InsufficientApproveCoverage) until a guardian operator allows it; the vault "
    "stays locked until then or a cancel."
)


BOND_BUFFER_LINES = {
    "swap": "🔁 bond buffer: the WOOD swap goes first in the bundle",
    "insufficient-usdg": "⚠️ the proposer's USDG can't pay the WOOD swap: fund it before propose",
    "premium-over-cap": "⚠️ the WOOD swap is priced over the feed cap: host, accept it on the signing page or fund WOOD",
    "unavailable": "⚠️ the bond-buffer swap is unavailable: fund the proposer with WOOD",
    "no-route": "⚠️ no USDG→WOOD route: fund the proposer with WOOD",
}


def bond_buffer_line(p: ProposalView) -> str:
    """The propose-portfolio `bondBuffer.status` (sherwood #740) as one card line ("" when nothing to say)."""
    line = BOND_BUFFER_LINES.get((p.bond_buffer_status or "").lower(), "")
    if line and p.bond_buffer_note:
        line += f" ({_short(p.bond_buffer_note, 120)})"
    return line


def render_join_prompt(f: FundView, *, base_url: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"🎟 <b>Join {esc(f.name)}</b>",
        "Members pool capital, agents propose trades, and the group can veto any of them.",
        "Tap Join for your personal link. Your wallet signs in the browser; nothing moves from this chat.",
    ]
    rows = [[
        InlineKeyboardButton("🎟 Join", callback_data=cb("join", f.vault.lower())),
        _view_button(f.vault, base_url),
    ]]
    return "\n".join(lines), _kb(rows)


def render_member_request(f: FundView, person: Person) -> tuple[str, InlineKeyboardMarkup]:
    text = f"🙋 {esc(person.handle)} wants to join <b>{esc(f.name)}</b>.\nHost: approve to let them deposit."
    return text, _kb([[InlineKeyboardButton("✅ Approve member", callback_data=cb("approve_member", int(person.tg_id)))]])


def render_agent_card(f: FundView, agent_name: str, *, pending: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    if pending:
        text = f"🤖 Agent <b>{esc(agent_name)}</b> asks to propose trades for <b>{esc(f.name)}</b>."
        row = [
            InlineKeyboardButton("✅ Approve agent", callback_data=cb("approve_agent", agent_name)),
            InlineKeyboardButton("🗑 Remove", callback_data=cb("remove_agent", agent_name)),
        ]
    else:
        text = f"🤖 Agent <b>{esc(agent_name)}</b> proposes trades for <b>{esc(f.name)}</b>."
        row = [InlineKeyboardButton("🗑 Remove agent", callback_data=cb("remove_agent", agent_name))]
    return text, _kb([row])


def render_leaderboard(
    rows: Sequence[LeaderRow],
    *,
    fund: FundView | None = None,
    people: People | None = None,
    base_url: str | None = None,
    limit: int = 10,
) -> tuple[str, InlineKeyboardMarkup]:
    title = "🏆 <b>Leaderboard</b>" + (f" · {esc(fund.name)}" if fund is not None else "")
    lines = [title]
    ranked = sorted(rows, key=lambda r: (-int(r.pnl_bps), -int(r.ideas), int(r.tg_id)))[:limit]
    if not ranked:
        lines.append("No settled ideas yet. Pitch one: mention me with a trade idea.")
    for i, r in enumerate(ranked, start=1):
        ideas = f"{int(r.ideas)} idea" + ("" if int(r.ideas) == 1 else "s")
        lines.append(f"{i}. {person_handle(r.tg_id, people)} <b>{fmt_pct(r.pnl_bps, signed=True)}</b> · {ideas}")
    kb_rows = [[_view_button(fund.vault, base_url)]] if fund is not None else []
    return "\n".join(lines), _kb(kb_rows)


SIGN_DETAIL_KEYS = (("depositor", "Depositor"), ("depositors", "Depositors"), ("agentAddress", "Agent wallet"),
                    ("agentId", "Agent id"), ("proposer", "Proposer"), ("vault", "Vault"))


def sign_link_details(url: str) -> list[str]:
    """Full addresses from a /tg/sign link's body, so the host checks them before signing."""
    from urllib.parse import parse_qs, urlsplit

    from .links import decode_sign_body

    try:
        q = parse_qs(urlsplit(url).query)
        action = q.get("action", [""])[0]
        body = decode_sign_body(q.get("body", [""])[0])
    except Exception:
        return []
    lines = [f"Action: <code>{esc(action)}</code>"] if action else []
    for key, label in SIGN_DETAIL_KEYS:
        value = body.get(key) if isinstance(body, dict) else None
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value not in (None, ""):
            lines.append(f"{label}: <code>{esc(value)}</code>")
    return lines


def _expires(kind: str) -> str:
    ttl = link_ttl_s(kind)
    return f"{ttl // 3600} hours" if ttl >= 3600 else f"{ttl // 60} minutes"


def render_personal_link(
    kind: str, url: str, fund_name: str, *, deposit_url: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """The per-user message holding a signed URL button.

    ``deposit_url`` (a join on a fund with open deposits): the member's deposit
    link rides along as a second button, since there is no host approval to wait for.
    """
    noun, label = LINK_LABELS.get(kind, (kind, "Open in browser"))
    text = (
        f"🔐 Your personal {esc(noun)} link for <b>{esc(fund_name)}</b>.\n"
        f"It is tied to your Telegram account and expires in {_expires(kind)}. "
        "Your wallet signs in the browser."
    )
    if kind == "sign":
        details = sign_link_details(url)
        if details:
            text += "\n\nCheck before you sign:\n" + "\n".join(details)
    rows = [[InlineKeyboardButton(label, url=url)]]
    if deposit_url:
        text += (
            "\n\nDeposits are open on this fund, so there is no approval to wait for: link your wallet, "
            f"then deposit with the second button (it expires in {_expires('deposit')})."
        )
        rows.append([InlineKeyboardButton(LINK_LABELS["deposit"][1], url=deposit_url)])
    return text, _kb(rows)


def group_command(command: str) -> tuple[str, bool]:
    """The propose command as the group sees it: a `--calls` plan path cut to its file name (the full
    path names the operator's home directory). Returns (text, shortened). The record keeps the full
    command; fund_pending_commands hands that one to the host's one-shot."""
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    if argv:
        out, shortened = [], False
        for i, tok in enumerate(argv):
            if i > 0 and argv[i - 1] == "--calls" and "/" in tok:
                out.append(tok.rsplit("/", 1)[1])
                shortened = True
            elif tok.startswith("--calls=") and "/" in tok:
                out.append("--calls=" + tok.rsplit("/", 1)[1])
                shortened = True
            else:
                out.append(tok)
        return (shlex.join(out), True) if shortened else (command, False)
    cut = re.sub(r"(--calls[ =])'?/[^\s']*/([^\s'/]+)'?", r"\1\2", command)
    return cut, cut != command


def render_shipped_command(base_html: str, command: str, *, shipped_by: str, proposer: str, limit: int = 4000) -> str:
    """A shipped draft card under proposal_signer: agent: the card plus the Sherwood CLI command to run
    (the proposing agent's CLI wallet signs it; the plugin does not). The draft's "Ship it sends this to
    a vote" hint goes (it is shipped), and a Generic Call plan path shows as its file name only."""
    base_html = _SHIP_IT_HINT_RE.sub("", base_html).rstrip()
    shown, shortened = group_command(command)
    head = (
        f"{base_html}\n\n<i>🚀 Shipped by {esc(shipped_by)}. {esc(proposer)} proposes it from its own wallet "
        "with the Sherwood CLI, outside this chat (a hermes -z one-shot or an approve-mode cron):</i>"
    )
    text = f"{head}\n<pre>{esc(shown)}</pre>"
    if shortened:
        text += "\n<i>Plan path shortened here: fund_pending_commands has the full command.</i>"
    if len(text) <= limit:
        return text
    return f"{head}\n<i>The command is too long for this card: fund_pending_commands has it.</i>"
