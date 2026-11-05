"""Golden text + keyboards for the card renderers."""
from __future__ import annotations

import pytest

pytest.importorskip("telegram")

from sherwood_monitor.telegram.cards import (  # noqa: E402
    cb,
    countdown,
    fmt_amount,
    fmt_pct,
    goal_bar,
    parse_cb,
    receipt_plain,
    render_agent_card,
    render_draft_card,
    render_fund_card,
    render_join_prompt,
    render_leaderboard,
    render_member_card,
    render_member_request,
    render_personal_link,
    render_proposal_card,
    render_settle_receipt,
    veto_meter,
)
from sherwood_monitor.telegram.types import (  # noqa: E402
    FundView,
    LeaderRow,
    Leg,
    MemberView,
    Person,
    Position,
    ProposalView,
    Receipt,
)

NOW = 1_700_000_000
VAULT = "0x1111111111111111111111111111111111111111"
GOV = "0x2222222222222222222222222222222222222222"
FUND_URL = f"https://app.sherwood.sh/tg/fund/{VAULT}"
PEOPLE = {
    1: Person(1, "alice"),
    2: Person(2, "bob"),
    3: Person(3, None, "Carol", "Smith"),
    4: Person(4, "dave"),
}


def proposal(**kw) -> ProposalView:
    base = dict(
        id="42",
        governor=GOV,
        proposer="atlas",
        thesis="Long NVDA into earnings",
        legs=(Leg("NVDA", 6000), Leg("AMD", 4000)),
        max_capital=3000,
        max_drawdown_bps=1500,
        bond_wood=12.4,
        status="voting",
        backers=(2, 3),
        objectors=(4,),
        idea_by=1,
        voting_ends_at=NOW + 24 * 60 - 30,
        against_bps=500,
    )
    base.update(kw)
    return ProposalView(**base)


def fund(**kw) -> FundView:
    base = dict(
        vault=VAULT,
        governor=GOV,
        name="Robin Fund",
        telegram_chat_id=-100123,
        host_tg_id=1,
        members=(MemberView(1, 1, 5000, username="alice"), MemberView(2, 2, 5000, username="bob")),
        agents=("atlas",),
        nav=10000,
        pnl_bps=820,
        positions=(Position("NVDA", 4000, 310), Position("USDC", 6000)),
        open_proposals=(proposal(),),
    )
    base.update(kw)
    return FundView(**base)


def kb(markup):
    return markup.to_dict()["inline_keyboard"]


# -- helpers --------------------------------------------------------------


def test_veto_meter_matches_spec_example():
    assert veto_meter(500, 2000) == "Against 5.0% ▮▮░░░░░░ 20%"
    assert veto_meter(0, 2000) == "Against 0.0% ░░░░░░░░ 20%"
    assert veto_meter(10, 2000) == "Against 0.1% ▮░░░░░░░ 20%"  # any Against shows
    assert veto_meter(2500, 2000) == "Against 25.0% ▮▮▮▮▮▮▮▮ 20%"


@pytest.mark.parametrize(
    "delta,expected",
    [(24 * 60 - 30, "ends in 24m"), (30, "ends in <1m"), (3900, "ends in 1h 05m"),
     (2 * 86400 + 3 * 3600, "ends in 2d 3h"), (0, "voting closed"), (-5, "voting closed")],
)
def test_countdown(delta, expected):
    assert countdown(NOW + delta, NOW) == expected


def test_number_formats():
    assert fmt_amount(3000) == "3,000"
    assert fmt_amount(12.4) == "12.4"
    assert fmt_amount(1234.567) == "1,234.57"
    assert fmt_pct(820, signed=True) == "+8.2%"
    assert fmt_pct(-310, signed=True) == "-3.1%"
    assert goal_bar(3000, 10000) == "▮▮▮░░░░░░░ 30% of NAV"
    assert goal_bar(3000, 0) == ""


def test_callback_data_limit_and_parse():
    assert cb("deposit", VAULT) == f"shfund:deposit:{VAULT}"
    assert len(cb("deposit", VAULT).encode()) == 57
    with pytest.raises(ValueError):
        cb("approve_agent", "a" * 50)
    assert parse_cb("shfund:remove_agent:agent:with:colons") == ("remove_agent", "agent:with:colons")
    assert parse_cb("other:x:y") is None
    assert parse_cb("shfund:") is None


# -- golden cards ---------------------------------------------------------


def test_proposal_card_voting_golden():
    text, markup = render_proposal_card(proposal(), fund=fund(), people=PEOPLE, wood_usd=0.25, now=NOW)
    assert text == (
        "🗳 VOTING · <b>Proposal #42</b>\n"
        "<i>Long NVDA into earnings</i>\n"
        "\n"
        "<b>NVDA</b> 60% · <b>AMD</b> 40%\n"
        "Max capital <b>3,000 USDC</b> · max drawdown <b>15.0%</b>\n"
        "Size ▮▮▮░░░░░░░ 30% of NAV\n"
        "\n"
        "Against 5.0% ▮▮░░░░░░ 20% · ends in 24m\n"
        "👍 @bob CS · ❌ @dave\n"
        "idea by @alice · agent atlas\n"
        "bond 12.4 WOOD (≈$3.10) · agent's skin in the game\n"
        "\n"
        "<i>Only Against counts. Silence = pass unless Against reaches 20% of shares before the window ends.</i>"
    )
    assert kb(markup) == [
        [
            {"text": "👍 Back it · 2", "callback_data": "shfund:back:42"},
            {"text": "❌ Object", "callback_data": "shfund:vote:42"},
        ],
        [{"text": "📊 View fund", "url": FUND_URL}],
    ]


def test_proposal_card_after_vote_has_no_vote_buttons():
    text, markup = render_proposal_card(
        proposal(status="vetoed", against_bps=2150), fund=fund(), people=PEOPLE, now=NOW
    )
    assert text.splitlines()[0] == "⛔ VETOED · <b>Proposal #42</b>"
    assert "Against 21.5% ▮▮▮▮▮▮▮▮ 20%" in text
    assert "ends in" not in text and "(≈$" not in text
    assert kb(markup) == [[{"text": "📊 View fund", "url": FUND_URL}]]


def test_proposal_card_escapes_html_and_truncates():
    evil = proposal(thesis="<b>pump</b> & dump " + "x" * 700, proposer="<script>", legs=(Leg("<i>", 10000),))
    text, _ = render_proposal_card(evil, people={}, now=NOW)
    assert "<b>pump</b>" not in text and "&lt;b&gt;pump&lt;/b&gt; &amp; dump" in text
    assert "&lt;script&gt;" in text and "<b>&lt;i&gt;</b> 100%" in text
    assert "…</i>" in text
    assert "idea by user1" in text  # unknown person falls back to user<id>


def test_proposal_card_many_backers_collapse():
    text, _ = render_proposal_card(proposal(backers=tuple(range(10, 18)), objectors=()), people={}, now=NOW)
    assert "👍 user10 user11 user12 user13 user14 +3" in text


def test_fund_card_golden():
    text, markup = render_fund_card(fund(), people=PEOPLE, now=NOW)
    assert text == (
        "🏦 <b>Robin Fund</b>\n"
        "NAV <b>10,000 USDC</b> · P&amp;L <b>+8.2%</b>\n"
        "\n"
        "<b>Positions</b>\n"
        "• NVDA 4,000 USDC (+3.1%)\n"
        "• USDC 6,000 USDC\n"
        "\n"
        "<b>Open proposals</b>\n"
        "• #42 Long NVDA into earnings · 🗳 VOTING · ends in 24m\n"
        "\n"
        "Members 2 · Agents atlas"
    )
    assert kb(markup) == [
        [{"text": "📊 View", "url": FUND_URL}],
        [
            {"text": "💰 Deposit", "callback_data": f"shfund:deposit:{VAULT}"},
            {"text": "↩️ Withdraw", "callback_data": f"shfund:withdraw:{VAULT}"},
        ],
        [{"text": "🎟 Join", "callback_data": f"shfund:join:{VAULT}"}],
    ]


def test_fund_card_empty_with_notice():
    text, _ = render_fund_card(
        fund(positions=(), open_proposals=(), agents=(), members=()), now=NOW, notice="Make me admin"
    )
    assert "• none yet" in text and "Open proposals" not in text
    assert text.endswith("Members 0 · Agents none\n\n<i>Make me admin</i>")


def test_member_card_golden():
    m = MemberView(tg_id=1, number=3, share_bps=320, joined_at=1762300800, username="alice")
    text, markup = render_member_card(m, fund=fund())
    assert text == "🎟 <b>Member #3</b> · @alice\n3.2% of shares · joined Nov 5, 2025\n<b>Robin Fund</b>"
    assert kb(markup) == [[{"text": "📊 View fund", "url": FUND_URL}]]


def test_settle_receipt_golden():
    r = Receipt(proposal_id="42", pnl_bps=820, symbol="NVDA", idea_by=1, backers=5)
    text, markup = render_settle_receipt(r, people=PEOPLE, fund=fund())
    assert text == (
        "🏁 <b>Proposal #42 settled</b>\n<b>+8.2%</b> on NVDA · idea by @alice · backed by 5\n"
        "<i>The agent's bond stays in escrow for the 14-day challenge window.</i>"
    )
    assert receipt_plain(r, PEOPLE) == "+8.2% on NVDA · idea by @alice · backed by 5"
    assert kb(markup) == [[{"text": "📊 View fund", "url": FUND_URL}]]


def test_draft_card_with_bond_buffer_golden():
    d = proposal(id="d7", status="draft", backers=(), objectors=(), voting_ends_at=None, against_bps=0)
    text, markup = render_draft_card(
        d, fund=fund(), people=PEOPLE, wood_usd=0.25, bond_shortfall_wood=4.04, requires_host_cosign=True
    )
    assert text == (
        "📝 DRAFT · <b>Draft #d7</b> · from @alice's idea\n"
        "<i>Long NVDA into earnings</i>\n"
        "\n"
        "<b>NVDA</b> 60% · <b>AMD</b> 40%\n"
        "Max capital <b>3,000 USDC</b> · max drawdown <b>15.0%</b>\n"
        "Size ▮▮▮░░░░░░░ 30% of NAV\n"
        "agent atlas\n"
        "bond 12.4 WOOD (≈$3.10) · agent's skin in the game\n"
        "needs ~4 WOOD (≈$1.01), swapping…\n"
        "\n"
        "<i>Host: 🚀 Ship it sends this to a vote.</i>"
    )
    assert kb(markup) == [[
        {"text": "🚀 Ship it", "callback_data": "shfund:shipit:d7"},
        {"text": "✍️ Co-sign (host)", "callback_data": "shfund:propose:d7"},
    ]]


def test_draft_card_without_buffer_or_cosign():
    d = proposal(id="d8", status="draft")
    text, markup = render_draft_card(d, people=PEOPLE)
    assert "swapping" not in text and "(≈$" not in text
    assert kb(markup) == [[{"text": "🚀 Ship it", "callback_data": "shfund:shipit:d8"}]]


def test_join_prompt_golden():
    text, markup = render_join_prompt(fund())
    assert text.splitlines()[0] == "🎟 <b>Join Robin Fund</b>"
    assert kb(markup) == [[
        {"text": "🎟 Join", "callback_data": f"shfund:join:{VAULT}"},
        {"text": "📊 View fund", "url": FUND_URL},
    ]]


def test_host_cards():
    text, markup = render_member_request(fund(), Person(77, "eve"))
    assert text.startswith("🙋 @eve wants to join <b>Robin Fund</b>.")
    assert kb(markup) == [[{"text": "✅ Approve member", "callback_data": "shfund:approve_member:77"}]]
    _, markup = render_agent_card(fund(), "nova")
    assert [b["callback_data"] for b in kb(markup)[0]] == ["shfund:approve_agent:nova", "shfund:remove_agent:nova"]
    _, markup = render_agent_card(fund(), "nova", pending=False)
    assert [b["callback_data"] for b in kb(markup)[0]] == ["shfund:remove_agent:nova"]


def test_leaderboard_golden():
    rows = [LeaderRow(2, 300, 1), LeaderRow(1, 1240, 3)]
    text, _ = render_leaderboard(rows, fund=fund(), people=PEOPLE)
    assert text == (
        "🏆 <b>Leaderboard</b> · Robin Fund\n"
        "1. @alice <b>+12.4%</b> · 3 ideas\n"
        "2. @bob <b>+3.0%</b> · 1 idea"
    )
    empty, _ = render_leaderboard([], fund=fund())
    assert "No settled ideas yet" in empty


def test_personal_link_is_a_url_button():
    text, markup = render_personal_link("vote", "https://app.sherwood.sh/tg/vote?x=1", "Robin <Fund>")
    assert "personal vote link for <b>Robin &lt;Fund&gt;</b>" in text and "15 minutes" in text
    assert kb(markup) == [[{"text": "❌ Cast Against in browser", "url": "https://app.sherwood.sh/tg/vote?x=1"}]]


def test_review_vetoed_cooldown_and_onchain_id_rendering():
    from datetime import datetime, timezone

    review = proposal(id="p-3", onchain_id=9, status="review", review_ends_at=NOW + 600, guardian_coverage="full")
    text, markup = render_proposal_card(review, fund=fund(), people=PEOPLE, now=NOW)
    assert text.startswith("🛡 GUARDIAN REVIEW · <b>Proposal #9</b>")
    assert "🛡 Guardian review · ends in 10m · coverage full" in text
    assert not [b for row in kb(markup) for b in row if b.get("callback_data", "").startswith("shfund:vote")]
    vetoed, _ = render_proposal_card(proposal(status="vetoed"), fund=fund(), now=NOW)
    assert "bond comes back at once" in vetoed
    summary = proposal(legs=(Leg("swap 400 USDG -> TSLA", 0),))
    assert "swap 400 USDG -&gt; TSLA" in render_proposal_card(summary, now=NOW)[0]
    text, _ = render_fund_card(fund(cooldown_until=NOW + 3600), now=NOW)
    hhmm = datetime.fromtimestamp(NOW + 3600, tz=timezone.utc).strftime("%H:%M")
    assert f"⏳ Next proposal possible at {hhmm} UTC" in text
