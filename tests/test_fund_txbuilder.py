"""Prepare-API client: request shapes, envelope parsing, EIP-5792 batches. No network."""
from __future__ import annotations

from pathlib import Path

import pytest

from sherwood_monitor.fund import txbuilder
from sherwood_monitor.fund.txbuilder import (
    ROUTES,
    TxBuilderClient,
    TxBuilderError,
    build_propose_body,
    parse_envelope,
)
from tests.fund_helpers import GUEST_ADDR, HOST_ADDR, MEMBER_ADDR, TARGET, VAULT


def ok_envelope(command="prepare.x", txs=None, chain=4663):
    return {
        "success": True,
        "data": {
            "txs": txs if txs is not None else [{"to": VAULT, "data": "0xabcd", "value": "0x0", "chainId": chain}],
            "preconditions": [{"type": "vault-not-locked", "vault": VAULT}],
            "description": "desc",
            "note": "n",
        },
        "meta": {"command": command, "chainId": chain, "timestamp": "2026-11-05T00:00:00Z"},
    }


class Recorder:
    def __init__(self, response=None, status=200):
        self.calls = []
        self.response = response or ok_envelope()
        self.status = status

    def __call__(self, url, body, timeout):
        self.calls.append((url, body, timeout))
        return self.status, self.response


def client(rec):
    return TxBuilderClient("https://api.example/api/v1/", post_json=rec)


def test_routes_cover_the_fund_flow():
    assert set(ROUTES) == {
        "create-fund", "approve-depositor", "approve-depositors", "register-agent", "deposit", "propose",
        "propose-portfolio", "vote", "execute", "settle", "redeem", "identity-mint", "governor-set",
        "set-agent-fee", "owner-stake",
    }


def test_route_bodies_and_urls():
    rec = Recorder()
    c = client(rec)
    c.approve_depositor(vault=VAULT, depositor=MEMBER_ADDR)
    c.register_agent(vault=VAULT, agent_address=GUEST_ADDR, agent_id=202)
    c.deposit(vault=VAULT, receiver=MEMBER_ADDR, amount_decimal="100")
    c.vote(vault=VAULT, proposal_id=3, vote="Against")
    c.execute(vault=VAULT, proposal_id="3")
    c.settle(vault=VAULT, proposal_id=3)
    c.redeem(vault=VAULT, receiver=MEMBER_ADDR, shares="1000")
    c.identity_mint(name="beta")
    c.governor_set(vault=VAULT, param="votingPeriod", value=1800)
    c.create_fund(creator=HOST_ADDR, asset=TARGET, creator_agent_id=101, metadata_uri="ipfs://bafy",
                  name="Alpha", symbol="ALPHA", subdomain="alpha-fund")
    urls = [u for u, _, _ in rec.calls]
    assert urls[0] == "https://api.example/api/v1/prepare/approve-depositor"
    bodies = [b for _, b, _ in rec.calls]
    assert all(b["chainId"] == 4663 for b in bodies)
    assert bodies[1] == {"chainId": 4663, "vault": VAULT, "agentAddress": GUEST_ADDR, "agentId": "202"}
    assert bodies[2]["amountDecimal"] == "100" and "amount" not in bodies[2]
    assert bodies[3]["vote"] == "Against" and bodies[3]["proposalId"] == "3"
    assert bodies[-1]["openDeposits"] is False and bodies[-1]["creatorAgentId"] == "101"


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.approve_depositor(vault="0x12", depositor=MEMBER_ADDR),
        lambda c: c.vote(vault=VAULT, proposal_id=1, vote="Yes"),
        lambda c: c.deposit(vault=VAULT, receiver=MEMBER_ADDR),
        lambda c: c.deposit(vault=VAULT, receiver=MEMBER_ADDR, amount="1", amount_decimal="1"),
        lambda c: c.redeem(vault=VAULT, receiver=MEMBER_ADDR, shares="0"),
        lambda c: c.governor_set(vault=VAULT, param="protocolFeeBps", value=1),
        lambda c: c.identity_mint(name=" "),
        lambda c: c.prepare("drain-vault", {}),
    ],
)
def test_local_validation_rejects_before_any_request(call):
    rec = Recorder()
    with pytest.raises(TxBuilderError):
        call(client(rec))
    assert rec.calls == []


def test_error_envelope_raises_with_status():
    rec = Recorder({"success": False, "error": "Rate limit exceeded", "meta": {}}, status=429)
    with pytest.raises(TxBuilderError) as exc:
        client(rec).settle(vault=VAULT, proposal_id=1)
    assert exc.value.status == 429 and "Rate limit" in str(exc.value)


def test_transport_failure_is_wrapped():
    def boom(url, body, timeout):
        raise OSError("connection refused")

    with pytest.raises(TxBuilderError, match="request failed"):
        TxBuilderClient(post_json=boom).settle(vault=VAULT, proposal_id=1)


def test_parse_envelope_and_wallet_send_calls_per_chain():
    txs = [
        {"to": VAULT, "data": "0x01", "value": "0x0", "chainId": 4663},
        {"to": TARGET, "data": "0x02", "value": "0x0", "chainId": 8453},
        {"to": VAULT, "data": "0x03", "value": "0x0", "chainId": 4663},
    ]
    action = parse_envelope("approve-agent", 200, ok_envelope("prepare.approveAgent", txs))
    assert action.command == "prepare.approveAgent" and len(action.txs) == 3
    batches = action.wallet_send_calls(HOST_ADDR)
    assert [b["chainId"] for b in batches] == [hex(4663), hex(8453)]
    assert batches[0]["calls"] == [{"to": VAULT, "data": "0x01", "value": "0x0"}, {"to": VAULT, "data": "0x03", "value": "0x0"}]
    assert batches[0]["from"] == HOST_ADDR and batches[0]["version"] == "2.0.0"
    assert action.to_dict()["txs"][1]["chainId"] == 8453


def test_malformed_tx_in_response():
    with pytest.raises(TxBuilderError, match="malformed"):
        parse_envelope("x", 200, ok_envelope(txs=[{"to": VAULT}]))


def test_build_propose_body_shape():
    body = build_propose_body(
        vault=VAULT, strategy=TARGET, metadata_uri="ipfs://bafyproposal", strategy_duration=3600,
        max_capital=500_000_000, max_drawdown_bps=1000,
        execute_calls=[{"target": TARGET, "data": "0xabcd", "value": 0}], execute_call_caps=[400_000_000],
        settlement_calls=[], settlement_call_caps=[],
    )
    assert body["envelope"] == {"maxCapital": "500000000", "maxDrawdownBps": 1000}
    assert body["executeCalls"] == [{"target": TARGET, "data": "0xabcd", "value": "0"}]
    assert body["executeCallCaps"] == ["400000000"] and body["strategyDuration"] == "3600"


@pytest.mark.parametrize(
    "override",
    [
        {"metadata_uri": "https://x/meta.json"},
        {"max_capital": 0},
        {"max_drawdown_bps": 10_001},
        {"execute_call_caps": []},
        {"strategy_duration": 0},
    ],
)
def test_build_propose_body_rejects(override):
    args = dict(
        vault=VAULT, strategy=TARGET, metadata_uri="ipfs://bafy", strategy_duration=60,
        max_capital=1, max_drawdown_bps=0,
        execute_calls=[{"target": TARGET, "data": "0x"}], execute_call_caps=[1],
        settlement_calls=[], settlement_call_caps=[],
    )
    args.update(override)
    with pytest.raises(TxBuilderError):
        build_propose_body(**args)


def test_module_never_signs():
    src = Path(txbuilder.__file__).read_text()
    for forbidden in ("eth_keys", "eth_account", "sign_transaction", "private_key", "privateKey"):
        assert forbidden not in src


# -- routes from sherwood PR #739 (docs/tx-builder.md) ---------------------------------------
def test_new_routes_bodies():
    rec = Recorder()
    c = client(rec)
    c.approve_depositors(vault=VAULT, depositors=[MEMBER_ADDR, GUEST_ADDR])
    c.set_agent_fee(vault=VAULT, bps=500)
    c.owner_stake(owner=HOST_ADDR)
    urls = [u.rsplit("/", 1)[1] for u, _, _ in rec.calls]
    assert urls == ["approve-depositors", "set-agent-fee", "owner-stake"]
    assert rec.calls[0][1] == {"chainId": 4663, "vault": VAULT, "depositors": [MEMBER_ADDR, GUEST_ADDR]}
    assert rec.calls[1][1] == {"chainId": 4663, "vault": VAULT, "bps": 500}
    with pytest.raises(TxBuilderError):
        c.set_agent_fee(vault=VAULT, bps=2600)
    with pytest.raises(TxBuilderError):
        c.approve_depositors(vault=VAULT, depositors=[])


def test_propose_portfolio_body_and_response_extras():
    body = txbuilder.build_propose_portfolio_body(
        vault=VAULT, proposer=HOST_ADDR, legs=[{"symbol": "nvda", "weight_bps": 6000}, {"symbol": "AAPL", "weightBps": 4000}],
        max_capital_decimal="10", strategy_duration="6h", metadata_uri="ipfs://bafkqaaa", bond_buffer_bps=500,
    )
    assert body == {
        "chainId": 4663, "vault": VAULT, "proposer": HOST_ADDR,
        "legs": [{"weightBps": 6000, "symbol": "NVDA"}, {"weightBps": 4000, "symbol": "AAPL"}],
        "strategyDuration": "6h", "maxCapitalDecimal": "10", "metadataURI": "ipfs://bafkqaaa", "bondBufferBps": 500,
    }
    for bad in ({"legs": [{"symbol": "NVDA", "weightBps": 5000}]}, {"legs": [{"weightBps": 10000}]}, {"metadata_uri": None}):
        args = dict(vault=VAULT, proposer=HOST_ADDR, legs=[{"symbol": "NVDA", "weightBps": 10000}],
                    max_capital_decimal="10", strategy_duration=3600, metadata_uri="ipfs://x")
        args.update(bad)
        with pytest.raises(TxBuilderError):
            txbuilder.build_propose_portfolio_body(**args)
    env = ok_envelope("prepare.proposePortfolio", txs=[
        {"to": "0xF8BC08092C06dB6148114DCf82AF881F1085f92b", "data": "0x095ea7b3", "value": "0x0", "chainId": 4663},
        {"to": TARGET, "data": "0x26ab0cd4", "value": "0x0", "chainId": 4663},
        {"to": TARGET, "data": "0x784ea274", "value": "0x0", "chainId": 4663},
    ])
    env["data"].update({"prelude": [], "clone": TARGET, "bondQuoteWood": "40812151661088890702",
                        "bondApproveWood": "42852759244143335237", "bondShortfallWood": "0", "warnings": ["cooldown"]})
    action = client(Recorder(env)).propose_portfolio(body)
    assert [t.data[:10] for t in action.txs] == ["0x095ea7b3", "0x26ab0cd4", "0x784ea274"]  # approve -> clone -> propose
    assert action.extra["bondShortfallWood"] == "0" and action.prelude == [] and action.warnings == ["cooldown"]


def test_new_route_404_names_the_route_and_the_pr():
    c = client(Recorder({"success": False, "error": "Not Found"}, status=404))
    with pytest.raises(TxBuilderError) as exc:
        c.approve_depositors(vault=VAULT, depositors=[MEMBER_ADDR])
    assert exc.value.status == 404 and "prepare/approve-depositors" in str(exc.value) and "#739" in str(exc.value)


def test_fund_card_read():
    calls = []

    def get_json(url, timeout):
        calls.append(url)
        return 200, {"success": True, "data": {"vault": VAULT, "proposeGate": {"ready": True}}}

    c = TxBuilderClient("https://api.example/api/v1", get_json=get_json)
    assert c.fund_card(VAULT, check=[MEMBER_ADDR])["proposeGate"] == {"ready": True}
    assert calls == [f"https://api.example/api/v1/funds/{VAULT}/tg-card?chain=4663&check={MEMBER_ADDR}"]
    missing = TxBuilderClient("https://api.example/api/v1", get_json=lambda u, t: (404, {}))
    with pytest.raises(TxBuilderError) as exc:
        missing.fund_card(VAULT)
    assert "tg-card" in str(exc.value) and "not deployed" in str(exc.value)


# -- sherwood #740: bond-buffer swap, ledger price vs feed, 409 premium / requote -------------------------
def _pp(**kw):
    base = dict(vault=VAULT, proposer=HOST_ADDR, legs=[{"symbol": "NVDA", "weightBps": 10000}],
                max_capital_decimal="10", strategy_duration="6h", metadata_uri="ipfs://bafkqaaa")
    base.update(kw)
    return txbuilder.build_propose_portfolio_body(**base)


def test_propose_portfolio_bond_buffer_params():
    body = _pp(bond_swap_max_premium_bps=300, bond_swap_deadline_seconds=600, quote_usd_per_wood="0.00085438",
               max_drift_bps=500, fill_bond_buffer=True, allow_bond_swap_premium=False)
    assert body["bondSwapMaxPremiumBps"] == 300 and body["bondSwapDeadlineSeconds"] == 600
    assert body["quoteUsdPerWood"] == "0.00085438" and body["maxDriftBps"] == 500  # echoed verbatim
    assert body["fillBondBuffer"] is True and body["allowBondSwapPremium"] is False
    for bad in ({"bond_swap_max_premium_bps": 5001}, {"bond_swap_deadline_seconds": 59}, {"max_drift_bps": 0},
                {"quote_usd_per_wood": "8.5e-4"}, {"quote_usd_per_wood": "$0.0008"}):
        with pytest.raises(TxBuilderError):
            _pp(**bad)


def test_response_exposes_the_ledger_price_not_the_feed():
    env = ok_envelope("prepare.proposePortfolio")
    env["data"].update({"quoteUsdPerWood": "0.00085438", "feedUsdPerWood": "0.00170876",
                        "bondBuffer": {"status": "swap", "swapRequired": True, "ledgerPrice": {"haircutBps": 5000}},
                        "bufferNote": "+0.59% over the WOOD/USD feed"})
    action = client(Recorder(env)).propose_portfolio(_pp())
    assert action.quote_usd_per_wood == "0.00085438"  # what to echo back for the drift check
    assert action.bond_buffer["status"] == "swap" and "first leg" in action.bond_note


def test_409_premium_and_requote_carry_their_data():
    premium = {"success": False, "error": "Bond-buffer swap priced +0.59% over the WOOD/USD feed, past bondSwapMaxPremiumBps 0",
               "data": {"premiumOverCap": True, "premiumBps": 59, "maxPremiumBps": 0, "optIn": "allowBondSwapPremium: true",
                        "bondBuffer": {"status": "premium-over-cap", "swapRequired": True}}}
    with pytest.raises(TxBuilderError) as exc:
        client(Recorder(premium, status=409)).propose_portfolio(_pp())
    assert exc.value.status == 409 and exc.value.data["premiumBps"] == 59
    assert "+0.59% over the WOOD/USD feed" in exc.value.bond_note and "allowBondSwapPremium" in exc.value.bond_note
    requote = {"success": False, "error": "moved 1001 bps", "data": {"requote": True, "driftBps": 1001, "maxDriftBps": 500}}
    with pytest.raises(TxBuilderError) as exc:
        client(Recorder(requote, status=409)).propose_portfolio(_pp(quote_usd_per_wood="0.00093982"))
    assert "re-quote" in exc.value.bond_note and "1001" in exc.value.bond_note
