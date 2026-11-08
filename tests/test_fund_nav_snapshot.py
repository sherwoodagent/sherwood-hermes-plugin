"""0.8.1: the NAV snapshot behind the Telegram fund card (no PTB needed here).

Every tg-card read stores the vault's NAV, P&L and asOf in the fund state, and the
card's view maps it (tests/test_tg_nav_card.py renders it). The fixture
`fixtures/tg_card_open_usdg_100.json` is a real read (2026-11-07, chain 4663): an
open-deposit USDG vault holding 100 USDG after one deposit, nobody joined in chat.
"""
from __future__ import annotations

import json

import pytest

from sherwood_monitor.fund.models import FundState, NavSnapshot, share_price_pnl_bps
from sherwood_monitor.fund.service import FundService
from sherwood_monitor.fund.store import LocalJsonBackend
from sherwood_monitor.fund.telegram import NAV_STALE_AFTER_S, fund_dict, fund_to_view
from tests.conftest import load_fixture
from tests.fund_helpers import Clock, fund, host_agent

LIVE = load_fixture("tg_card_open_usdg_100")
AS_OF = 1794091159  # the fixture's asOf.timestamp: 2026-11-07T22:39:19Z


def make_service(tmp_path, clock, **fund_kw) -> FundService:
    svc = FundService(LocalJsonBackend(tmp_path / "funds"), clock=clock)
    svc.create_fund(fund(open_deposits=True, **fund_kw), host_agent())
    return svc


@pytest.fixture
def clock():
    return Clock(AS_OF + 41.0)


def card_with(**data) -> dict:
    """The live read with some top-level `data` sections replaced (None removes one)."""
    raw = json.loads(json.dumps(LIVE["data"]))
    for key, value in data.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    return raw


# -- P&L: share-price based ---------------------------------------------------------------


@pytest.mark.parametrize(
    "price, bps",
    [
        ("1", 0),
        ("1.0123", 123),
        ("0.98", -200),
        ("1.25", 2500),
        ("1.00005", 1),  # half a bp rounds away from zero
        ("0.99995", -1),
        ("0", -10_000),  # everything lost
        ("-3", -10_000),  # clamped: a share price is never below 0
        ("1000", 1_000_000),  # clamped: past +10,000% is a bad read
        (None, 0),  # no shares outstanding: the read sends null
        ("", 0),
        ("abc", 0),
        ("NaN", 0),
        ("Infinity", 0),
    ],
)
def test_pnl_bps_is_the_share_price_against_one(price, bps):
    assert share_price_pnl_bps(price) == bps


# -- the snapshot: stored from every read, persisted --------------------------------------


def test_the_live_read_becomes_the_nav_snapshot(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", LIVE)  # the envelope or its data: both are accepted
    nav = svc.load("alpha-fund").nav
    assert nav == NavSnapshot(
        total_assets="100000000", total_assets_decimal="100", total_supply="100000000000000",
        share_price_decimal="1", idle_assets="100000000", realized_pnl_decimal="0", unrealized_pnl_decimal=None,
        as_of=float(AS_OF), as_of_block="108757783", read_at=clock(), asset_decimals=6, share_decimals=12,
    )
    assert nav.nav == 100 and nav.pnl_bps == 0 and nav.shares_outstanding
    assert not nav.active_proposal_id and nav.position_rows() == []  # cash only: "none yet"
    # Persisted in the fund JSON, and read back the same.
    on_disk = json.loads((tmp_path / "funds" / "alpha-fund.json").read_text())
    assert on_disk["nav"]["total_assets_decimal"] == "100" and on_disk["schema_version"] == 1
    assert FundState.from_dict(on_disk).nav == nav


def test_state_written_before_0_8_1_has_no_snapshot():
    raw = FundState(fund=fund()).to_dict()
    raw.pop("nav")
    st = FundState.from_dict(raw)
    assert st.nav is None and st.to_dict()["nav"] is None
    assert NavSnapshot.from_dict("garbage") is None
    assert NavSnapshot.from_dict({"total_assets_decimal": "5", "read_at": "not a time"}) is None


def test_a_read_without_a_usable_nav_keeps_the_snapshot(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", LIVE)
    before = svc.load("alpha-fund").nav
    clock.advance(60)
    svc.apply_chain_card("alpha-fund", card_with(nav=None))
    svc.apply_chain_card("alpha-fund", card_with(nav={"totalAssets": "abc", "totalAssetsDecimal": None}))
    svc.apply_chain_card("alpha-fund", {"data": {"proposeGate": {"cooldownUntil": "0"}}})  # pre-0.8.1 test shape
    assert svc.load("alpha-fund").nav == before


def test_the_decimal_comes_from_base_units_when_the_read_omits_it(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", card_with(nav={"totalAssets": "1500000", "totalSupply": "0", "sharePrice": None,
                                                      "sharePriceDecimal": None}))
    nav = svc.load("alpha-fund").nav
    assert nav.total_assets_decimal == "1.5" and nav.pnl_bps == 0 and not nav.shares_outstanding


def test_a_cached_read_of_an_older_block_never_replaces_a_newer_snapshot(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", LIVE)
    older = card_with(asOf={"block": "108757700", "timestamp": str(AS_OF - 10)},
                      nav={**LIVE["data"]["nav"], "totalAssetsDecimal": "90", "totalAssets": "90000000"})
    svc.apply_chain_card("alpha-fund", older)
    assert svc.load("alpha-fund").nav.total_assets_decimal == "100"
    newer = card_with(asOf={"block": "108757800", "timestamp": str(AS_OF + 30)},
                      nav={**LIVE["data"]["nav"], "totalAssets": "101500000", "totalAssetsDecimal": "101.5"})
    svc.apply_chain_card("alpha-fund", newer)
    assert svc.load("alpha-fund").nav.total_assets_decimal == "101.5"


# -- view fields ----------------------------------------------------------------------------


def test_the_view_carries_nav_pnl_and_supply(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    view = fund_to_view(fund_dict(svc.load("alpha-fund")))
    assert (view.nav, view.pnl_bps, view.nav_as_of, view.nav_stale, view.shares_outstanding) == (0, 0, None, False, False)
    # P&L is NAV per share against 1 (101.23 USDG over 100 shares), not the read's cash-only sharePriceDecimal.
    svc.apply_chain_card("alpha-fund", card_with(nav={**LIVE["data"]["nav"], "totalAssets": "101230000",
                                                      "sharePriceDecimal": "1"}))
    st = svc.load("alpha-fund")
    view = fund_to_view(fund_dict(st))
    assert (view.nav, view.pnl_bps, view.nav_as_of, view.nav_stale, view.shares_outstanding) == (101.23, 123, AS_OF, False, True)
    assert view.nav_symbol == "USDG" and view.deposits_open and view.positions == ()
    # Only the fund card's own read flags a snapshot stale: a failed read, or an old snapshot.
    assert fund_to_view(fund_dict(st, chain_failed=True)).nav_stale
    assert not fund_to_view(fund_dict(st, now=clock() + NAV_STALE_AFTER_S)).nav_stale
    assert fund_to_view(fund_dict(st, now=clock() + NAV_STALE_AFTER_S + 1)).nav_stale


def test_a_positions_list_is_still_parsed_if_one_ever_arrives():
    """The read has no positions list today (the card says "none yet"); the view's parser stays."""
    raw = {"name": "alpha-fund", "vault": "0x" + "11" * 20, "nav_symbol": "USDG",
           "positions": [{"symbol": "TSLA", "value": 40, "pnl_bps": 120}]}
    assert [(p.symbol, p.value, p.pnl_bps) for p in fund_to_view(raw).positions] == [("TSLA", 40.0, 120)]
