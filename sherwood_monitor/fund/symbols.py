"""Which stock-token symbols a portfolio draft may name: the tx-builder's `/sources/prices`.

That feed (sherwood app api/v1/sources, "Agent data feeds for Robinhood Chain") lists every
token the Sherwood token book has a Chainlink feed for on chain 4663, with its token
address (`data.records[]: {symbol, address, feed, price_usd, portfolio_price_ok, ...}`),
plus ETH (no token address) and USDG. PortfolioStrategy prices every leg from a Chainlink
feed, so a symbol missing from it (NFLX in CLI 0.103.0's book) cannot be proposed as a
leg. `portfolio_price_ok` is price freshness (it goes false over a weekend), not coverage,
so it is not used here.

Read with a short timeout and cached; never raises. Unreachable or unreadable -> None,
and the draft goes ahead with a note that the symbols were not checked.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)

PRICES_CHAIN_ID = 4663  # the feed reads Robinhood Chain only
TTL_S = 900.0  # the feed's own cadence
FAILURE_TTL_S = 60.0  # an unreachable feed is retried after this, not on every draft


MAX_BYTES = 512 * 1024  # the live feed is ~18 KB
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,15}$")
# CLI 0.103.0 prices a WETH or ETH portfolio leg from ETH_USD_FEED (buildInitDataForTemplate);
# the feed lists that feed as ETH with no token address.
ETH_FEED_SYMBOLS = frozenset({"ETH", "WETH"})


def _get_json(url: str, timeout: float) -> Any:
    return fetch_json(url, timeout)


def fetch_json(url: str, timeout: float) -> Any:
    """GET `url` as JSON, refusing a body over MAX_BYTES."""
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "sherwood-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError(f"prices source answered more than {MAX_BYTES} bytes")
        return json.loads(body.decode() or "{}")


def priced_symbols(payload: Any) -> frozenset[str] | None:
    """Upper-case symbols a portfolio leg may name, from a `/sources/prices` payload (None if
    unreadable): those listed with a token address, plus WETH/ETH when the ETH feed is listed.
    Anything that is not a plain ticker is dropped."""
    data = payload.get("data") if isinstance(payload, dict) else None
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return None
    out: set[str] = set()
    for r in records:
        if not isinstance(r, dict) or not isinstance(r.get("symbol"), str):
            continue
        symbol = r["symbol"].strip().upper()
        if not _SYMBOL_RE.match(symbol):
            continue
        if r.get("address"):
            out.add(symbol)
        elif symbol == "ETH" and r.get("feed"):
            out.update(ETH_FEED_SYMBOLS)
    return frozenset(out) if out else None


class PricedSymbols:
    """Cached reader of `{base_url}/sources/prices` -> symbols, or None when it cannot be read."""

    def __init__(self, base_url: str, *, timeout: float = 4.0, fetch: Callable[[str, float], Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.url = f"{base_url.rstrip('/')}/sources/prices"
        self.timeout = timeout
        self.clock = clock
        self._fetch = fetch
        self._value: frozenset[str] | None = None
        self._until = -1e18

    def __call__(self) -> frozenset[str] | None:
        now = self.clock()
        if now < self._until:
            return self._value
        try:
            self._value = priced_symbols((self._fetch or _get_json)(self.url, self.timeout))
        except Exception as exc:  # noqa: BLE001 - a feed outage must never fail a draft
            _log.warning("symbol check: %s could not be read (%s); drafts go ahead unchecked", self.url, exc)
            self._value = None
        self._until = now + (TTL_S if self._value is not None else FAILURE_TTL_S)
        return self._value


    def peek(self) -> frozenset[str] | None:
        """The cached symbols while still fresh, else None. Never reads the network (the Telegram
        message path uses this; a cold cache means "not checked here")."""
        return self._value if self.clock() < self._until else None


_SOURCES: dict[str, PricedSymbols] = {}


def source_for(base_url: str) -> PricedSymbols:
    if base_url not in _SOURCES:
        _SOURCES[base_url] = PricedSymbols(base_url)
    return _SOURCES[base_url]
