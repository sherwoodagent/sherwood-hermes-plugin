"""The adversarial review of PR #33 (0.8.1 NAV card): its 18 repros, asserting the 0.8.2 behavior.

Each test is the reviewer's case. Where 0.8.1 rendered something wrong (a cash-only NAV with
a strategy live, a raise on a broken read), the assertion is now the fixed card. The
share-price edge cases vary `totalAssets` instead of `sharePriceDecimal`: P&L is NAV per
share against 1, so the read's cash-only share price no longer drives it.
"""
from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("telegram")

from sherwood_monitor.fund.models import FundState, ProposalRecord  # noqa: E402
from sherwood_monitor.fund.service import FundService  # noqa: E402
from sherwood_monitor.fund.store import LocalJsonBackend  # noqa: E402
from sherwood_monitor.fund.telegram import fund_dict, fund_to_view, proposal_dict, proposal_to_view  # noqa: E402
from sherwood_monitor.telegram.cards import goal_bar, render_draft_card, render_fund_card  # noqa: E402
from tests.conftest import load_fixture  # noqa: E402
from tests.fund_helpers import Clock, fund, host_agent  # noqa: E402

LIVE = load_fixture("tg_card_open_usdg_100")
AS_OF = 1794091159


def svc_for(tmp_path, clock):
    svc = FundService(LocalJsonBackend(tmp_path / "funds"), clock=clock)
    svc.create_fund(fund(open_deposits=True), host_agent())
    return svc


def card_text(svc, clock, **kw):
    text, _ = render_fund_card(fund_to_view(fund_dict(svc.load("alpha-fund"), now=clock(), **kw)), now=clock())
    return text


def with_nav(**nav):
    """The live read with `nav` fields replaced (None removes one)."""
    c = copy.deepcopy(LIVE)
    c["data"]["nav"].update(nav)
    c["data"]["nav"] = {k: v for k, v in c["data"]["nav"].items() if v is not None}
    return c


def test_baseline_live_fixture(tmp_path):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", LIVE)
    assert "NAV <b>100 USDG</b> · P&amp;L <b>+0.0%</b>" in card_text(svc, clock)


def test_an_active_strategy_counts_its_capital_and_shows_its_position(tmp_path):
    """totalAssets() is the vault's cash; the strategy took 40 of 100 USDG out at execute. 0.8.1
    rendered "NAV 60 USDG · P&L -40.0%" and "none yet"; the fund is 100.5 USDG, +0.5%."""
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(LIVE)
    d = c["data"]
    d["fund"]["totalAssets"] = "60000000"
    d["fund"]["redemptionsLocked"] = True
    d["nav"].update(totalAssets="60000000", totalAssetsDecimal="60", sharePrice="600000",
                    sharePriceDecimal="0.6", idleAssets="60000000")
    d["pnl"].update(unrealized="500000", unrealizedDecimal="0.5")
    d["activeProposal"] = {"id": "1", "strategy": "0x" + "ab" * 20, "positionValue": "40500000",
                           "positionValueValid": True,
                           "positions": [{"token": "0x" + "cd" * 20, "symbol": "TSLA", "decimals": 18,
                                          "targetWeightBps": 10000, "tokenAmount": "1", "investedAmount": "40000000"}]}
    svc.apply_chain_card("alpha-fund", c)
    t = card_text(svc, clock)
    assert "NAV <b>100.5 USDG</b> · P&amp;L <b>+0.5%</b>" in t
    # One token: its live value and P&L (40.5 / 40 = +125 bps, printed half-even as +1.2%), then the cash.
    assert "<b>Positions</b>\n• TSLA 40.5 USDG (+1.2%)\n• Cash 60 USDG\n" in t
    assert "none yet" not in t


def test_goal_bar_units_at_nav_100(tmp_path):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", LIVE)
    st = svc.load("alpha-fund")
    rec = ProposalRecord(id="p-1", proposer_agent="alpha", thesis="t", status="draft",
                         max_capital="40000000", max_drawdown_bps=1000)
    t, _ = render_draft_card(proposal_to_view(proposal_dict(st, rec)), fund=fund_to_view(fund_dict(st)))
    assert "Max capital <b>40 USDG</b>" in t and "40% of NAV" in t
    assert goal_bar(500, 100) == "▮▮▮▮▮▮▮▮▮▮ 500% of NAV"
    assert goal_bar(40, 0.01) == "▮▮▮▮▮▮▮▮▮▮ >999% of NAV"  # 0.8.1: "400000% of NAV"


@pytest.mark.parametrize("label,nav,expect", [
    ("numbers not strings", dict(totalAssets=100000000, totalAssetsDecimal=100, sharePriceDecimal=1,
                                 totalSupply=100000000000000), "NAV <b>100 USDG</b> · P&amp;L <b>+0.0%</b>"),
    # Base units win over the decimal text; with no base units the decimal (even a float) is used.
    ("float total", dict(totalAssets=None, totalAssetsDecimal=40.5), "NAV <b>40.5 USDG</b>"),
    ("sci notation", dict(totalAssets=None, totalAssetsDecimal="1e+2"), "NAV <b>100 USDG</b>"),
    ("null share price", dict(sharePriceDecimal=None), "P&amp;L <b>+0.0%</b>"),
    ("nothing left", dict(totalAssets="0", totalAssetsDecimal="0", sharePriceDecimal="0"),
     "NAV <b>0 USDG</b> · P&amp;L <b>-100.0%</b>"),
    ("negative pnl", dict(totalAssets="98765000", sharePriceDecimal="0.98765"), "P&amp;L <b>-1.2%</b>"),
    ("huge share price", dict(totalAssets="1000000000000000"), "P&amp;L <b>+10000.0%</b>"),  # clamped
    ("supply 0 with assets (donation)", dict(totalSupply="0", sharePriceDecimal=None),
     "NAV <b>100 USDG</b> · P&amp;L <b>+0.0%</b>"),
    ("NaN text", dict(totalAssetsDecimal="NaN", totalAssets="100000000"), "NAV <b>100 USDG</b>"),
    ("garbage decimal falls back to base units", dict(totalAssetsDecimal="abc", totalAssets="40500000"),
     "NAV <b>40.5 USDG</b>"),
])
def test_edge_values(tmp_path, label, nav, expect):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", with_nav(**nav))
    t = card_text(svc, clock)
    assert expect in t
    assert "nan" not in t.lower() and "inf" not in t.lower()


@pytest.mark.parametrize("nav", [dict(totalAssets=None, totalAssetsDecimal="1e400"),
                                 dict(totalAssets="1" + "0" * 400, totalAssetsDecimal=None)])
def test_an_absurd_total_is_not_stored_and_never_crashes_the_card(tmp_path, nav):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", with_nav(**nav))  # 0.8.1: stored, then OverflowError in fmt_amount
    assert svc.load("alpha-fund").nav is None
    assert "NAV <b>unavailable</b>" in card_text(svc, clock)


def test_a_millisecond_timestamp_does_not_poison_the_snapshot(tmp_path):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(LIVE)
    c["data"]["asOf"]["timestamp"] = str(AS_OF * 1000)
    svc.apply_chain_card("alpha-fund", c)
    assert svc.load("alpha-fund").nav.as_of is None  # not a time in seconds: dropped, the block stays
    c2 = with_nav(totalAssetsDecimal="150", totalAssets="150000000")
    c2["data"]["asOf"]["timestamp"] = str(AS_OF + 60)
    svc.apply_chain_card("alpha-fund", c2)  # same block, later time: the newer read lands
    assert svc.load("alpha-fund").nav.total_assets_decimal == "150"
    t = card_text(svc, clock, chain_failed=True)  # 0.8.1: ValueError (year out of range)
    assert "NAV <b>150 USDG</b>" in t and "<i>Last known NAV, as of 22:40 UTC.</i>" in t


def test_a_read_without_a_nav_block_and_no_snapshot_is_unavailable_not_zero(tmp_path):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(LIVE)
    del c["data"]["nav"]
    svc.apply_chain_card("alpha-fund", c)
    assert "NAV <b>unavailable</b> · P&amp;L <b>n/a</b>" in card_text(svc, clock)  # 0.8.1: "NAV 0 USDG"


def test_a_nan_timestamp_never_breaks_fund_dict(tmp_path):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(LIVE)
    c["data"]["asOf"]["timestamp"] = "NaN"
    svc.apply_chain_card("alpha-fund", c)
    d = fund_dict(svc.load("alpha-fund"))  # 0.8.1: ValueError on int(nan), for every handler
    assert d["nav"] == 100.0 and d["nav_as_of"] == int(clock())  # no block time: when it was stored


def test_080_file_loads_and_roundtrips(tmp_path):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    path = tmp_path / "funds" / "alpha-fund.json"
    raw = json.loads(path.read_text())
    raw.pop("nav", None)
    path.write_text(json.dumps(raw))
    assert svc.load("alpha-fund").nav is None
    svc.apply_chain_card("alpha-fund", LIVE)
    raw2 = json.loads(path.read_text())
    assert raw2["nav"]["total_assets_decimal"] == "100"
    assert FundState.from_dict(raw2).to_dict() == raw2
