"""Signed deep links from Telegram into the Sherwood web app (``/tg/*`` routes).

Contract (shared with the app lane; see ``docs/telegram.md`` for test vectors).
Every signed message starts with the link kind, so a link's signature is valid
for that route only (a join link can never be replayed as a withdraw link):

    ${APP_BASE_URL}/tg/<join|deposit|withdraw>?fund=<vault>&tg=<telegram_user_id>&ts=<unix_s>&sig=<hex>
        sig = hex(hmac_sha256(SHERWOOD_TG_LINK_SECRET, "<kind>|<vault>|<tg>|<ts>"))

    ${APP_BASE_URL}/tg/vote?proposal=<governor>:<proposal_id>&tg=<telegram_user_id>&ts=<unix_s>&sig=<hex>
        sig = hex(hmac_sha256(SHERWOOD_TG_LINK_SECRET, "vote|<governor>:<proposal_id>|<tg>|<ts>"))

    ${APP_BASE_URL}/tg/sign?fund=<vault>&action=<route>&body=<b64>&tg=<id>&ts=<unix_s>&sig=<hex>
        sig = hex(hmac_sha256(SHERWOOD_TG_LINK_SECRET, "sign|<vault>|<action>|<body>|<tg>|<ts>"))

Links are issued fresh on every tap. The app accepts ``ts`` up to 15 minutes old
for join and sign links, 24 hours for deposit / withdraw / vote, and up to 5
minutes in the future (``LINK_TTL_S``, ``LINK_FUTURE_SKEW_S``).

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
# What the app accepts (sherwood-app src/lib/tg-link.ts): join 15 min, deposit / withdraw / vote 24 h,
# host sign links 15 min, and 5 min of clock skew into the future.
LINK_TTL_S = {"join": 15 * 60, "deposit": 24 * 3600, "withdraw": 24 * 3600, "vote": 24 * 3600, "sign": 15 * 60}
LINK_FUTURE_SKEW_S = 5 * 60
RECOMMENDED_LINK_TTL_S = LINK_TTL_S["join"]  # the shortest, kept for callers that pass no kind


def link_ttl_s(kind: str) -> int:
    """How long the app accepts a link of this kind (host co-sign links are sign links)."""
    return LINK_TTL_S.get("sign" if kind == "propose" else kind, RECOMMENDED_LINK_TTL_S)

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


def fund_link_message(kind: str, vault: str, tg_user_id: object, ts: int) -> str:
    """The string signed for join / deposit / withdraw links: ``"<kind>|<vault>|<tg>|<ts>"``."""
    if kind not in FUND_LINK_KINDS:
        raise ValueError(f"not a fund link kind: {kind!r}")
    return f"{kind}|{canonical_address(vault)}|{_canonical_tg(tg_user_id)}|{int(ts)}"


def vote_link_message(governor: str, proposal_id: object, tg_user_id: object, ts: int) -> str:
    """The string signed for vote links: ``"vote|<governor>:<proposal_id>|<tg>|<ts>"``."""
    return (
        f"{VOTE_LINK_KIND}|{canonical_address(governor)}:{canonical_proposal_id(proposal_id)}"
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
        sig = sign_link(secret, fund_link_message(kind, vault, tg, ts_i))
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


# ---------------------------------------------------------------------------
# Host signing page: ${APP_BASE_URL}/tg/sign (the app lane adds the route).
# ---------------------------------------------------------------------------

SIGN_ACTIONS = ("approve-depositor", "approve-depositors", "register-agent", "propose-portfolio", "propose", "set-agent-fee")
# A URL button this long still opens in every Telegram client we tried; past it,
# hand the host the CLI path instead of a link.
SIGN_URL_MAX = 2048


def encode_sign_body(body: dict) -> str:
    """Compact JSON, base64url without padding: the prepare body the page posts."""
    import base64
    import json

    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_sign_body(encoded: str) -> dict:
    import base64
    import json

    pad = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + pad).decode("utf-8"))


SIGN_LINK_KIND = "sign"


def sign_link_message(vault: str, action: str, body_b64: str, tg_user_id: object, ts: int) -> str:
    """The string signed for host signing links: ``"sign|<vault>|<action>|<body>|<tg>|<ts>"``."""
    if action not in SIGN_ACTIONS:
        raise ValueError(f"unknown signing action: {action!r}")
    return f"{SIGN_LINK_KIND}|{canonical_address(vault)}|{action}|{body_b64}|{_canonical_tg(tg_user_id)}|{int(ts)}"


def build_sign_link(
    *,
    vault: str,
    action: str,
    body: dict,
    tg_user_id: object,
    secret: str | bytes,
    base_url: str | None = None,
    ts: int | None = None,
) -> str | None:
    """``${APP_BASE_URL}/tg/sign?fund=<vault>&action=<route>&body=<b64>&tg=<id>&ts=<s>&sig=<hex>``.

    The page decodes ``body`` (the exact ``POST /api/v1/prepare/<action>`` body),
    verifies ``sig`` over ``"sign|<vault>|<action>|<body>|<tg>|<ts>"``, checks the
    Telegram login is ``tg``, prepares through the tx-builder and has the
    host's wallet sign. Returns None when the URL would exceed SIGN_URL_MAX.
    """
    ts_i = int(time.time()) if ts is None else int(ts)
    v = canonical_address(vault)
    tg = _canonical_tg(tg_user_id)
    encoded = encode_sign_body(body)
    sig = sign_link(secret, sign_link_message(v, action, encoded, tg, ts_i))
    url = f"{_base(base_url)}/tg/sign?fund={v}&action={action}&body={encoded}&tg={tg}&ts={ts_i}&sig={sig}"
    return url if len(url) <= SIGN_URL_MAX else None


# ---------------------------------------------------------------------------
# Share images (sherwood-app #153, docs/tg-cards.md): bot-signed social fields,
# same SHERWOOD_TG_LINK_SECRET, HMAC-SHA256 hex, no timestamp (a card URL stays valid).
#
#   receipt|<Vault>:<proposalId>|<by>|<backers>   /tg/receipt/<Vault>/<proposalId>.png?by=&backers=&sig=
#   member|<Vault>:<n>|<handle>|<joined>          /tg/member/<Vault>/<n>.png?handle=&joined=&sig=
#   attrib|<Vault>|<attrib>                       /tg/leaderboard/<Vault>.png?window=all&attrib=&sig=
#   (unsigned)                                    /tg/fund/<Vault>/card.png[?square=1]
#
# <Vault> is the EIP-55 checksum address, exactly as in the URL path (the app
# redirects other casings, and Telegram keeps the first image it fetches). A
# missing field signs as "". Handles are Telegram username characters only, no "@".
# <n> is the member's deposit order on chain, never Telegram join order.
# ---------------------------------------------------------------------------

_HANDLE_RE = re.compile(r"[^A-Za-z0-9_]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ATTRIB_MAX_PAIRS = 100


def checksum_address(address: str) -> str:
    """EIP-55. Needs a keccak (eth-utils, or pycryptodome): the plugin's declared deps."""
    lower = canonical_address(address)[2:]
    try:
        from eth_utils import keccak  # type: ignore[import-not-found]

        digest = keccak(text=lower).hex()
    except ImportError:
        from Crypto.Hash import keccak as _k  # type: ignore[import-not-found]

        digest = _k.new(digest_bits=256, data=lower.encode()).hexdigest()
    return "0x" + "".join(c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(lower))


def social_handle(handle: object) -> str:
    """A Telegram username for a card: no "@", only [A-Za-z0-9_], at most 32 chars ("" when none)."""
    return _HANDLE_RE.sub("", str(handle or "").lstrip("@"))[:32]


def _proposal_ref(proposal_id: object) -> str:
    pid = str(proposal_id).strip()
    if not pid.isdigit():
        raise ValueError(f"share cards need the on-chain proposal id, got {proposal_id!r}")
    return pid


def receipt_image_message(vault: str, proposal_id: object, by: object = "", backers: object = "") -> str:
    by_s = social_handle(by)
    backers_s = str(int(backers)) if str(backers).strip() not in ("", "None") else ""
    return f"receipt|{checksum_address(vault)}:{_proposal_ref(proposal_id)}|{by_s}|{backers_s}"


def member_image_message(vault: str, n: object, handle: object = "", joined: str = "") -> str:
    if joined and not _DATE_RE.match(joined):
        raise ValueError("joined must be YYYY-MM-DD")
    return f"member|{checksum_address(vault)}:{int(n)}|{social_handle(handle)}|{joined or ''}"


def leaderboard_attrib(pairs: list[tuple[object, object]]) -> str:
    """``8:alice,5:bob``: (on-chain proposal id, idea author's handle), at most 100 pairs."""
    out = []
    for pid, handle in pairs[:ATTRIB_MAX_PAIRS]:
        h = social_handle(handle)
        if h and str(pid).strip().isdigit():
            out.append(f"{int(pid)}:{h}")
    return ",".join(out)


def attrib_image_message(vault: str, attrib: str) -> str:
    return f"attrib|{checksum_address(vault)}|{attrib or ''}"


def _image_url(path: str, params: list[tuple[str, str]], base_url: str | None) -> str:
    from urllib.parse import urlencode

    query = urlencode([(k, v) for k, v in params if v != ""], safe=":,")
    return f"{_base(base_url)}{path}" + (f"?{query}" if query else "")


def receipt_image_url(
    vault: str, proposal_id: object, *, by: object = "", backers: object = "", secret: str | bytes, base_url: str | None = None
) -> str:
    v = checksum_address(vault)
    pid = _proposal_ref(proposal_id)
    msg = receipt_image_message(v, pid, by, backers)
    _, _, by_s, backers_s = msg.split("|")
    sig = sign_link(secret, msg)
    return _image_url(f"/tg/receipt/{v}/{pid}.png", [("by", by_s), ("backers", backers_s), ("sig", sig)], base_url)


def member_image_url(
    vault: str, n: object, *, handle: object = "", joined: str = "", secret: str | bytes, base_url: str | None = None
) -> str:
    v = checksum_address(vault)
    msg = member_image_message(v, n, handle, joined)
    sig = sign_link(secret, msg)
    return _image_url(f"/tg/member/{v}/{int(n)}.png", [("handle", social_handle(handle)), ("joined", joined or ""), ("sig", sig)], base_url)


def leaderboard_image_url(vault: str, attrib: str, *, secret: str | bytes, base_url: str | None = None, window: str = "all") -> str:
    v = checksum_address(vault)
    sig = sign_link(secret, attrib_image_message(v, attrib))
    return _image_url(f"/tg/leaderboard/{v}.png", [("window", window), ("attrib", attrib or ""), ("sig", sig)], base_url)


def fund_card_image_url(vault: str, *, square: bool = False, base_url: str | None = None) -> str:
    """Unsigned fund card image."""
    v = checksum_address(vault)
    return f"{_base(base_url)}/tg/fund/{v}/card.png" + ("?square=1" if square else "")


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
    future_skew_s: int = LINK_FUTURE_SKEW_S,
) -> bool:
    """Reference verifier (what the app lane should do): constant-time compare + freshness."""
    current = time.time() if now is None else now
    if int(ts) > current + future_skew_s or current - int(ts) > max_age_s:
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
    "position": "m",  # 👤 My position: the widget, no link
}
_START_KINDS_REV = {v: k for k, v in START_KINDS.items()}
_START_RE = re.compile(r"^sf([a-z])_([0-9a-f]{40})(?:_([0-9A-Za-z_-]{1,40}))?$")
START_PAYLOAD_MAX = 64


def encode_start_payload(kind: str, vault: str, item_id: object | None = None) -> str:
    """``sf<k>_<vault hex without 0x>[_<item id>]``; raises ``ValueError`` if over 64 chars."""
    code = START_KINDS.get(kind)
    if code is None:
        raise ValueError(f"unknown start kind: {kind!r}")
    payload = f"sf{code}_{canonical_address(vault)[2:]}"
    if item_id is not None:
        item = str(item_id)
        if not re.match(r"^[0-9A-Za-z_-]{1,40}$", item):
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
