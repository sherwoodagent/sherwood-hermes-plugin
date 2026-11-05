"""Telegram id -> linked wallet, from the Sherwood app (sherwood-app #152).

After a member finishes ``/tg/join`` (Privy Telegram login + wallet), the app
knows which address that Telegram user linked for the fund:

    GET ${APP_BASE_URL}/api/tg/link?fund=<vault>&tg=<telegram_id>
    Authorization: Bearer <SHERWOOD_TG_API_TOKEN>

``SHERWOOD_TG_API_TOKEN`` is its own secret, shared with the app for this API
only (read through ``secrets.get_secret``, never logged). It is NOT the link
HMAC key: ``SHERWOOD_TG_LINK_SECRET`` signs personal and host links and never
leaves this process.

The request goes only to ``https://`` on an allowlisted host
(``fund.app_link_hosts``, default ``app.sherwood.sh``; operator config only),
and redirects are refused, so the token never reaches another origin.

The host's ✅ Approve then builds the approveDepositor body without anyone
typing an address; ``/fund-approve <tg> <address>`` stays the fallback when the
lookup returns nothing. Never raises: no token, a refused URL, any HTTP or
transport error, or an odd body all mean "not linked".
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

from . import secrets

_log = logging.getLogger(__name__)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
DEFAULT_APP_LINK_HOSTS = ("app.sherwood.sh",)

# (url, headers, timeout) -> (http_status, parsed_json_body)
GetJson = Callable[[str, dict[str, str], float], tuple[int, Any]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an answer, not an instruction: never follow it (the bearer would go along)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, method="GET", headers={"accept": "application/json", "user-agent": "sherwood-monitor", **headers})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - https + allowlisted host, checked by the caller
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as err:  # includes refused 3xx
        return err.code, {}


def _address_in(payload: Any) -> str | None:
    """`{"address"}`, `{"data": {"address"}}` or `{"linked": {"address"}}`; anything else is None."""
    node = payload
    for _ in range(3):
        if not isinstance(node, dict):
            return None
        addr = node.get("address")
        if isinstance(addr, str) and _ADDRESS_RE.match(addr):
            return addr
        node = node.get("data") if isinstance(node.get("data"), dict) else node.get("linked")
    return None


def parse_hosts(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    items = raw.split(",") if isinstance(raw, str) else list(raw or ())
    hosts = tuple(h.strip().lower() for h in items if h and h.strip())
    return hosts or DEFAULT_APP_LINK_HOSTS


class AppLinkClient:
    def __init__(
        self,
        base_url: str,
        *,
        allowed_hosts: str | Iterable[str] | None = None,
        token: Callable[[], str | None] | None = None,
        get_json: GetJson | None = None,
        timeout: float = 4.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.allowed_hosts = parse_hosts(allowed_hosts)
        self._token = token or (lambda: secrets.get_secret(secrets.SHERWOOD_TG_API_TOKEN))
        self._get_json = get_json
        self.timeout = timeout

    def base_ok(self) -> bool:
        """https:// on an allowlisted host, no credentials or port tricks in the URL."""
        try:
            parts = urllib.parse.urlsplit(self.base_url)
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and host in self.allowed_hosts
            and not parts.username
            and not parts.password
            and parts.port in (None, 443)
        )

    def linked_address(self, vault: str, telegram_id: str | int) -> str | None:
        """The wallet this Telegram user linked for the fund, or None."""
        try:
            return self._lookup(vault, telegram_id)
        except Exception as exc:  # never raises: http.client.HTTPException, ssl errors, anything
            _log.info("app link lookup failed: %s", type(exc).__name__)
            return None

    def _lookup(self, vault: str, telegram_id: str | int) -> str | None:
        tg = str(telegram_id or "").strip()
        if not self.base_url or not _ADDRESS_RE.match(vault or "") or not tg.lstrip("-").isdigit():
            return None
        if not self.base_ok():
            _log.warning("app link lookup refused: APP_BASE_URL must be https:// on %s", ", ".join(self.allowed_hosts))
            return None
        token = self._token()
        if not token:
            _log.debug("SHERWOOD_TG_API_TOKEN is not set; app link lookup skipped")
            return None
        query = urllib.parse.urlencode({"fund": vault.lower(), "tg": tg})
        url = f"{self.base_url}/api/tg/link?{query}"
        fetch = self._get_json or _http_get_json  # module lookup at call time (tests patch it)
        status, payload = fetch(url, {"authorization": f"Bearer {token}"}, self.timeout)
        if status != 200:
            return None
        return _address_in(payload)


def ensure_member_address(rt: Any, fund: str, who: str) -> str:
    """Fill in a member's wallet from the app link when fund state has none.

    Call it only after the host check. Returns the address now on record (""
    when neither fund state nor the app has one; the host then passes it:
    `/fund-approve <tg> <address>`).
    """
    state = rt.service.load(fund)
    member = state.member(str(who))
    if member is not None and member.address:
        return member.address
    tg = str(who or "")
    if not tg.lstrip("-").isdigit():
        return ""  # an address or an unknown id: nothing to look up
    client = getattr(rt, "applink", None)
    address = client.linked_address(state.fund.vault, tg) if client is not None else None
    if not address:
        return ""
    rt.service.request_membership(fund, tg, address=address)
    return address


def image_ready(url: str, *, allowed_hosts: str | Iterable[str] | None = None, get_status: Callable[[str, float], int] | None = None,
                timeout: float = 6.0) -> bool | None:
    """Is a share image (/tg/receipt|member|leaderboard/...png) servable yet?

    True on 200, None on 503 ("not readable yet": retry later, never send it, Telegram
    keeps the first image it fetches), False otherwise (refused URL, 404, a redirect,
    any error). Only https on an allowlisted app host; redirects are not followed.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in parse_hosts(allowed_hosts):
            return False
        status = (get_status or _image_status)(url, timeout)
    except Exception:
        return False
    if status == 200:
        return True
    if status == 503:
        return None
    return False


def _image_status(url: str, timeout: float) -> int:
    req = urllib.request.Request(url, method="GET", headers={"accept": "image/png", "user-agent": "sherwood-monitor"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - https + allowlisted host, checked above
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
