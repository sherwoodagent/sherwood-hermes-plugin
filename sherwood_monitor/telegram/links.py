"""Signed deep links from Telegram into the Sherwood web app (``/tg/*`` routes).

Contract (shared with the app lane; see ``docs/telegram.md`` for test vectors):

    ${APP_BASE_URL}/tg/<join|deposit|withdraw>?fund=<vault>&tg=<telegram_user_id>&ts=<unix_s>&sig=<hex>
        sig = hex(hmac_sha256(SHERWOOD_TG_LINK_SECRET, "<vault>|<tg>|<ts>"))

    ${APP_BASE_URL}/tg/vote?proposal=<governor>:<proposal_id>&tg=<telegram_user_id>&ts=<unix_s>&sig=<hex>
        sig = hex(hmac_sha256(SHERWOOD_TG_LINK_SECRET, "<governor>:<proposal_id>|<tg>|<ts>"))

    ${APP_BASE_URL}/tg/fund/<vault>          (public, unsigned)

Addresses are canonicalised to lowercase ``0x`` + 40 hex before signing and
are written to the URL in that same form, so a verifier signs over the query
values exactly as it receives them (after ordinary URL decoding). The ``:`` in
``proposal=`` is written literally (it is legal in a query string); a verifier
that receives ``%3A`` decodes it to the same value.

Pure module: no Telegram or Hermes imports.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import quote

DEFAULT_APP_BASE_URL = "https://app.sherwood.sh"

FUND_LINK_KINDS = ("join", "deposit", "withdraw")
VOTE_LINK_KIND = "vote"
LINK_KINDS = FUND_LINK_KINDS + (VOTE_LINK_KIND,)

# Recommended max age the app should accept for ``ts`` (documented, not enforced here).
RECOMMENDED_LINK_TTL_S = 15 * 60

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PROPOSAL_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,78}$")


def canonical_address(address: str) -> str:
    """Return ``address`` as lowercase ``0x`` + 40 hex; raise ``ValueError`` otherwise."""
    if not isinstance(address, str) or not _ADDRESS_RE.match(address.strip()):
        raise ValueError(f"not an EVM address: {address!r}")
    return address.strip().lower()


def canonical_proposal_id(proposal_id: object) -> str:
    """Proposal ids are decimal (or simple token) strings; reject anything that needs escaping."""
    text = str(proposal_id).strip()
    if not _PROPOSAL_ID_RE.match(text):
        raise ValueError(f"bad proposal id: {proposal_id!r}")
    return text


def _canonical_tg(tg_user_id: object) -> str:
    tg = int(tg_user_id)  # raises on junk
    if tg <= 0:
        raise ValueError("telegram user id must be positive")
    return str(tg)


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, bytes):
        key = secret
    else:
        key = str(secret).encode("utf-8")
    if not key:
        raise ValueError("link secret is empty")
    return key


def sign_link(secret: str | bytes, message: str) -> str:
    """``hex(hmac_sha256(secret, message))`` — lowercase hex, 64 chars."""
    return hmac.new(_secret_bytes(secret), message.encode("utf-8"), hashlib.sha256).hexdigest()


def fund_link_message(vault: str, tg_user_id: object, ts: int) -> str:
    """The string signed for join / deposit / withdraw links: ``"<vault>|<tg>|<ts>"``."""
    return f"{canonical_address(vault)}|{_canonical_tg(tg_user_id)}|{int(ts)}"


def vote_link_message(governor: str, proposal_id: object, tg_user_id: object, ts: int) -> str:
    """The string signed for vote links: ``"<governor>:<proposal_id>|<tg>|<ts>"``."""
    return (
        f"{canonical_address(governor)}:{canonical_proposal_id(proposal_id)}"
        f"|{_canonical_tg(tg_user_id)}|{int(ts)}"
    )


def _base(base_url: str | None) -> str:
    return (base_url or DEFAULT_APP_BASE_URL).rstrip("/")


def build_link(
    kind: str,
    *,
    tg_user_id: object,
    secret: str | bytes,
    base_url: str | None = None,
    fund: str | None = None,
    governor: str | None = None,
    proposal_id: object | None = None,
    ts: int | None = None,
) -> str:
    """Build a personal, signed ``/tg/<kind>`` URL.

    ``kind`` is one of ``join``, ``deposit``, ``withdraw`` (needs ``fund``) or
    ``vote`` (needs ``governor`` and ``proposal_id``). ``ts`` defaults to now.
    """
    ts_i = int(time.time()) if ts is None else int(ts)
    tg = _canonical_tg(tg_user_id)
    if kind in FUND_LINK_KINDS:
        if fund is None:
            raise ValueError(f"{kind} link needs fund")
        vault = canonical_address(fund)
        sig = sign_link(secret, fund_link_message(vault, tg, ts_i))
        return f"{_base(base_url)}/tg/{kind}?fund={vault}&tg={tg}&ts={ts_i}&sig={sig}"
    if kind == VOTE_LINK_KIND:
        if governor is None or proposal_id is None:
            raise ValueError("vote link needs governor and proposal_id")
        gov = canonical_address(governor)
        pid = canonical_proposal_id(proposal_id)
        sig = sign_link(secret, vote_link_message(gov, pid, tg, ts_i))
        proposal = quote(f"{gov}:{pid}", safe=":")
        return f"{_base(base_url)}/tg/vote?proposal={proposal}&tg={tg}&ts={ts_i}&sig={sig}"
    raise ValueError(f"unknown link kind: {kind!r}")


def fund_page_url(vault: str, base_url: str | None = None) -> str:
    """Public, unsigned fund page: ``${APP_BASE_URL}/tg/fund/<vault>``."""
    return f"{_base(base_url)}/tg/fund/{canonical_address(vault)}"


def verify_link_signature(
    secret: str | bytes,
    message: str,
    sig: str,
    *,
    ts: int,
    now: float | None = None,
    max_age_s: int = RECOMMENDED_LINK_TTL_S,
) -> bool:
    """Reference verifier (what the app lane should do): constant-time compare + freshness."""
    current = time.time() if now is None else now
    if int(ts) > current + 60 or current - int(ts) > max_age_s:
        return False
    expected = sign_link(secret, message)
    return hmac.compare_digest(expected, (sig or "").strip().lower())


# ---------------------------------------------------------------------------
# /start payloads (t.me/<bot>?start=<payload>) for the DM fallback.
# Telegram allows 1-64 chars of [A-Za-z0-9_-].
# ---------------------------------------------------------------------------

START_KINDS = {
    "join": "j",
    "deposit": "d",
    "withdraw": "w",
    "vote": "v",
    "propose": "p",
}
_START_KINDS_REV = {v: k for k, v in START_KINDS.items()}
_START_RE = re.compile(r"^sf([a-z])_([0-9a-f]{40})(?:_([0-9A-Za-z-]{1,40}))?$")
START_PAYLOAD_MAX = 64


def encode_start_payload(kind: str, vault: str, item_id: object | None = None) -> str:
    """``sf<k>_<vault hex without 0x>[_<item id>]``; raises ``ValueError`` if over 64 chars."""
    code = START_KINDS.get(kind)
    if code is None:
        raise ValueError(f"unknown start kind: {kind!r}")
    payload = f"sf{code}_{canonical_address(vault)[2:]}"
    if item_id is not None:
        item = str(item_id)
        if not re.match(r"^[0-9A-Za-z-]{1,40}$", item):
            raise ValueError(f"item id not start-payload safe: {item_id!r}")
        payload += f"_{item}"
    if len(payload) > START_PAYLOAD_MAX:
        raise ValueError("start payload over 64 chars")
    return payload


def decode_start_payload(payload: str) -> tuple[str, str, str | None] | None:
    """Inverse of :func:`encode_start_payload`; ``None`` when it is not ours."""
    m = _START_RE.match((payload or "").strip())
    if not m:
        return None
    kind = _START_KINDS_REV.get(m.group(1))
    if kind is None:
        return None
    return kind, "0x" + m.group(2), m.group(3)
