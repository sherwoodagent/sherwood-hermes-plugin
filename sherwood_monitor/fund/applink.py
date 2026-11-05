"""Telegram id -> linked wallet, from the Sherwood app (sherwood-app #152).

After a member finishes ``/tg/join`` (Privy Telegram login + wallet), the app
knows which address that Telegram user linked for the fund:

    GET ${APP_BASE_URL}/api/tg/link?fund=<vault>&tg=<telegram_id>
    Authorization: Bearer <SHERWOOD_TG_LINK_SECRET>

The bot and the app share that secret (read through ``secrets.get_secret``,
never logged). The host's ✅ Approve then builds the approveDepositor body
without anyone typing an address; ``/fund-approve <tg> <address>`` stays the
fallback when the lookup returns nothing.

Never raises: no secret, a 404, a timeout or an odd body all mean "not linked".
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import secrets

_log = logging.getLogger(__name__)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# (url, headers, timeout) -> (http_status, parsed_json_body)
GetJson = Callable[[str, dict[str, str], float], tuple[int, Any]]


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, method="GET", headers={"accept": "application/json", "user-agent": "sherwood-monitor", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https base URL from config
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as err:
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


class AppLinkClient:
    def __init__(
        self,
        base_url: str,
        *,
        secret: Callable[[], str | None] | None = None,
        get_json: GetJson | None = None,
        timeout: float = 4.0,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._secret = secret or (lambda: secrets.get_secret(secrets.SHERWOOD_TG_LINK_SECRET))
        self._get_json = get_json
        self.timeout = timeout

    def linked_address(self, vault: str, telegram_id: str | int) -> str | None:
        """The wallet this Telegram user linked for the fund, or None."""
        tg = str(telegram_id or "").strip()
        if not self.base_url or not _ADDRESS_RE.match(vault or "") or not tg.lstrip("-").isdigit():
            return None
        secret = self._secret()
        if not secret:
            _log.debug("SHERWOOD_TG_LINK_SECRET is not set; app link lookup skipped")
            return None
        query = urllib.parse.urlencode({"fund": vault.lower(), "tg": tg})
        url = f"{self.base_url}/api/tg/link?{query}"
        fetch = self._get_json or _http_get_json  # module lookup at call time (tests patch it)
        try:
            status, payload = fetch(url, {"authorization": f"Bearer {secret}"}, self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            _log.info("app link lookup for tg %s failed: %s", tg, exc)
            return None
        if status != 200:
            return None
        return _address_in(payload)


def ensure_member_address(rt: Any, fund: str, who: str) -> str:
    """Fill in a member's wallet from the app link when fund state has none.

    Returns the address now on record ("" when neither fund state nor the app
    has one; the host then passes it: `/fund-approve <tg> <address>`).
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
