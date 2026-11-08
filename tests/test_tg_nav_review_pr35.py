"""The review of PR #35 (0.8.2): its repros, asserting the fixed cards.

- The read could not read the proposals (activeProposal null) while one is open: the card
  showed 0.8.1's fake loss again. Now redemptionsLocked plus the read's warning marks the
  cash as a floor ("NAV ≥60 USDG · P&L n/a").
- On Robinhood Chain the PortfolioStrategy has no positionValue() (a read-only cast call on
  the approved template reverts; its bytecode has no such selector), so the read returns
  nulls and the strategy counts at cost: the card says "(at cost)" and P&L n/a, not a flat
  +0.0% whatever the market did.
- Rounded rows missed NAV by a cent: the "Cash" row now takes the rounding remainder.

The reviewer's sixth repro (a fund with no vault yet) is not here: rendering any fund card
without a vault raises in the 📊 View button, unchanged since 0.8.0 and outside this fix.
"""
from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("telegram")

from sherwood_monitor.fund.service import FundService  # noqa: E402
from sherwood_monitor.fund.store import LocalJsonBackend  # noqa: E402
from sherwood_monitor.fund.telegram import fund_dict, fund_to_view  # noqa: E402
from sherwood_monitor.telegram.cards import render_fund_card  # noqa: E402
from tests.conftest import load_fixture  # noqa: E402
from tests.fund_helpers import Clock, fund, host_agent  # noqa: E402

LIVE = load_fixture("tg_card_open_usdg_100")
EXEC = load_fixture("tg_card_executed_usdg_synthetic")
AS_OF = 1794091159
UNREAD = "Could not read the vault's proposals."


def svc_for(tmp_path, clock, **kw):
    svc = FundService(LocalJsonBackend(tmp_path / "funds"), clock=clock)
    svc.create_fund(fund(open_deposits=True, **kw), host_agent())
    return svc


def card(svc, clock, **kw):
    text, _ = render_fund_card(fund_to_view(fund_dict(svc.load("alpha-fund"), now=clock(), **kw)), now=clock())
    return text


def test_v1_reality_no_position_value_counts_at_cost_and_says_so(tmp_path):
    clock = Clock(AS_OF + 3700.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(EXEC)
    c["data"]["activeProposal"].update(positionValue=None, positionValueValid=None)
    c["data"]["pnl"].update(unrealized=None, unrealizedDecimal=None)
    svc.apply_chain_card("alpha-fund", c)
    t = card(svc, clock)
    assert "NAV <b>100 USDG</b> (at cost) · P&amp;L <b>n/a</b>" in t  # 0.8.2 head: "P&L +0.0%"
    assert "• TSLA 24 USDG at cost\n• NVDA 16 USDG at cost\n• Cash 60 USDG\n" in t


def test_rows_add_up_to_the_nav_after_rounding(tmp_path):
    clock = Clock(AS_OF + 3700.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(EXEC)
    ap = c["data"]["activeProposal"]
    ap["positionValue"] = None
    ap["positionValueValid"] = None
    ap["positions"] = [dict(ap["positions"][0], symbol=s, investedAmount=v)
                       for s, v in (("A", "13333333"), ("B", "13333333"), ("C", "13333334"))]
    c["data"]["nav"].update(totalAssets="60004999", totalAssetsDecimal="60.004999")
    svc.apply_chain_card("alpha-fund", c)
    t = card(svc, clock)
    rows = [line for line in t.splitlines() if line.startswith("• ")]
    total = sum(float(line.split()[2].replace(",", "")) for line in rows)
    nav = float(t.splitlines()[1].split("<b>")[1].split()[0])
    assert rows[-1] == "• Cash 60.01 USDG"  # takes the cent the three 13.33 rows lose
    assert round(total, 2) == nav == 100.0  # 0.8.2 head: 99.99 vs 100


def test_proposals_unreadable_while_one_is_open_is_a_floor_not_a_loss(tmp_path):
    clock = Clock(AS_OF + 3700.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(EXEC)
    c["data"]["activeProposal"] = None
    c["data"]["proposals"] = {"count": "1", "open": [], "recent": []}
    c["data"]["warnings"] = [UNREAD]
    svc.apply_chain_card("alpha-fund", c)
    t = card(svc, clock)
    assert "NAV <b>≥60 USDG</b> · P&amp;L <b>n/a</b>" in t and "-40.0%" not in t  # 0.8.2 head: "NAV 60 · -40.0%"
    assert "• strategy value unreadable" in t
    assert t.endswith("<i>NAV counts the vault's cash only: the active strategy's value could not be read.</i>")


@pytest.mark.parametrize("locked, warnings", [
    (False, [UNREAD]),  # nothing open (redemptions unlocked): the cash is the NAV
    (True, []),  # proposals read fine, none executed (voting): the cash is the NAV
    (True, ["Could not read the governor's open-proposal count / cooldown."]),  # another section
])
def test_the_floor_needs_both_an_open_proposal_and_an_unread_proposals_section(tmp_path, locked, warnings):
    clock = Clock(AS_OF + 3700.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(EXEC)
    c["data"]["activeProposal"] = None
    c["data"]["fund"]["redemptionsLocked"] = locked
    c["data"]["warnings"] = warnings
    svc.apply_chain_card("alpha-fund", c)
    assert "NAV <b>60 USDG</b> · P&amp;L <b>-40.0%</b>" in card(svc, clock)  # what the read says, unflagged


def test_some_proposals_unread_counts_too(tmp_path):
    clock = Clock(AS_OF + 3700.0)
    svc = svc_for(tmp_path, clock)
    c = copy.deepcopy(EXEC)
    c["data"]["activeProposal"] = None
    c["data"]["warnings"] = ["Could not read some of the vault's proposals."]
    svc.apply_chain_card("alpha-fund", c)
    assert "NAV <b>≥60 USDG</b>" in card(svc, clock)


def test_081_snapshot_on_disk_still_renders(tmp_path):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    path = tmp_path / "funds" / "alpha-fund.json"
    raw = json.loads(path.read_text())
    raw["nav"] = {"total_assets": "100000000", "total_assets_decimal": "100", "total_supply": "100000000000000",
                  "share_price_decimal": "1", "idle_assets": "100000000", "realized_pnl_decimal": "0",
                  "unrealized_pnl_decimal": None, "as_of": float(AS_OF), "as_of_block": "108757783",
                  "read_at": AS_OF + 1.0}
    path.write_text(json.dumps(raw))
    assert "NAV <b>100 USDG</b> · P&amp;L <b>+0.0%</b>" in card(svc, clock)


def test_bad_stored_snapshot_dropped(tmp_path):
    clock = Clock(AS_OF + 41.0)
    svc = svc_for(tmp_path, clock)
    path = tmp_path / "funds" / "alpha-fund.json"
    raw = json.loads(path.read_text())
    raw["nav"] = {"total_assets_decimal": "1e400", "as_of": "NaN", "read_at": 1}
    path.write_text(json.dumps(raw))
    assert svc.load("alpha-fund").nav is None
    fund_dict(svc.load("alpha-fund"))  # no raise
