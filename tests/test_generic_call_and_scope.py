"""Generic Call preparation, and answers scoped to the configured fund.

Local fixtures only. Nothing here signs, broadcasts, funds a vault, or messages Telegram.
The register() check in an empty home is an in-process import/load, not a cold install:
the release step `hermes plugins validate` (docs/catalog-submission.md) is that check.
"""
from __future__ import annotations

import asyncio
import copy
import json
import shlex
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_utils import function_signature_to_4byte_selector

from sherwood_monitor.fund.generic_call import (
    MIN_CLI_FOR_GENERIC_CALL,
    TEMPLATE_ADDRESS_BOOK,
    preview_generic_call,
    validate_generic_call_plan,
    write_generic_call_plan,
)
from decimal import Decimal

from sherwood_monitor.fund.models import CHAIN_ID_ROBINHOOD, TOKEN_ADDRESSES_4663, Agent, FundState, ProposalRecord
from sherwood_monitor.fund.rules import RuleViolation
from sherwood_monitor.fund.scope import (
    discover_strategy_templates,
    fund_scope,
    parse_strategy_list,
    reference_skills,
    scope_text,
)
from sherwood_monitor.fund.service import FundService
from sherwood_monitor.fund.store import LocalJsonBackend
from sherwood_monitor.fund.txbuilder import TxBuilderClient, TxBuilderError
from sherwood_monitor import session
from tests.conftest import load_fixture
from tests.fund_helpers import GOVERNOR, HOST_ADDR, VAULT, Clock, fund, host_agent
from tests.test_fund_open_protocol import fake_version
from tests.test_fund_plugin import HOST_CFG, boot

USDG = TOKEN_ADDRESSES_4663["USDG"]
TARGET = "0x" + "77" * 20
STATE = Path(".hermes/plugins/sherwood-monitor/funds")
SETUP = {
    "fund": "alpha-fund", "vault": VAULT, "governor": GOVERNOR, "telegram_chat_id": "-100500",
    "host_telegram_id": "900", "max_capital": "1000", "max_drawdown_bps": 2000, "voting_period": 1800,
    "agent_fee_bps": 500,
}
FIXTURES = Path(__file__).parent.parent / "fixtures"
# `sherwood strategy list`, CLI 0.103.0, captured read-only (scratch HOME) on 2026-11-08.
REAL_LISTING_0_103 = (FIXTURES / "strategy_list_0.103.0.txt").read_text()


def listing_with_generic_call_available(text: str = REAL_LISTING_0_103) -> str:
    """The 0.103.0 output with Generic Call moved to the available rows, as CLI 0.104.0's printer
    (index.js:4990-5028) prints a template whose address-book entry is set."""
    text = text.replace("    Generic Call (generic-call)\n", "")
    row = (
        "\n  Generic Call (generic-call)\n"
        "    Run a fixed list of calls against any protocol, no Solidity (--calls <plan.json>, --amount).\n"
        "    Template: 0xee67db9630fCB4F69C013928b2113C5489B39833\n"
    )
    return text.replace("\n  Not available on", row + "\n  Not available on", 1)


LISTING = """\x1b[1mStrategy Templates (Robinhood Chain)\x1b[0m
  Portfolio (portfolio)
    A basket of stock tokens.
    Template: 0xabc
Not available on this network
  Moonwell Supply (moonwell-supply)
    Supply USDC.
"""


ROUTER = "0x" + "88" * 20
MAX_UINT = str(2**256 - 1)


def _sel(signature: str) -> str:
    return function_signature_to_4byte_selector(signature).hex()


def _addr_word(address: str) -> str:
    return "00" * 12 + address[2:].lower()


def _call(target: str = TARGET, selector: str = "c0ffee00", words: int = 1) -> dict:
    return {"target": target, "data": "0x" + selector + ("ab" * 32) * words}


def plan(**kw) -> dict:
    base = {
        "executeCalls": [_call()],
        "settleCalls": [_call(selector="d00d1234")],
        "settlePatches": [],
        "sweepTokens": [],
    }
    base.update(kw)
    return base


def _service(tmp_path: Path, *, signer: str = "agent") -> FundService:
    return FundService(LocalJsonBackend(tmp_path / STATE), proposal_signer=signer)


@pytest.fixture
def agent_ctx(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, {**HOST_CFG, "proposal_signer": "agent"})
    assert ctx.call_tool("fund_setup", SETUP)["created"]
    return ctx


def _draft(ctx, calls, **kw):
    ctx.call_tool("fund_claim_topic", {"topic": kw.get("topic", "custom route")})
    args = {
        "topic": "custom route", "thesis": "a custom call on this vault's chain",
        "template": "generic-call", "calls": calls,
        "max_capital": "100", "max_drawdown_bps": 1000, "strategy_duration_seconds": 3600,
    }
    args.update(kw)
    return ctx.call_tool("fund_draft_proposal", args)


# -- plan shape --------------------------------------------------------------------------------


def test_a_plan_on_the_configured_chain_previews_unsigned_and_unaccepted():
    f = fund()
    stored = validate_generic_call_plan(plan(), f)
    preview = preview_generic_call(f, stored, max_capital=100_000_000, max_drawdown_bps=1000, strategy_duration=3600)
    assert preview["ready"] and preview["signed"] is False and preview["execution_acceptance"] is False
    body = preview["body"]
    assert body["chain_id"] == 4663 and body["robinhood_brokerage"] is False
    assert body["template_address"] == TEMPLATE_ADDRESS_BOOK[4663]
    assert body["pull"]["human"] == "100" and body["execute_calls"][0]["target"] == TARGET
    assert "UNAUDITED_TEMPLATE" in body["guardian"] and "Abstain" in body["guardian"]
    assert "simulate_batch" in body["simulation"] and "NotActiveProposalStrategy" in body["simulation"]
    assert "quote-time" in body["exit"]


def test_a_raw_balance_patch_is_allowed_and_labeled_as_no_price_floor():
    raw = plan(settlePatches=[{
        "callIndex": 0, "wordIndex": 0, "token": TARGET, "mulNum": 0, "mulDen": 0,
    }])
    stored = validate_generic_call_plan(raw, fund())
    preview = preview_generic_call(fund(), stored, max_capital=1, max_drawdown_bps=0, strategy_duration=60)
    patch = preview["body"]["settle_patches"][0]
    assert patch["quote_time_floor"] is False and "no price floor" in patch["floor"]
    assert any("no price floor" in w for w in preview["body"]["warnings"])


@pytest.mark.parametrize("calls,code", [
    ({"executeCalls": [_call()], "settleCalls": [], "settlePatches": [], "sweepTokens": []}, "no_exit"),
    (plan(executeCalls=[_call()] * 17), "bad_plan"),
    (plan(yield_note=1), "bad_plan"),
    ({**plan(), "executeCalls": [{"target": "Moonwell", "data": "0xdeadbeef"}]}, "bad_target"),
    ({**plan(), "executeCalls": [{"target": VAULT, "data": "0xdeadbeef"}]}, "bad_target"),
    ({**plan(), "executeCalls": [{"target": TARGET, "data": "0xabc", "value": "1"}]}, "bad_call"),
    ({**plan(), "executeCalls": [_call(target=USDG, selector="deadbeef")]}, "bad_call"),
    ({**plan(), "executeCalls": [{"target": USDG, "data": "0x095ea7b3" + "11" * 32 + "ff" * 32}]}, "unlimited_approve"),
    (plan(settlePatches=[{"callIndex": 0, "wordIndex": 0, "token": TARGET, "mulNum": 1}]), "bad_plan"),
    (plan(executeCalls=[_call()], settleCalls=[], sweepTokens=[USDG]), None),
])
def test_malformed_plans_are_rejected_and_an_asset_sweep_is_an_exit(calls, code):
    # The parametrize name "yield_note" is not a plan key: build it as an unknown field.
    if "yield_note" in calls:
        calls = {k: v for k, v in calls.items() if k != "yield_note"}
        calls["yield"] = 1
    if code is None:
        assert validate_generic_call_plan(calls, fund())["sweepTokens"] == [USDG]
        return
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(calls, fund())
    assert exc.value.code == code


# -- unlimited approvals: the same rule for raw data and signature + args (H1) ---------------------


@pytest.mark.parametrize("call", [
    {"target": USDG, "signature": "approve(address,uint256)", "args": [ROUTER, MAX_UINT]},
    {"target": TARGET, "signature": "approve(address spender, uint256 amount)", "args": [ROUTER, str(2**255)]},
    {"target": TARGET, "signature": "function approve(address,uint256)", "args": ["$clone", MAX_UINT]},
    {"target": TARGET, "signature": "increaseAllowance(address,uint256)", "args": [ROUTER, MAX_UINT]},
    {"target": TARGET, "signature": "setApprovalForAll(address,bool)", "args": [ROUTER, True]},
    {"target": TARGET, "signature": "setApprovalForAll(address,bool)", "args": [ROUTER, "true"]},
    {"target": TARGET, "signature": "approve(address,address,uint160,uint48)",
     "args": [TARGET, ROUTER, str(2**160 - 1), 0]},
    {"target": TARGET, "data": "0x" + _sel("increaseAllowance(address,uint256)") + _addr_word(ROUTER) + "ff" * 32},
    {"target": TARGET, "data": "0x" + _sel("setApprovalForAll(address,bool)") + _addr_word(ROUTER) + "00" * 31 + "01"},
    {"target": USDG, "data": "0x" + _sel("approve(address,uint256)") + _addr_word(ROUTER) + "ff" * 32},
])
def test_an_unlimited_approval_is_refused_in_every_form(call):
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(plan(executeCalls=[call]), fund(), pull_amount=100_000_000)
    assert exc.value.code == "unlimited_approve"
    assert "never allowed" in exc.value.reason


def test_an_unlimited_approval_in_the_settle_leg_is_refused_too():
    settle = [{"target": USDG, "signature": "approve(address,uint256)", "args": [ROUTER, MAX_UINT]}]
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(plan(settleCalls=settle), fund(), pull_amount=1)
    assert exc.value.code == "unlimited_approve"


@pytest.mark.parametrize("args", [[ROUTER, "100000000"], [ROUTER, "$pullAmount"], [ROUTER, 5], ["$clone", "$pull_amount"]])
def test_an_exact_or_pull_amount_approval_on_the_asset_is_allowed(args):
    call = {"target": USDG, "signature": "approve(address,uint256)", "args": args}
    stored = validate_generic_call_plan(plan(executeCalls=[call]), fund(), pull_amount=100_000_000)
    assert stored["executeCalls"][0]["args"] == args


def test_an_operator_approval_switched_off_is_allowed():
    call = {"target": TARGET, "signature": "setApprovalForAll(address,bool)", "args": [ROUTER, False]}
    assert validate_generic_call_plan(plan(executeCalls=[call]), fund())["executeCalls"]


def test_the_asset_call_is_judged_by_its_selector_not_its_signature_label():
    # data says 0xdeadbeef; the label says approve. The selector is what runs.
    labelled = {"target": USDG, "signature": "approve(address,uint256)", "data": "0xdeadbeef" + "ab" * 64}
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(plan(executeCalls=[labelled]), fund())
    assert exc.value.code in ("bad_call", "bad_calldata")


def test_the_guard_blocks_an_unlimited_signature_approve_before_the_handler(agent_ctx):
    agent_ctx.call_tool("fund_claim_topic", {"topic": "custom route"})
    hook = agent_ctx.hooks["pre_tool_call"][0]
    calls = plan(executeCalls=[{"target": USDG, "signature": "approve(address,uint256)", "args": [ROUTER, MAX_UINT]}])
    blocked = asyncio.run(hook(tool_name="fund_draft_proposal", args={
        "topic": "custom route", "thesis": "x", "template": "generic-call", "calls": calls,
        "max_capital": "100", "max_drawdown_bps": 1000,
    }))
    assert blocked["action"] == "block" and "unlimited_approve" in blocked["message"]
    drafted = _draft(agent_ctx, calls)
    assert drafted["ok"] is False and drafted["code"] == "unlimited_approve"


# -- a plan the plugin accepts is one CLI 0.104.0 compiles (M4) ------------------------------------
# Mirrors @sherwoodagent/cli@0.104.0 dist/chunk-ADWPGCQW.js: compileCall (6676-6709), coerce,
# scaleOf (6711-6720), compileGenericCallSpec (6721-6793), parseGenericCallSpec (6795-6810) and
# validateGenericCallInit (6446-6550).

SWAP = "swapExactIn(address,uint256,uint256)"


def _settle_swap(**kw) -> dict:
    call = {"target": TARGET, "signature": SWAP, "args": [USDG, "1", "0"]}
    call.update(kw)
    return call


def _patch(**kw) -> dict:
    patch = {"callIndex": 0, "wordIndex": 1, "token": TARGET}
    patch.update(kw)
    return patch


@pytest.mark.parametrize("patch,why", [
    ({"callIndex": "0", "wordIndex": 1, "token": TARGET}, "a string callIndex"),  # review 3a
    ({"callIndex": 0, "wordIndex": "1", "token": TARGET}, "a string wordIndex"),  # review 3a
    ({"callIndex": 0, "argIndex": None, "wordIndex": 1, "token": TARGET}, "argIndex null beside wordIndex"),  # 3b
    ({"callIndex": 0, "wordIndex": None, "token": TARGET}, "a null wordIndex"),
    ({"callIndex": 0.0, "wordIndex": 1, "token": TARGET}, "a float callIndex"),
    ({"callIndex": True, "wordIndex": 1, "token": TARGET}, "a bool callIndex"),
    (_patch(mulNum=2**53, mulDen=1), "a JSON number above 2^53-1"),  # review 3c
    (_patch(mulNum=None, mulDen=None), "null scales"),
    (_patch(mulNum="1.5", mulDen="1"), "a decimal-point scale"),
    (_patch(mulNum=str(2**256), mulDen="1"), "a scale above uint256"),
    ({"callIndex": 0, "argIndex": 1, "token": TARGET}, "argIndex on a raw-data call"),
    ({"callIndex": 0, "wordIndex": 0, "token": TARGET}, None),  # control: allowed
])
def test_patch_fields_are_typed_as_the_cli_reads_them(patch, why):
    raw = plan(settlePatches=[patch])
    if why is None:
        assert validate_generic_call_plan(raw, fund())["settlePatches"] == [patch]
        return
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(raw, fund())
    assert exc.value.code == "bad_plan", why


def test_large_scales_go_as_decimal_strings_and_the_stored_patch_is_normalized():
    raw = plan(
        settleCalls=[_settle_swap()],
        settlePatches=[{"callIndex": 0, "argIndex": 2, "token": TARGET, "mulNum": str(2**200), "mulDen": 2**53 - 1}],
    )
    stored = validate_generic_call_plan(raw, fund())
    assert stored["settlePatches"] == [
        {"callIndex": 0, "argIndex": 2, "token": TARGET, "mulNum": str(2**200), "mulDen": 2**53 - 1},
    ]


@pytest.mark.parametrize("patch,code", [
    ({"callIndex": 0, "argIndex": 0, "token": TARGET}, "bad_plan"),  # address argument, not a uint
    ({"callIndex": 0, "argIndex": 3, "token": TARGET}, "bad_plan"),  # no argument 3
    ({"callIndex": 0, "wordIndex": 3, "token": TARGET}, "bad_plan"),  # past the 3 argument words
    ({"callIndex": 1, "wordIndex": 0, "token": TARGET}, "bad_plan"),  # no settle call 1
    ({"callIndex": 0, "wordIndex": 1, "token": ROUTER}, "bad_target"),  # not the asset, a target or a sweep
    ({"callIndex": 0, "wordIndex": 1, "token": "$asset"}, None),
    ({"callIndex": 0, "argIndex": 1, "token": "$asset"}, None),
])
def test_patches_resolve_against_the_compiled_settle_call(patch, code):
    raw = plan(settleCalls=[_settle_swap()], settlePatches=[patch])
    if code is None:
        assert validate_generic_call_plan(raw, fund())["settlePatches"][0]["token"] == "$asset"
        return
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(raw, fund())
    assert exc.value.code == code


def test_a_patch_may_not_overwrite_a_transferfrom_address_word():
    settle = [{"target": TARGET, "data": "0x" + _sel("transferFrom(address,address,uint256)") + "00" * 96}]
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(plan(settleCalls=settle, settlePatches=[_patch(wordIndex=1)]), fund())
    assert exc.value.code == "bad_plan" and "address word" in exc.value.reason
    ok = validate_generic_call_plan(plan(settleCalls=settle, settlePatches=[_patch(wordIndex=2)]), fund())
    assert ok["settlePatches"][0]["wordIndex"] == 2


@pytest.mark.parametrize("call,code", [
    ({"target": TARGET, "data": "0xc0ffee00" + "ab" * 32, "value": "0x0"}, "bad_call"),
    ({"target": TARGET, "data": "0xc0ffee00" + "ab" * 32, "value": None}, "bad_call"),
    ({"target": TARGET, "data": "0xc0ffee00" + "ab" * 32, "value": False}, "bad_call"),
    ({"target": TARGET, "data": "0xc0ffee00" + "ab" * 32, "value": 1}, "bad_call"),
    ({"target": "$clone", "data": "0xc0ffee00" + "ab" * 32}, "bad_target"),
    ({"target": "$vault", "data": "0xc0ffee00" + "ab" * 32}, "bad_target"),
    ({"target": "$pullAmount", "data": "0xc0ffee00" + "ab" * 32}, "bad_target"),
    ({"target": TARGET, "signature": SWAP, "args": [USDG, "1"]}, "bad_call"),  # arity
    ({"target": TARGET, "signature": SWAP, "args": ["Moonwell", "1", "0"]}, "bad_call"),  # not an address
    ({"target": TARGET, "signature": SWAP, "args": [USDG, 2**53, "0"]}, "bad_call"),  # unsafe JSON number
    ({"target": TARGET, "signature": SWAP, "args": [USDG, "-1", "0"]}, "bad_call"),  # uint below 0
    ({"target": TARGET, "signature": SWAP, "args": [USDG, 1.5, "0"]}, "bad_call"),
    ({"target": TARGET, "signature": "f(bytes32)", "args": ["0xabcd"]}, "bad_call"),  # not 32 bytes
    ({"target": TARGET, "signature": "f(bytes)", "args": ["abcd"]}, "bad_call"),  # not 0x hex
    ({"target": TARGET, "signature": "f(bool)", "args": [1]}, "bad_call"),
    ({"target": TARGET, "signature": "f(fixed128x18)", "args": ["1"]}, "bad_call"),  # unknown type
    ({"target": TARGET, "signature": "f(uint256 address)", "args": ["1"]}, "bad_call"),  # keyword name
    ({"target": TARGET, "signature": "f (uint256)", "args": ["1"]}, "bad_call"),  # not a signature
    ({"target": TARGET, "signature": "f(function)", "args": ["0x"]}, "bad_call"),  # the CLI cannot encode it
    ({"target": TARGET, "signature": SWAP, "data": "0x" + _sel(SWAP) + "00" * 64}, "bad_calldata"),  # short
    ({"target": TARGET, "signature": "f(bool)", "data": "0x" + _sel("f(bool)") + "00" * 31 + "02"}, "bad_calldata"),
    ({"target": TARGET, "signature": "g(uint256)", "data": "0x" + _sel("f(uint256)") + "00" * 32}, "bad_calldata"),
    ({"target": TARGET, "data": "0xc0ffee00" + "ab" * 32, "args": []}, "bad_calldata"),
    ({"target": TARGET, "args": [1]}, "bad_calldata"),  # args with no signature
    ({"target": TARGET, "data": "0xc0ffee00" + "ab" * 32, "note": 5}, "bad_plan"),
])
def test_calls_the_cli_would_refuse_are_refused_here(call, code):
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(plan(executeCalls=[call]), fund(), pull_amount=100_000_000)
    assert exc.value.code == code


@pytest.mark.parametrize("call", [
    {"target": TARGET, "data": "0xc0ffee00" + "ab" * 32, "value": "0"},
    {"target": TARGET, "data": "0xc0ffee00" + "ab" * 32, "value": 0, "note": "swap leg"},
    {"target": "$asset", "signature": "approve(address,uint256)", "args": [ROUTER, "$pullAmount"]},
    {"target": TARGET, "signature": SWAP, "args": ["$asset", "$pullAmount", "0"]},
    {"target": TARGET, "signature": "function swap(address tokenIn, uint256 amountIn) external returns (uint256)",
     "args": [USDG, 2**53 - 1]},
    {"target": TARGET, "signature": "route((address,uint24)[],bytes,string)",
     "args": [[[USDG, 3000], {"0": TARGET, "1": 500}], "0x", "memo"]},
    {"target": TARGET, "signature": "route((address,uint24)[],bytes,string)",
     "args": ['[["0x' + "77" * 20 + '", 3000]]', "0xabcd", ""]},
    {"target": TARGET, "signature": SWAP, "data": "0x" + _sel(SWAP) + _addr_word(USDG) + "00" * 64},
    {"target": TARGET, "signature": "f(bool)", "data": "0x" + _sel("f(bool)") + "00" * 31 + "01"},
])
def test_calls_the_cli_compiles_are_accepted(call):
    stored = validate_generic_call_plan(plan(executeCalls=[call]), fund(), pull_amount=100_000_000)
    entry = stored["executeCalls"][0]
    assert "value" not in entry and entry["target"] == call["target"]
    if "note" in call:
        assert entry["note"] == call["note"]


def test_the_written_plan_holds_only_keys_the_cli_reads(agent_ctx, tmp_path):
    calls = plan(
        executeCalls=[{"target": "$asset", "signature": "approve(address,uint256)", "args": [ROUTER, "$pullAmount"],
                       "value": "0", "note": "  approve the router  "}],
        settleCalls=[_settle_swap()],
        settlePatches=[{"callIndex": 0, "argIndex": 1, "token": " $asset "}],
        sweepTokens=["$asset"],
    )
    drafted = _draft(agent_ctx, calls)
    assert drafted["ok"], drafted
    shipped = _service(tmp_path).ship_draft("alpha-fund", drafted["proposal"]["id"], caller_telegram_id="900", agent_signs=True)
    args = shlex.split(shipped.propose_command)
    written = json.loads(Path(args[args.index("--calls") + 1]).read_text())
    assert written == {
        "executeCalls": [{"target": "$asset", "args": [ROUTER, "$pullAmount"], "signature": "approve(address,uint256)",
                          "note": "approve the router"}],
        "settleCalls": [{"target": TARGET, "args": [USDG, "1", "0"], "signature": SWAP}],
        "settlePatches": [{"callIndex": 0, "argIndex": 1, "token": "$asset"}],
        "sweepTokens": ["$asset"],
    }


def test_the_asset_placeholder_is_held_to_the_asset_rule():
    with pytest.raises(RuleViolation) as exc:
        validate_generic_call_plan(plan(executeCalls=[{"target": "$asset", "data": "0xdeadbeef" + "ab" * 64}]), fund())
    assert exc.value.code == "bad_call"
    # Swept as $asset, the asset is an exit even without a settle leg; listed twice is refused.
    assert validate_generic_call_plan(plan(settleCalls=[], sweepTokens=["$asset"]), fund())["sweepTokens"] == ["$asset"]
    with pytest.raises(RuleViolation):
        validate_generic_call_plan(plan(settleCalls=[], sweepTokens=["$asset", USDG]), fund())


# -- the preview is not ready when this host's CLI cannot propose generic-call (M5) ----------------


def _discovery(monkeypatch, listing=None, *, error=None):
    result = (
        {"ok": False, "error": error, "available": [], "unavailable": []}
        if error else parse_strategy_list(listing)
    )
    monkeypatch.setattr("sherwood_monitor.fund.scope.discover_strategy_templates", lambda *a, **k: result)
    return result


def test_the_preview_is_not_ready_when_the_strategy_list_has_generic_call_unavailable(agent_ctx, monkeypatch):
    _discovery(monkeypatch, REAL_LISTING_0_103)  # CLI 0.103.0: "Not available on robinhood-mainnet"
    drafted = _draft(agent_ctx, plan())
    assert drafted["ok"], drafted
    prepare = drafted["prepare"]
    assert prepare["ready"] is False and prepare["signed"] is False
    (cli,) = [m for m in prepare["missing"] if m.startswith("cli:")]
    assert "Not available on robinhood-mainnet" in cli and "0.104.0" in cli


def test_the_preview_is_ready_when_the_list_prints_generic_call(agent_ctx, monkeypatch):
    _discovery(monkeypatch, listing_with_generic_call_available())
    prepare = _draft(agent_ctx, plan())["prepare"]
    assert prepare["ready"] is True and prepare["missing"] == []


def test_an_older_cli_that_does_not_list_generic_call_is_missing_too(agent_ctx, monkeypatch):
    _discovery(monkeypatch, REAL_LISTING_0_103.replace("    Generic Call (generic-call)\n", ""))
    prepare = _draft(agent_ctx, plan())["prepare"]
    assert prepare["ready"] is False and any("does not list generic-call at all" in m for m in prepare["missing"])


def test_an_unread_list_is_a_warning_not_a_ready_claim_about_the_cli(agent_ctx, monkeypatch):
    _discovery(monkeypatch, error="`sherwood strategy list` failed (TimeoutExpired)")
    prepare = _draft(agent_ctx, plan())["prepare"]
    assert prepare["ready"] is True
    assert any("could not be read" in w and "0.104.0" in w for w in prepare["body"]["warnings"])


def test_another_chain_is_not_handed_a_4663_template_and_moonwell_is_not_banned_by_name():
    other = fund(chain_id=8453, asset="WETH")
    stored = validate_generic_call_plan(plan(), other)
    preview = preview_generic_call(other, stored, max_capital=1, max_drawdown_bps=0, strategy_duration=60)
    assert preview["ready"] is False and preview["execution_acceptance"] is False
    assert any("address book" in item for item in preview["missing"])
    assert "Moonwell" not in str(preview)


# -- draft, ship, pending -----------------------------------------------------------------------


def test_agent_signer_ships_the_cli_command_and_writes_the_plan(agent_ctx, tmp_path, monkeypatch):
    drafted = _draft(agent_ctx, plan())
    assert drafted["ok"], drafted
    assert drafted["prepare"]["execution_acceptance"] is False and drafted["prepare"]["signed"] is False
    assert drafted["proposal"]["generic_call"]["executeCalls"]
    svc = _service(tmp_path)
    shipped = svc.ship_draft("alpha-fund", drafted["proposal"]["id"], caller_telegram_id="900", agent_signs=True)
    args = shlex.split(shipped.propose_command)
    assert args[:4] == ["sherwood", "strategy", "propose", "generic-call"]
    assert "--chain" not in shipped.propose_command
    assert args[args.index("--vault") + 1] == VAULT
    assert args[args.index("--amount") + 1] == "100"
    assert args[args.index("--duration") + 1] == "1h"
    assert args[args.index("--max-drawdown-bps") + 1] == "1000"
    path = Path(args[args.index("--calls") + 1])
    assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "executeCalls" in path.read_text()


def test_a_member_cannot_ship_and_the_host_signer_leaves_the_draft(agent_ctx, tmp_path):
    drafted = _draft(agent_ctx, plan())
    host_svc = _service(tmp_path, signer="host")
    with pytest.raises(RuleViolation) as member:
        host_svc.ship_draft("alpha-fund", drafted["proposal"]["id"], caller_telegram_id="1")
    assert member.value.code == "not_host"
    with pytest.raises(RuleViolation) as host_mode:
        host_svc.ship_draft("alpha-fund", drafted["proposal"]["id"], caller_telegram_id="900", agent_signs=False)
    assert host_mode.value.code == "draft_not_ready" and "generic-call" in host_mode.value.reason
    assert host_svc.load("alpha-fund").proposal(drafted["proposal"]["id"]).shipped_at is None


def test_the_host_signer_refuses_a_generic_call_draft_and_keeps_the_slot_free(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, HOST_CFG)  # proposal_signer defaults to host
    assert ctx.call_tool("fund_setup", SETUP)["created"]
    hook = ctx.hooks["pre_tool_call"][0]
    ctx.call_tool("fund_claim_topic", {"topic": "custom route"})
    args = {
        "topic": "custom route", "thesis": "a custom call", "template": "generic-call", "calls": plan(),
        "max_capital": "100", "max_drawdown_bps": 1000, "strategy_duration_seconds": 3600,
    }
    blocked = asyncio.run(hook(tool_name="fund_draft_proposal", args=args))
    assert blocked["action"] == "block" and "agent_signer_only" in blocked["message"]
    assert "proposal_signer: agent" in blocked["message"]
    drafted = ctx.call_tool("fund_draft_proposal", args)
    assert drafted["ok"] is False and drafted["code"] == "agent_signer_only"
    assert "no prepare route" in drafted["error"]
    status = ctx.call_tool("fund_status")
    assert status["open_proposals"] == []  # nothing holds the open-proposal slot
    # The claim is still the agent's, so it can draft the topic as a portfolio instead.
    assert [c["topic"] for c in status["claims"]] == ["custom route"]


def test_a_service_draft_under_the_host_signer_is_refused(tmp_path):
    svc = _service(tmp_path, signer="host")
    svc.create_fund(fund(), Agent(name="alpha", identity_id="101", address=HOST_ADDR))
    svc.claim_topic("alpha-fund", "alpha", "custom route")
    with pytest.raises(RuleViolation) as exc:
        svc.draft_proposal(
            "alpha-fund", "alpha", topic="custom route", thesis="t", legs=[], max_capital=100_000_000,
            max_drawdown_bps=1000, strategy_duration=3600, generic_call=plan(),
        )
    assert exc.value.code == "agent_signer_only"
    assert svc.load("alpha-fund").proposals == []


def test_pending_generic_call_needs_cli_0_104_and_a_card_timeout_holds_it(agent_ctx, tmp_path, monkeypatch):
    drafted = _draft(agent_ctx, plan())
    _service(tmp_path).ship_draft("alpha-fund", drafted["proposal"]["id"], caller_telegram_id="900", agent_signs=True)

    def idle(url, timeout):
        return 200, {"success": True, "data": {
            "proposeGate": {"openProposals": 0, "cooldownUntil": "0", "ready": True},
            "proposals": {"count": 0, "open": [], "recent": []},
            "warnings": [],
        }}

    monkeypatch.setattr("sherwood_monitor.fund.txbuilder._urllib_get_json", idle)
    fake_version(monkeypatch, "0.104.0")
    listed = agent_ctx.call_tool("fund_pending_commands")
    assert listed["ok"] and listed["proposals"] and "generic-call" in listed["proposals"][0]["command"]
    assert MIN_CLI_FOR_GENERIC_CALL == "0.104.0"
    assert "cli_warning" not in listed and "0.104.0" in listed["note"]

    fake_version(monkeypatch, "0.103.0")
    old = agent_ctx.call_tool("fund_pending_commands", {"include_recent": True})
    assert "0.103.0" in old["cli_warning"] and "0.104.0" in old["cli_warning"]

    def timed_out(self, vault):
        raise TxBuilderError("fund-card", "timed out")

    monkeypatch.setattr(TxBuilderClient, "fund_card", timed_out)
    held = agent_ctx.call_tool("fund_pending_commands", {"include_recent": True})
    assert held["proposals"] == [] and "timed out" in held["chain_hold"]


def test_the_guard_blocks_a_bad_plan_before_the_handler(agent_ctx):
    agent_ctx.call_tool("fund_claim_topic", {"topic": "custom route"})
    hook = agent_ctx.hooks["pre_tool_call"][0]
    blocked = asyncio.run(hook(tool_name="fund_draft_proposal", args={
        "topic": "custom route", "thesis": "x", "template": "generic-call",
        "calls": {"executeCalls": [{"target": TARGET, "data": "0xzz"}], "settleCalls": [], "settlePatches": [], "sweepTokens": []},
        "max_capital": "100", "max_drawdown_bps": 1000,
    }))
    assert blocked["action"] == "block" and "bad_calldata" in blocked["message"]
    # An operator surface (no chat session) does not silently allow a host tool.
    asked = asyncio.run(hook(tool_name="fund_set_proposal_status", args={"proposal_id": "p-1", "status": "cancelled"}))
    assert asked["action"] == "approve"


def test_a_telegram_member_is_denied_host_tools(agent_ctx, monkeypatch):
    monkeypatch.setattr(session, "_session_env", lambda name: {
        "HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_USER_ID": "1", "HERMES_SESSION_CHAT_ID": "-100500",
    }.get(name, ""))
    out = agent_ctx.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "cancelled"})
    assert out["ok"] is False and out["code"] == "not_host"
    hook = agent_ctx.hooks["pre_tool_call"][0]
    blocked = asyncio.run(hook(tool_name="fund_record_settle", args={"proposal_id": "p-1", "pnl": "1", "tx_hash": "0x" + "ab" * 32}))
    assert blocked["action"] == "block" and "not_host" in blocked["message"]


def test_cancel_from_approved_frees_the_slot_and_settle_waits_for_executed(tmp_path):
    clock = Clock()
    svc = FundService(LocalJsonBackend(tmp_path), clock=clock, proposal_signer="agent")
    svc.create_fund(fund(), Agent(name="alpha", identity_id="101", address=HOST_ADDR))
    svc.claim_topic("alpha-fund", "alpha", "custom route")
    rec = svc.draft_proposal(
        "alpha-fund", "alpha", topic="custom route", thesis="a custom call on this vault", legs=[],
        max_capital=100_000_000, max_drawdown_bps=1000, strategy_duration=3600, generic_call=plan(),
    ).proposal
    for status in ("proposed", "passed", "approved"):
        svc.set_proposal_status("alpha-fund", "alpha", rec.id, status, onchain_id=7, tx_hash="0x" + "ab" * 32)
    with pytest.raises(RuleViolation) as early:
        svc.record_settle("alpha-fund", "alpha", rec.id, pnl=0, tx_hash="0x" + "cd" * 32)
    assert early.value.code == "bad_transition"
    svc.set_proposal_status("alpha-fund", "alpha", rec.id, "cancelled")
    assert svc.load("alpha-fund").proposal(rec.id).status == "cancelled"
    svc.claim_topic("alpha-fund", "alpha", "second route")
    again = svc.draft_proposal(
        "alpha-fund", "alpha", topic="second route", thesis="another custom call", legs=[],
        max_capital=50_000_000, max_drawdown_bps=500, strategy_duration=60, generic_call=plan(),
    ).proposal
    svc.set_proposal_status("alpha-fund", "alpha", again.id, "proposed", onchain_id=8, tx_hash="0x" + "11" * 32)
    svc.set_proposal_status("alpha-fund", "alpha", again.id, "passed")
    svc.set_proposal_status("alpha-fund", "alpha", again.id, "approved")
    svc.set_proposal_status("alpha-fund", "alpha", again.id, "executed", tx_hash="0x" + "22" * 32)
    with pytest.raises(RuleViolation) as stuck:
        svc.set_proposal_status("alpha-fund", "alpha", again.id, "cancelled")
    assert stuck.value.code == "bad_transition"
    receipt = svc.record_settle("alpha-fund", "alpha", again.id, pnl=-1_000_000, pnl_bps=-100, tx_hash="0x" + "33" * 32)
    assert receipt.tx_hash.endswith("33" * 32) and svc.load("alpha-fund").proposal(again.id).status == "settled"


def test_an_http_backend_does_not_invent_a_plan_path():
    rec = ProposalRecord(id="p-1", proposer_agent="alpha", thesis="t", generic_call=plan())
    with pytest.raises(RuleViolation) as exc:
        write_generic_call_plan(SimpleNamespace(), "alpha-fund", rec)
    assert exc.value.code == "draft_not_ready"


# -- scope --------------------------------------------------------------------------------------


def test_strategy_list_splits_available_from_unavailable_and_a_timeout_is_not_a_list():
    parsed = parse_strategy_list(LISTING)
    assert parsed["network"] == "Robinhood Chain"
    assert parsed["available"] == [{"name": "Portfolio", "key": "portfolio"}]
    assert parsed["unavailable"][0]["key"] == "moonwell-supply"

    def timed_out(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    missed = discover_strategy_templates("sherwood-scope-test", runner=timed_out, refresh=True)
    assert missed["ok"] is False and missed["available"] == []


def test_the_real_0_103_listing_never_makes_the_header_a_template():
    parsed = parse_strategy_list(REAL_LISTING_0_103)
    keys = [row["key"] for row in parsed["available"]]
    assert parsed["network"] == "robinhood-mainnet"
    assert "robinhood-mainnet" not in keys
    assert keys == ["portfolio", "morpho-supply", "concentrated-liquidity", "lighter-perp", "launchpad"]
    unavailable = [row["key"] for row in parsed["unavailable"]]
    assert "generic-call" in unavailable and "moonwell-supply" in unavailable
    assert "robinhood-mainnet" not in unavailable
    # Descriptions ("... (--market-id, --amount)"), Template: lines and the footer are not rows.
    assert all(" " not in key for key in keys + unavailable)
    text = scope_text(fund_scope(FundState(fund=fund()), parsed))
    assert "robinhood-mainnet, portfolio" not in text and ": portfolio, morpho-supply" in text


def test_a_0_104_shaped_listing_reads_generic_call_as_available():
    parsed = parse_strategy_list(listing_with_generic_call_available())
    assert "generic-call" in [row["key"] for row in parsed["available"]]
    assert "generic-call" not in [row["key"] for row in parsed["unavailable"]]


# -- discovery: cached with a TTL, off the event loop, fail soft (M6, L3, N3) --------------------


class _Runner:
    def __init__(self, *, fail: bool = False, delay: float = 0.0):
        self.calls = 0
        self.threads: list[int] = []
        self.fail = fail
        self.delay = delay

    def __call__(self, argv, timeout):
        import threading
        import time as _time

        self.calls += 1
        self.threads.append(threading.get_ident())
        if self.delay:
            _time.sleep(self.delay)
        if self.fail:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, 0, REAL_LISTING_0_103, "")


def test_a_read_is_reused_for_ten_minutes_and_a_failure_for_one(monkeypatch):
    from sherwood_monitor.fund import scope

    now = [1000.0]
    monkeypatch.setattr(scope, "_clock", lambda: now[0])
    ok = _Runner()
    assert discover_strategy_templates("sherwood-ttl-ok", runner=ok)["ok"]
    now[0] += scope.DISCOVERY_OK_TTL_S - 1
    discover_strategy_templates("sherwood-ttl-ok", runner=ok)
    assert ok.calls == 1
    now[0] += 2  # past ten minutes: a CLI upgrade shows up without a restart
    discover_strategy_templates("sherwood-ttl-ok", runner=ok)
    assert ok.calls == 2

    bad = _Runner(fail=True)
    assert discover_strategy_templates("sherwood-ttl-bad", runner=bad)["ok"] is False
    now[0] += scope.DISCOVERY_FAIL_TTL_S - 1
    discover_strategy_templates("sherwood-ttl-bad", runner=bad)
    assert bad.calls == 1
    now[0] += 2  # one cold timeout does not blind every later turn
    discover_strategy_templates("sherwood-ttl-bad", runner=bad)
    assert bad.calls == 2


def test_errors_name_the_command_not_the_binary_path(monkeypatch):
    from sherwood_monitor.fund import scope

    monkeypatch.setattr(scope, "_which", lambda name: None)
    missing = discover_strategy_templates("/opt/private/tools/sherwood")
    assert missing["ok"] is False and "/opt/private" not in missing["error"]

    def broken(argv, timeout):
        raise OSError(argv[0])

    failed = discover_strategy_templates("/opt/private/tools/sherwood-2", runner=broken)
    assert "/opt/private" not in failed["error"] and "`sherwood strategy list`" in failed["error"]


def test_the_scope_source_reads_off_the_event_loop_within_budget(tmp_path, monkeypatch):
    import threading
    import time as _time

    from sherwood_monitor.fund import scope
    from sherwood_monitor.fund.scope import make_fund_scope_source

    rt = _runtime(tmp_path)
    rt.sherwood_bin = "sherwood-slow-test"
    rt.service.create_fund(fund(), host_agent())
    monkeypatch.setattr(scope, "DISCOVERY_TIMEOUT_S", 0.2)
    slow = _Runner(delay=1.5)
    monkeypatch.setattr(scope, "_run", slow)
    source = make_fund_scope_source(rt)
    assert asyncio.iscoroutinefunction(source)
    ticks = []

    async def turn():
        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.02)

        task = asyncio.create_task(ticker())
        started = _time.monotonic()
        out = await source(session_id="s1")
        task.cancel()
        return out, _time.monotonic() - started

    out, elapsed = asyncio.run(turn())
    assert elapsed < 1.2  # the turn did not wait for the 1.5 s read
    assert len(ticks) >= 5  # the loop kept running while the list was read
    assert slow.threads and slow.threads[0] != threading.get_ident()
    assert "Templates unknown" in out["context"] and "Do not invent" in out["context"]


def test_the_skill_pack_is_scanned_once_per_chain():
    from sherwood_monitor.fund import scope

    scope._reference_skills.cache_clear()
    first = reference_skills(CHAIN_ID_ROBINHOOD)
    first.append({"name": "mutated"})  # a caller's copy, not the cache
    assert reference_skills(CHAIN_ID_ROBINHOOD) != first
    assert scope._reference_skills.cache_info().misses == 1


def test_tests_never_run_the_real_cli():
    from sherwood_monitor.fund import scope

    assert scope._run.__name__ == "<lambda>"  # the conftest stub, whatever `sherwood` is on PATH
    assert discover_strategy_templates()["network"] == "robinhood-mainnet"


def test_a_4663_fund_labels_other_network_skills_and_does_not_quote_yields():
    discovered = {
        "ok": True, "source": "sherwood strategy list", "network": "Robinhood Chain",
        "available": [{"name": "Portfolio", "key": "portfolio"}],
        "unavailable": [{"name": "Moonwell Supply", "key": "moonwell-supply"}],
    }
    scope = fund_scope(FundState(fund=fund()), discovered)
    text = scope_text(scope)
    assert scope["chain_id"] == CHAIN_ID_ROBINHOOD and scope["robinhood_brokerage"] is False
    assert scope["vault"] == VAULT and "portfolio" in scope["strategies"]["available"]
    assert "moonwell-supply" in scope["reference_only_skills"]
    assert "levered-swap" in scope["reference_only_skills"]
    assert "memecoin-alpha" in scope["reference_only_skills"]
    assert "Moonwell" in text and "Other-network" in text
    assert "%" not in text and "3–8" not in text and "3-8" not in text
    assert "execution acceptance" in text
    # Listed as available: do not keep calling that template an out-of-network example.
    discovered["available"].append({"name": "Moonwell Supply", "key": "moonwell-supply"})
    scoped = scope_text(fund_scope(FundState(fund=fund()), discovered))
    assert "Moonwell" not in scoped.split("Other-network examples", 1)[-1].split(".", 1)[0]


def test_another_chain_does_not_inherit_the_4663_skill_ban():
    assert reference_skills(8453) == []
    scope = fund_scope(
        FundState(fund=fund(chain_id=8453, asset="WETH")),
        {"ok": False, "error": "not checked", "available": [], "unavailable": []},
    )
    assert scope["reference_only_skills"] == []
    text = scope_text(scope)
    assert "moonwell-supply" not in text
    assert "do not assume one is executable" in text.lower() or "Still do not assume" in text


def test_fund_status_and_pre_llm_speak_for_the_configured_fund(agent_ctx, monkeypatch):
    monkeypatch.setattr(
        "sherwood_monitor.fund.scope.discover_strategy_templates",
        lambda *a, **k: {
            "ok": True, "source": "sherwood strategy list", "network": "Robinhood Chain",
            "available": [{"name": "Portfolio", "key": "portfolio"}],
            "unavailable": [{"name": "Moonwell Supply", "key": "moonwell-supply"}],
        },
    )
    status = agent_ctx.call_tool("fund_status")
    assert status["scope"]["chain_id"] == 4663 and status["scope"]["robinhood_brokerage"] is False
    assert status["scope"]["vault"] == VAULT
    assert "portfolio" in status["scope"]["strategies"]["available"]
    assert "moonwell-supply" not in status["scope"]["strategies"]["available"]
    (out,) = asyncio.run(agent_ctx.fire("pre_llm_call", session_id="s1"))
    block = out["context"].split("<sherwood-fund-scope>", 1)[1].split("</sherwood-fund-scope>", 1)[0]
    assert "4663" in block and "brokerage" in block and "Moonwell" in block
    assert "wired up" in block and "%" not in block and "3–8" not in block and "3-8" not in block
    assert len(agent_ctx.hooks["pre_llm_call"]) == 1


def test_no_fund_at_all_adds_no_scope_block(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, {})
    (out,) = asyncio.run(ctx.fire("pre_llm_call", session_id="s1"))
    assert "sherwood-fund-scope" not in (out or {}).get("context", "")


# -- the scope is for this turn's fund, and only in that fund's chat (M2) -----------------------

B_VAULT = "0x" + "99" * 20
B_CHAT = "-100600"


def _runtime(tmp_path, **settings):
    from sherwood_monitor.fund.runtime import FundRuntime
    from sherwood_monitor.fund.settings import FundSettings

    svc = FundService(LocalJsonBackend(tmp_path / "funds"))
    return FundRuntime(svc, FundSettings(**settings), None, "sherwood-scope-test")


def _scope(rt, **kwargs):
    from sherwood_monitor.fund.scope import make_fund_scope_source

    out = make_fund_scope_source(rt)(**kwargs)
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    return (out or {}).get("context", "")


@pytest.fixture
def two_funds(tmp_path, monkeypatch):
    monkeypatch.setattr("sherwood_monitor.fund.scope.discover_strategy_templates", lambda *a, **k: {
        "ok": True, "source": "sherwood strategy list", "network": "robinhood-mainnet",
        "available": [{"name": "Portfolio", "key": "portfolio"}], "unavailable": [],
    })
    rt = _runtime(tmp_path, default_fund="alpha-fund", owner_telegram_id="4242")
    rt.service.create_fund(fund(), host_agent())
    rt.service.create_fund(
        fund(name="beta-fund", vault=B_VAULT, telegram_chat_id=B_CHAT, host_telegram_id="901"), host_agent(),
    )
    rt.service.request_membership("beta-fund", "31")
    return rt


def test_a_turn_in_fund_bs_chat_is_scoped_to_fund_b_not_the_default(two_funds):
    text = _scope(two_funds, platform="telegram", chat_id=B_CHAT, sender_id="901")
    assert "Active fund beta-fund" in text and B_VAULT in text
    assert "alpha-fund" not in text and VAULT not in text
    member = _scope(two_funds, platform="telegram", chat_id=B_CHAT, sender_id="31")
    assert "Active fund beta-fund" in member
    owner = _scope(two_funds, platform="telegram", chat_id=B_CHAT, sender_id="4242")
    assert "Active fund beta-fund" in owner


@pytest.mark.parametrize("kwargs", [
    {"platform": "telegram", "chat_id": "-100999", "sender_id": "900"},  # a chat no fund is bound to
    {"platform": "telegram", "chat_id": "900", "sender_id": "900"},  # the host's DM with the bot
    {"platform": "telegram", "chat_id": B_CHAT, "sender_id": "66"},  # a stranger in fund B's group
    {"platform": "telegram", "chat_id": "-100500", "sender_id": "901"},  # B's host in A's group
    {"platform": "discord", "chat_id": "-100777", "sender_id": "900"},  # another chat surface, no fund
])
def test_other_chats_dms_and_strangers_get_no_fund_context(two_funds, kwargs):
    assert _scope(two_funds, **kwargs) == ""


def test_an_operator_turn_gets_the_default_fund(two_funds):
    text = _scope(two_funds, session_id="s1")
    assert "Active fund alpha-fund" in text and VAULT in text


def test_one_fund_and_no_default_is_that_fund_not_a_no_default_line(tmp_path, monkeypatch):
    monkeypatch.setattr("sherwood_monitor.fund.scope.discover_strategy_templates", lambda *a, **k: {
        "ok": False, "error": "not checked", "available": [], "unavailable": [],
    })
    rt = _runtime(tmp_path)
    rt.service.create_fund(fund(), host_agent())
    text = _scope(rt, session_id="s1")
    assert "Active fund alpha-fund" in text and "No default fund" not in text
    assert "Active fund alpha-fund" in _scope(rt, platform="telegram", chat_id="-100500", sender_id="900")


def test_several_funds_and_no_default_tell_an_operator_turn_to_pick_one(tmp_path):
    rt = _runtime(tmp_path)
    rt.service.create_fund(fund(), host_agent())
    rt.service.create_fund(fund(name="beta-fund", vault=B_VAULT, telegram_chat_id=B_CHAT), host_agent())
    text = _scope(rt, session_id="s1")
    assert "Several funds" in text and "wired up" in text and VAULT not in text


# -- NAV: 60 cash + 40 invested at cost is 100, not 60 and not -40% ----------------------------


def _executed(**active):
    card = copy.deepcopy(load_fixture("tg_card_executed_usdg_synthetic"))
    row = card["data"]["activeProposal"]
    row.update(active)
    card["data"]["activeProposal"] = {k: v for k, v in row.items() if v is not None}
    return card


def test_nav_100_with_40_invested_is_at_cost_not_a_loss(tmp_path):
    clock = Clock()
    svc = FundService(LocalJsonBackend(tmp_path), clock=clock)
    svc.create_fund(fund(open_deposits=True), host_agent())
    svc.apply_chain_card("alpha-fund", _executed(positionValue=None, positionValueValid=None))
    snap = svc.load("alpha-fund").nav
    assert snap.cash_units == 60_000_000 and snap.invested_units == 40_000_000
    assert snap.at_cost and not snap.nav_partial and snap.nav == 100 and snap.pnl_bps == 0
    # Mark to market when the read has a live value.
    svc.apply_chain_card("alpha-fund", load_fixture("tg_card_executed_usdg_synthetic"))
    live = svc.load("alpha-fund").nav
    assert live.nav == Decimal("100.6") and not live.at_cost and live.pnl_bps == 60
    # Unknown value: a floor, never a -40% print.
    unknown = _executed(positionValue=None, positionValueValid=None, positions=None)
    unknown["data"]["proposals"] = {"count": "1", "open": [], "recent": []}
    svc.apply_chain_card("alpha-fund", unknown)
    partial = svc.load("alpha-fund").nav
    assert partial.nav_partial and partial.pnl_bps == 0 and partial.nav == 60
    from sherwood_monitor.fund.telegram import fund_dict
    stale = fund_dict(svc.load("alpha-fund"), now=partial.read_at + 1000)
    assert stale["nav_stale"] is True and stale["nav_partial"] is True and stale["pnl_bps"] == 0


# -- register() in an empty home (an import check, not a cold install) ---------------------------


def test_register_in_an_empty_home_is_an_import_check_not_a_cold_install(tmp_path, monkeypatch):
    """Loads the plugin in a scratch home. hermes is not invoked, so this is not a cold install."""
    for key in ("TELEGRAM_BOT_TOKEN", "SHERWOOD_PRIVATE_KEY", "SHERWOOD_TG_LINK_SECRET", "SHERWOOD_TG_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    ctx = boot(tmp_path, monkeypatch, {})
    assert "fund_draft_proposal" in ctx.tools and "fund_status" in ctx.tools
