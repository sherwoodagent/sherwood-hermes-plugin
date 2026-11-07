"""0.8.2: NAV counts the capital an executed strategy holds, and the card lists its positions.

The vault's `totalAssets()` is only the cash it holds: a PortfolioStrategy takes the capital
out at execute. The tg-card read carries the rest in `activeProposal` (`positionValue`,
`positionValueValid`, `positions[].investedAmount`, all in asset base units).
`fixtures/tg_card_executed_usdg_synthetic.json` is SYNTHETIC (no proposal has executed on the
live vault yet), built from the live read in tg-card.ts's shape: 40 of 100 USDG deployed
(TSLA 60% / NVDA 40%), now worth 40.6 USDG, 60 USDG cash in the vault.
"""
from __future__ import annotations

import copy
import json
import math
from decimal import Decimal

import pytest

pytest.importorskip("telegram")

from sherwood_monitor.fund.models import FundState, NavSnapshot, ProposalRecord  # noqa: E402
from sherwood_monitor.fund.service import FundService  # noqa: E402
from sherwood_monitor.fund.store import LocalJsonBackend  # noqa: E402
from sherwood_monitor.fund.telegram import fund_dict, fund_to_view, proposal_dict, proposal_to_view  # noqa: E402
from sherwood_monitor.telegram.cards import fmt_amount, goal_bar, nav_notice, render_draft_card, render_fund_card  # noqa: E402
from sherwood_monitor.telegram.types import FundView  # noqa: E402
from tests.conftest import load_fixture  # noqa: E402
from tests.fund_helpers import Clock, fund, host_agent  # noqa: E402

LIVE = load_fixture("tg_card_open_usdg_100")
EXECUTED = load_fixture("tg_card_executed_usdg_synthetic")
AS_OF = 1794091159
EXEC_AS_OF = AS_OF + 3600  # 23:39:19 UTC


@pytest.fixture
def clock():
    return Clock(EXEC_AS_OF + 41.0)


@pytest.fixture
def svc(tmp_path, clock):
    s = FundService(LocalJsonBackend(tmp_path / "funds"), clock=clock)
    s.create_fund(fund(open_deposits=True), host_agent())
    return s


def executed(**active) -> dict:
    """The executed read with `activeProposal` fields replaced (None removes one)."""
    c = copy.deepcopy(EXECUTED)
    a = c["data"]["activeProposal"]
    a.update(active)
    c["data"]["activeProposal"] = {k: v for k, v in a.items() if v is not None}
    return c


def card(svc, clock, **kw) -> str:
    return render_fund_card(fund_to_view(fund_dict(svc.load("alpha-fund"), now=clock(), **kw)), now=clock())[0]


def test_the_executed_read_counts_deployed_capital_and_lists_positions(svc, clock):
    svc.apply_chain_card("alpha-fund", EXECUTED)
    snap = svc.load("alpha-fund").nav
    assert (snap.active_proposal_id, snap.position_value, snap.position_value_valid) == ("1", "40600000", True)
    assert [(p.symbol, p.invested, p.target_weight_bps) for p in snap.positions] == [
        ("TSLA", "24000000", 6000), ("NVDA", "16000000", 4000)]
    assert snap.nav == Decimal("100.6") and snap.pnl_bps == 60  # the read's own sharePriceDecimal says 0.6
    assert card(svc, clock) == (
        "🏦 <b>alpha-fund</b>\n"
        "NAV <b>100.6 USDG</b> · P&amp;L <b>+0.6%</b>\n"
        "\n"
        "<b>Positions</b>\n"
        "• TSLA 24 USDG at cost\n"
        "• NVDA 16 USDG at cost\n"
        "• Unrealized 0.6 USDG (+1.5%)\n"
        "• USDG 60 USDG\n"
        "\n"
        "Members 0 (depositors haven't tapped 🎟 Join yet) · Agents alpha"
    )


def test_the_position_rows_add_up_to_the_nav(svc, clock):
    for read in (EXECUTED, executed(positionValueValid=False), executed(positions=[executed()["data"]["activeProposal"]["positions"][0]])):
        svc.apply_chain_card("alpha-fund", read)
        view = fund_to_view(fund_dict(svc.load("alpha-fund")))
        assert math.isclose(sum(p.value for p in view.positions), view.nav)


def test_an_unpriced_strategy_counts_at_cost(svc, clock):
    svc.apply_chain_card("alpha-fund", executed(positionValueValid=False))
    t = card(svc, clock)
    assert "NAV <b>100 USDG</b> · P&amp;L <b>+0.0%</b>" in t
    assert "• TSLA 24 USDG at cost\n• NVDA 16 USDG at cost\n• USDG 60 USDG\n" in t and "Unrealized" not in t


def test_a_one_token_strategy_shows_its_live_value(svc, clock):
    one = executed(positionValue="40600000", positions=[{**EXECUTED["data"]["activeProposal"]["positions"][0],
                                                          "investedAmount": "40000000", "symbol": None}])
    svc.apply_chain_card("alpha-fund", one)
    t = card(svc, clock)
    assert "• 0xa1a1…a1a1 40.6 USDG (+1.5%)\n• USDG 60 USDG\n" in t  # no symbol: the short token address


def test_a_strategy_whose_value_cannot_be_read_is_a_floor_not_a_loss(svc, clock):
    """activeProposal present, positionValue and positions both failed soft: the cash is not the NAV."""
    svc.apply_chain_card("alpha-fund", executed(positionValue=None, positionValueValid=None, positions=None))
    snap = svc.load("alpha-fund").nav
    assert snap.nav_partial and snap.pnl_bps == 0
    t = card(svc, clock)
    assert "NAV <b>≥60 USDG</b> · P&amp;L <b>n/a</b>" in t and "-40" not in t
    assert "• strategy value unreadable" in t
    assert t.endswith("<i>NAV counts the vault's cash only: the active strategy's value could not be read.</i>")


def test_a_draft_is_sized_against_the_whole_nav(svc, clock):
    svc.apply_chain_card("alpha-fund", EXECUTED)
    st = svc.load("alpha-fund")
    rec = ProposalRecord(id="p-2", proposer_agent="alpha", thesis="t", status="draft", max_capital="40000000")
    text, _ = render_draft_card(proposal_to_view(proposal_dict(st, rec)), fund=fund_to_view(fund_dict(st)))
    assert "40% of NAV" in text  # 40 / 100.6; against the cash alone it would say 67%


def test_settling_back_to_cash_only_clears_the_positions(svc, clock):
    svc.apply_chain_card("alpha-fund", EXECUTED)
    later = copy.deepcopy(LIVE)
    later["data"]["asOf"] = {"block": "108800000", "timestamp": str(EXEC_AS_OF + 600)}
    later["data"]["nav"].update(totalAssets="100600000")
    svc.apply_chain_card("alpha-fund", later)
    t = card(svc, clock)
    assert "NAV <b>100.6 USDG</b> · P&amp;L <b>+0.6%</b>" in t and "• none yet" in t


def test_a_snapshot_stored_by_0_8_1_still_renders(svc, clock, tmp_path):
    path = tmp_path / "funds" / "alpha-fund.json"
    raw = json.loads(path.read_text())
    raw["nav"] = {"total_assets": "100000000", "total_assets_decimal": "100", "total_supply": "100000000000000",
                  "share_price_decimal": "1", "idle_assets": "100000000", "realized_pnl_decimal": "0",
                  "unrealized_pnl_decimal": None, "as_of": float(AS_OF), "as_of_block": "108757783",
                  "read_at": AS_OF + 41.0}
    path.write_text(json.dumps(raw))
    snap = svc.load("alpha-fund").nav
    assert snap is not None and snap.asset_decimals is None and snap.nav == 100 and snap.pnl_bps == 0
    assert "NAV <b>100 USDG</b> · P&amp;L <b>+0.0%</b>" in card(svc, clock)


# -- hardening -------------------------------------------------------------------------------


def test_the_older_read_check_uses_the_block(svc, clock):
    svc.apply_chain_card("alpha-fund", EXECUTED)
    cached = copy.deepcopy(LIVE)  # an older block with a later timestamp: still refused
    cached["data"]["asOf"] = {"block": "108757783", "timestamp": str(EXEC_AS_OF + 999)}
    svc.apply_chain_card("alpha-fund", cached)
    assert svc.load("alpha-fund").nav.active_proposal_id == "1"
    no_block = copy.deepcopy(LIVE)  # no block to compare: it replaces
    no_block["data"]["asOf"] = {"timestamp": str(EXEC_AS_OF + 5)}
    svc.apply_chain_card("alpha-fund", no_block)
    assert svc.load("alpha-fund").nav.active_proposal_id == ""


def test_an_absurd_position_value_is_not_stored(svc, clock):
    svc.apply_chain_card("alpha-fund", EXECUTED)
    svc.apply_chain_card("alpha-fund", executed(positionValue="9" * 400))
    assert svc.load("alpha-fund").nav.position_value == "40600000"  # the earlier snapshot stands


@pytest.mark.parametrize("bad", [{"as_of": float("nan")}, {"as_of": 1.7e12}, {"total_assets_decimal": "1e400"},
                                 {"read_at": float("inf")}])
def test_a_bad_stored_snapshot_is_dropped_on_load(svc, clock, tmp_path, bad):
    svc.apply_chain_card("alpha-fund", LIVE)
    path = tmp_path / "funds" / "alpha-fund.json"
    raw = json.loads(path.read_text())
    raw["nav"].update(bad, total_assets="" if "total_assets_decimal" in bad else raw["nav"]["total_assets"],
                      asset_decimals=None if "total_assets_decimal" in bad else 6)
    path.write_text(json.dumps(raw))  # Python's json writes NaN / Infinity as is
    assert svc.load("alpha-fund").nav is None
    assert "NAV <b>unavailable</b>" in card(svc, clock)


def test_a_bad_snapshot_in_memory_never_breaks_fund_dict():
    st = FundState(fund=fund(), nav=NavSnapshot(total_assets_decimal="1e400", as_of=float("nan")))
    assert fund_dict(st)["nav"] == 0.0  # dropped: not a raise in every handler
    assert fund_dict(st, now=EXEC_AS_OF)["nav_stale"]  # the card says unavailable


def test_card_helpers_never_raise_on_absurd_values():
    assert goal_bar(40, 0.01) == "▮▮▮▮▮▮▮▮▮▮ >999% of NAV"
    assert goal_bar(9.99, 1) == "▮▮▮▮▮▮▮▮▮▮ 999% of NAV"
    assert goal_bar(40, float("inf")) == "" and goal_bar(float("nan"), 100) == ""
    assert fmt_amount(float("inf")) == "?"
    f = FundView(vault="0x" + "11" * 20, governor="", name="x", nav=1, nav_as_of=10**15, nav_stale=True)
    assert nav_notice(f, EXEC_AS_OF) == "Last known NAV."
