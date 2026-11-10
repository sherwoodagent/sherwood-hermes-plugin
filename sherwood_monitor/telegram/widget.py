"""The member's "My position" message widget (Bot API 10.4 ``sendWidget``).

A widget is one fixed HTML document plus changeable state that only the bot
writes. Here the HTML is the constant :data:`WIDGET_HTML`: it never contains a
value from a member or the chain. Everything it shows travels in the widget's
``chat_data`` (JSON, at most 1024 bytes) and is drawn with ``textContent``, so a
fund name or proposal title is always data, never markup or script.

In production Telegram shows widgets only in a private chat with the bot, so a
widget is only ever sent to a member's DM, never to the group.

The same :class:`Position` also renders as plain text: the widget's caption
(what clients without widget support show instead, and the chat-list preview)
and the DM text message sent when ``sendWidget`` fails.

No queries: Hermes polls with PTB 22.8's ``Update.ALL_TYPES`` (Bot API 10.0),
which has no ``widget_query``, so the widget is sent without ``allow_queries``.
Signed personal links, secrets and keys never go into widget data.

Needs PTB only through ``cards`` (formatting helpers).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .cards import fmt_amount, fmt_pct, fmt_weight, status_chip
from .types import FundView

HTML_MAX_BYTES = 32768
HTML_MAX_ZLIB = 4096  # after zlib level 9, as the server counts it
DATA_MAX_BYTES = 1024
CAPTION_MAX = 1024
MAX_PROPOSALS = 3
NAME_MAX = 48
TITLE_MAX = 64
SYMBOL_MAX = 12
STALE_HINT_S = 300  # the widget says "NAV as of" when the NAV is older than this

# One self-contained document; no value from a member or the chain is ever written into it.
WIDGET_HTML = """<style>
body{margin:0;padding:10px 12px 14px}
h1{font-size:1.05em;margin:0 0 6px}
.r{display:flex;justify-content:space-between;gap:12px;padding:1px 0}
.k,.h{color:var(--tg-theme-hint-color)}
.h{font-size:.85em}
.p{margin-top:8px;padding-top:6px;border-top:1px solid var(--tg-theme-section-separator-color,rgba(136,136,136,.3))}
.t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m{height:4px;border-radius:2px;margin:4px 0;background:var(--tg-theme-secondary-bg-color,rgba(136,136,136,.2))}
.m div{height:4px;border-radius:2px;background:var(--tg-theme-destructive-text-color,#e53935)}
</style>
<div id="a"></div>
<script>
var W=window.TelegramWidget,A=document.getElementById('a');
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=String(x);return e}
function num(v){v=Number(v);return isFinite(v)?v:0}
function tm(s){try{return new Date(s*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}catch(e){return ''}}
function left(e){var s=e-Date.now()/1000;if(s<=0)return 'voting closed';var m=Math.ceil(s/60);if(m<60)return 'ends in '+m+'m';var h=Math.floor(m/60);if(h<24)return 'ends in '+h+'h '+String(m%60).padStart(2,'0')+'m';return 'ends in '+Math.floor(h/24)+'d '+(h%24)+'h'}
function rd(){if(W&&W.ready)W.ready()}
function draw(){
var d=null;try{d=JSON.parse(W&&W.chatData)}catch(e){}
A.replaceChildren();
if(!d||typeof d!='object'){A.appendChild(el('div','h','No data yet.'));rd();return}
A.appendChild(el('h1',0,d.f));
(Array.isArray(d.r)?d.r:[]).forEach(function(r){if(!Array.isArray(r))return;var x=el('div','r');x.appendChild(el('span','k',r[0]));x.appendChild(el('span',0,r[1]));A.appendChild(x)});
if(d.n)A.appendChild(el('div','h',d.n));
(Array.isArray(d.p)?d.p:[]).forEach(function(p){
if(!p||typeof p!='object')return;
var b=el('div','p');b.appendChild(el('div','t','#'+p.i+' '+p.t));
b.appendChild(el('div','h',p.e?p.s+' \\u00b7 '+left(num(p.e)):p.s));
if(p.h){var a=num(p.a),h=Math.max(1,num(p.h)),m=el('div','m'),f=el('div');f.style.width=Math.min(100,a/h*100)+'%';m.appendChild(f);b.appendChild(m);b.appendChild(el('div','h','Against '+(a/100).toFixed(1)+'% of '+(h/100)+'% to veto'))}
if(p.m=='b')b.appendChild(el('div','h','\\ud83d\\udc4d You back this'));
if(p.m=='o')b.appendChild(el('div','h','\\u274c You object in chat'));
A.appendChild(b)});
if(d.x)A.appendChild(el('div','h','+'+num(d.x)+' more on the fund card'));
var u=num(d.u),o=num(d.o),s=u?'Updated '+tm(u):'';
if(o&&u-o>300)s+=(s?' \\u00b7 ':'')+'NAV as of '+tm(o);
if(s)A.appendChild(el('div','h',s));
rd()}
if(W)W.onEvent('chatDataChanged',draw);
draw();setInterval(draw,30000);
</script>"""


def _clip(text: object, limit: int) -> str:
    """One line, at most ``limit`` characters (``…`` when cut). Plain text, never markup."""
    t = " ".join(str(text if text is not None else "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def fmt_price(value: float) -> str:
    """A share price: 4 decimals, trailing zeros cut down to 2 (``1.0213``, ``1.00``)."""
    text = f"{float(value):,.4f}"
    whole, frac = text.split(".")
    frac = frac.rstrip("0").ljust(2, "0")
    return f"{whole}.{frac}"


MAX_PLAUSIBLE_TS = 10**11  # unix seconds; a larger value is milliseconds or junk (as fund.models)


def _plausible(ts: Any) -> int | None:
    """Unix seconds, or None for anything else (0, negative, milliseconds, NaN, text)."""
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    return int(value) if math.isfinite(value) and 0 < value < MAX_PLAUSIBLE_TS else None


def _utc(ts: Any, now: float) -> str:
    """``14:05 UTC`` today, else ``Nov 12 14:05 UTC``; "" for a time no calendar holds."""
    try:
        when = datetime.fromtimestamp(int(ts), tz=UTC)
        today = datetime.fromtimestamp(int(now), tz=UTC).date()
    except (OverflowError, OSError, TypeError, ValueError):
        return ""
    if when.date() == today:
        return f"{when:%H:%M} UTC"
    return f"{when:%b} {when.day} {when:%H:%M} UTC"


def _date(ts: Any) -> str:
    """``Nov 14, 2023``; "" for a time no calendar holds."""
    try:
        when = datetime.fromtimestamp(int(ts), tz=UTC)
    except (OverflowError, OSError, TypeError, ValueError):
        return ""
    return f"{when:%b} {when.day}, {when.year}"


@dataclass(frozen=True)
class PositionProposal:
    id: str
    title: str
    status: str  # the card's chip, e.g. "🗳 VOTING"
    against_bps: int | None = None  # set while voting
    threshold_bps: int | None = None
    ends_at: int | None = None  # voting window end (unix s), while voting
    mine: str = ""  # "b": this member backs it in chat, "o": objects in chat


@dataclass(frozen=True)
class Position:
    """What the plugin knows about one member's place in one fund. Rows it cannot fill are absent."""

    fund: str
    rows: tuple[tuple[str, str], ...] = ()
    note: str = ""
    proposals: tuple[PositionProposal, ...] = ()
    more: int = 0  # open proposals not listed
    nav_as_of: int | None = None


def _nav_rows(f: FundView) -> list[tuple[str, str]]:
    """NAV rows, only from a NAV the chain actually returned (``nav_as_of`` set): never a made-up 0."""
    if f.nav_as_of is None or not math.isfinite(float(f.nav)):
        return []
    sym = _clip(f.nav_symbol, SYMBOL_MAX)
    nav = fmt_amount(round(f.nav, 2))
    if f.nav_partial:  # a strategy's value could not be read: the cash is a floor, no P&L
        return [("NAV", f"≥{nav} {sym}")]
    if f.nav_at_cost:  # counted at what it invested: the market move is unknown, no P&L
        return [("NAV", f"{nav} {sym} (at cost)")]
    rows = [("NAV", f"{nav} {sym}"), ("P&L", fmt_pct(f.pnl_bps, signed=True))]
    if f.share_price is not None and math.isfinite(float(f.share_price)) and f.share_price > 0:
        rows.append(("Share price", f"{fmt_price(f.share_price)} {sym}"))
    return rows


def is_member_or_host(f: FundView, user_id: int) -> bool:
    """A recorded (approved) member of the fund, or its host."""
    uid = int(user_id)
    return (f.host_tg_id is not None and int(f.host_tg_id) == uid) or any(int(m.tg_id) == uid for m in f.members)


def build_position(f: FundView, user_id: int) -> Position:
    """The member's position from the fund view. Per-member shares are not read by the plugin, so
    there is no shares or value row; a member who has not joined gets the fund rows and a note."""
    uid = int(user_id)
    rows = _nav_rows(f)
    member = next((m for m in f.members if int(m.tg_id) == uid), None)
    is_host = f.host_tg_id is not None and int(f.host_tg_id) == uid
    roles = ["Host"] if is_host else []
    if member is not None:
        roles.append(f"Member #{int(member.number)}" if int(member.number or 0) > 0 else "Member")
    if roles:
        rows.append(("You", " · ".join(roles)))
    joined = _date(member.joined_at) if member is not None and member.joined_at else ""
    if joined:
        rows.append(("Joined", joined))
    if member is not None and member.share_bps is not None:
        rows.append(("Your share", fmt_pct(member.share_bps)))
    note = "" if roles else "Not a member yet: tap 🎟 Join on the fund card in the group."
    props = []
    for p in f.open_proposals:
        voting = (p.status or "").lower() == "voting"
        mine = "b" if uid in {int(b) for b in p.backers} else "o" if uid in {int(o) for o in p.objectors} else ""
        props.append(PositionProposal(
            id=_clip(p.display_id, 24),
            title=_clip(p.thesis, TITLE_MAX),
            status=status_chip(p.status),
            against_bps=int(p.against_bps) if voting else None,
            threshold_bps=int(p.threshold_bps) if voting else None,
            ends_at=_plausible(p.voting_ends_at) if voting else None,  # a millisecond voteEnd: no countdown
            mine=mine,
        ))
    return Position(
        fund=_clip(f.name, NAME_MAX),
        rows=tuple(rows),
        note=note,
        proposals=tuple(props[:MAX_PROPOSALS]),
        more=max(0, len(props) - MAX_PROPOSALS),
        nav_as_of=_plausible(f.nav_as_of),
    )


def _dump(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def _data_dict(pos: Position, now: float) -> dict[str, Any]:
    d: dict[str, Any] = {"f": pos.fund, "r": [list(r) for r in pos.rows]}
    if pos.note:
        d["n"] = pos.note
    proposals = []
    for p in pos.proposals:
        item: dict[str, Any] = {"i": p.id, "t": p.title, "s": p.status}
        if p.threshold_bps:
            item["a"], item["h"] = p.against_bps or 0, p.threshold_bps
        if p.ends_at:
            item["e"] = p.ends_at
        if p.mine:
            item["m"] = p.mine
        proposals.append(item)
    if proposals:
        d["p"] = proposals
    if pos.more:
        d["x"] = pos.more
    if pos.nav_as_of:
        d["o"] = int(pos.nav_as_of)
    if now:
        d["u"] = int(now)
    return d


def widget_data(pos: Position, *, now: float) -> str:
    """``chat_data`` JSON, at most :data:`DATA_MAX_BYTES` UTF-8 bytes whatever the inputs.

    Over budget, proposals go first (counted in ``x``), then titles and the fund name get
    shorter, then the note and rows go; the fund name and time always fit.
    """
    d = _data_dict(pos, now)
    s = _dump(d)

    def fits() -> bool:
        return len(s.encode("utf-8")) <= DATA_MAX_BYTES

    while not fits() and d.get("p"):
        d["p"].pop()
        d["x"] = int(d.get("x", 0)) + 1
        if not d["p"]:
            del d["p"]
        s = _dump(d)
    for limit in (24, 12):
        if fits():
            break
        d["f"] = _clip(d["f"], limit)
        s = _dump(d)
    if not fits():
        d.pop("n", None)
        s = _dump(d)
    while not fits() and d["r"]:
        d["r"].pop()
        s = _dump(d)
    return s


def data_fingerprint(pos: Position) -> str:
    """Changes only when the values the widget draws change: not with the update time, and not
    with a newer read that left the values as they were (``nav_as_of``, the read's block time,
    moves on every read), so a chain read alone edits no widget."""
    return hashlib.sha256(widget_data(replace(pos, nav_as_of=None), now=0).encode("utf-8")).hexdigest()


def position_text(pos: Position, *, now: float) -> str:
    """Plain text (no markup, so names and titles show literally): the widget's caption and the DM
    fallback when ``sendWidget`` fails. At most :data:`CAPTION_MAX` characters.

    Refreshes rewrite only the widget's data, never this text, so it ends with the time it was
    written: clients without widgets know it is a snapshot."""
    head = [f"👤 Your position · {pos.fund}"]
    head += [f"{label}: {value}" for label, value in pos.rows]
    nav_as_of = _utc(pos.nav_as_of, now) if pos.nav_as_of else ""
    if nav_as_of and pos.rows and pos.rows[0][0] == "NAV":
        head.append(f"NAV as of: {nav_as_of}")
    if pos.note:
        head.append(pos.note)
    lines = []
    for p in pos.proposals:
        parts = [f"#{p.id} {p.title}", p.status]
        if p.threshold_bps:
            parts.append(f"Against {fmt_pct(p.against_bps or 0)} of {fmt_weight(p.threshold_bps)}")
        ends = _utc(p.ends_at, now) if p.ends_at else ""
        if ends:
            parts.append("voting closed" if (p.ends_at or 0) <= now else f"ends {ends}")
        if p.mine:
            parts.append("👍 you back this" if p.mine == "b" else "❌ you object in chat")
        lines.append(" · ".join(parts))
    stamp = _utc(now, now)
    foot = [f"As of {stamp}. Tap 👤 My position again for the latest."] if stamp else []
    more = pos.more
    while True:
        tail = ([""] + lines if lines else []) + ([f"+{more} more on the fund card"] if more else [])
        text = "\n".join(head + tail + foot)
        if len(text) <= CAPTION_MAX or not lines:
            break
        lines.pop()
        more += 1
    return text if len(text) <= CAPTION_MAX else text[: CAPTION_MAX - 1] + "…"
