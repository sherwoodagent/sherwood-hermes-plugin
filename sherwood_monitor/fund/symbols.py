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
import time
import urllib.request
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)

PRICES_CHAIN_ID = 4663  # the feed reads Robinhood Chain only
TTL_S = 900.0  # the feed's own cadence
FAILURE_TTL_S = 60.0  # an unreachable feed is retried after this, not on every draft


def _get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "sherwood-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def priced_symbols(payload: Any) -> frozenset[str] | None:
    """Upper-case symbols with a token address in a `/sources/prices` payload, or None if unreadable."""
    data = payload.get("data") if isinstance(payload, dict) else None
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return None
    out = {
        str(r["symbol"]).upper()
        for r in records
        if isinstance(r, dict) and r.get("symbol") and r.get("address")
    }
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


_SOURCES: dict[str, PricedSymbols] = {}


def source_for(base_url: str) -> PricedSymbols:
    if base_url not in _SOURCES:
        _SOURCES[base_url] = PricedSymbols(base_url)
    return _SOURCES[base_url]
