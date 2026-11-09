"""Approval forms the unlimited-approval ban missed (review R1 on PR #36, head 680c802), and the
bound the plugin now puts on approvals.

r1/r2: an unlimited approve inside multicall(bytes[]) (signature + args, or raw data); r3: the
older OpenZeppelin increaseApproval; r4: ERC777 authorizeOperator; r5: an approve just under
2^255. Local fixtures only: nothing here signs, broadcasts or reads a chain.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from eth_utils import function_signature_to_4byte_selector

from sherwood_monitor.fund.generic_call import validate_generic_call_plan
from sherwood_monitor.fund.models import TOKEN_ADDRESSES_4663
from sherwood_monitor.fund.rules import RuleViolation
from tests.fund_helpers import fund
from tests.test_generic_call_and_scope import _draft, agent_ctx, plan  # noqa: F401  (fixture)

USDG = TOKEN_ADDRESSES_4663["USDG"]
TARGET = "0x" + "77" * 20  # a token the clone holds, not the vault asset
ROUTER = "0x" + "88" * 20
PERMIT2 = "0x" + "99" * 20
PULL = 100_000_000  # 100 USDG
MAX = 2**256 - 1


def _sel(signature: str) -> str:
    return function_signature_to_4byte_selector(signature).hex()


def _word(n: int) -> bytes:
    return n.to_bytes(32, "big")


def _addr(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def _call(signature: str, *words: bytes) -> bytes:
    return bytes.fromhex(_sel(signature)) + b"".join(words)


def _bytes_array(items: list[bytes]) -> bytes:
    """ABI encoding of a bytes[] value (length, offsets, then each item padded)."""
    offsets, tails = [], b""
    for item in items:
        offsets.append(_word(32 * len(items) + len(tails)))
        tails += _word(len(item)) + item + bytes(-len(item) % 32)
    return _word(len(items)) + b"".join(offsets) + tails


def _multicall(items: list[bytes]) -> bytes:
    return bytes.fromhex(_sel("multicall(bytes[])")) + _word(32) + _bytes_array(items)


APPROVE_MAX = _call("approve(address,uint256)", _addr(ROUTER), _word(MAX))
APPROVE_SMALL = _call("approve(address,uint256)", _addr(ROUTER), _word(1_000))
SWAP = _call("swapExactIn(address,uint256,uint256)", _addr(USDG), _word(1), _word(0))


def _hex(b: bytes) -> str:
    return "0x" + b.hex()


def _validate(*calls: dict, settle: list | None = None, patches: list | None = None, pull: int = PULL) -> dict:
    kw = {"executeCalls": list(calls)}
    if settle is not None:
        kw["settleCalls"] = settle
    if patches is not None:
        kw["settlePatches"] = patches
    return validate_generic_call_plan(plan(**kw), fund(), pull_amount=pull)


# -- r1/r2: approvals inside wrappers ------------------------------------------------------------


@pytest.mark.parametrize("call", [
    {"target": TARGET, "signature": "multicall(bytes[])", "args": [[_hex(APPROVE_MAX)]]},  # r1
    {"target": TARGET, "data": _hex(_multicall([APPROVE_MAX]))},  # r2
    {"target": TARGET, "data": _hex(_multicall([SWAP, APPROVE_MAX]))},  # the second inner call
    {"target": TARGET, "signature": "multicall(uint256,bytes[])", "args": ["1", [_hex(APPROVE_MAX)]]},
    {"target": TARGET, "signature": "multicall(bytes32,bytes[])", "args": ["0x" + "00" * 32, [_hex(APPROVE_MAX)]]},
    {"target": TARGET, "signature": "batch(bytes[],bool)", "args": [[_hex(APPROVE_MAX)], True]},
    {"target": ROUTER, "signature": "aggregate((address,bytes)[])", "args": [[[TARGET, _hex(APPROVE_MAX)]]]},
    {"target": ROUTER, "signature": "aggregate3((address,bool,bytes)[])", "args": [[[TARGET, False, _hex(APPROVE_MAX)]]]},
    {"target": ROUTER, "signature": "aggregate3Value((address,bool,uint256,bytes)[])",
     "args": [[[TARGET, False, "0", _hex(APPROVE_MAX)]]]},
    {"target": ROUTER, "signature": "tryAggregate(bool,(address,bytes)[])", "args": [False, [[TARGET, _hex(APPROVE_MAX)]]]},
    {"target": TARGET, "data": _hex(_multicall([_multicall([APPROVE_MAX])]))},  # a wrapper in a wrapper
    {"target": TARGET, "data": _hex(_multicall([_call("setApprovalForAll(address,bool)", _addr(ROUTER), _word(1))]))},
])
def test_r1_an_unlimited_approval_inside_a_wrapper_is_refused(call):
    with pytest.raises(RuleViolation) as exc:
        _validate(call)
    assert exc.value.code == "unlimited_approve", exc.value.reason
    assert "call 0" in exc.value.reason or "call 1" in exc.value.reason


def test_r1_harmless_wrapped_calls_are_allowed():
    stored = _validate({"target": TARGET, "data": _hex(_multicall([SWAP, APPROVE_SMALL]))})
    assert stored["executeCalls"][0]["data"].startswith("0x" + _sel("multicall(bytes[])"))


def test_r1_a_wrapper_that_does_not_decode_or_nests_too_deep_is_refused():
    garbled = bytes.fromhex(_sel("multicall(bytes[])")) + _word(32) + _word(5)  # five items, none present
    with pytest.raises(RuleViolation) as bad:
        _validate({"target": TARGET, "data": _hex(garbled)})
    assert bad.value.code == "bad_calldata" and "cannot be checked" in bad.value.reason
    deep = SWAP
    for _ in range(4):
        deep = _multicall([deep])
    with pytest.raises(RuleViolation) as nested:
        _validate({"target": TARGET, "data": _hex(deep)})
    assert nested.value.code == "bad_calldata" and "deeper than 3" in nested.value.reason


def test_r1_the_guard_blocks_a_wrapped_approval_before_the_handler(agent_ctx):  # noqa: F811
    agent_ctx.call_tool("fund_claim_topic", {"topic": "custom route"})
    hook = agent_ctx.hooks["pre_tool_call"][0]
    calls = plan(executeCalls=[{"target": TARGET, "signature": "multicall(bytes[])", "args": [[_hex(APPROVE_MAX)]]}])
    blocked = asyncio.run(hook(tool_name="fund_draft_proposal", args={
        "topic": "custom route", "thesis": "x", "template": "generic-call", "calls": calls,
        "max_capital": "100", "max_drawdown_bps": 1000,
    }))
    assert blocked["action"] == "block" and "unlimited_approve" in blocked["message"]


# -- r3/r4: approval forms by other names ------------------------------------------------------------


@pytest.mark.parametrize("call", [
    {"target": TARGET, "signature": "increaseApproval(address,uint256)", "args": [ROUTER, str(MAX)]},  # r3
    {"target": TARGET, "signature": "authorizeOperator(address)", "args": [ROUTER]},  # r4
    {"target": TARGET, "data": _hex(_call("authorizeOperator(address)", _addr(ROUTER)))},
    {"target": TARGET, "signature": "approveAndCall(address,uint256,bytes)", "args": [ROUTER, str(MAX), "0x"]},
])
def test_r3_r4_approval_forms_by_other_names_are_refused(call):
    with pytest.raises(RuleViolation) as exc:
        _validate(call)
    assert exc.value.code == "unlimited_approve"


# -- r5: the bound -------------------------------------------------------------------------------------


@pytest.mark.parametrize("call", [
    {"target": USDG, "signature": "approve(address,uint256)", "args": [ROUTER, str(2**255 - 1)]},  # r5 on the asset
    {"target": "$asset", "signature": "approve(address,uint256)", "args": [ROUTER, str(PULL + 1)]},
    {"target": USDG, "signature": "increaseAllowance(address,uint256)", "args": [ROUTER, str(PULL + 1)]},
    {"target": PERMIT2, "signature": "approve(address,address,uint160,uint48)", "args": [USDG, ROUTER, str(PULL + 1), 0]},
    {"target": ROUTER, "signature": "aggregate((address,bytes)[])",
     "args": [[[USDG, _hex(_call("approve(address,uint256)", _addr(ROUTER), _word(PULL + 1)))]]]},
])
def test_r5_an_approval_of_the_vault_asset_above_the_pull_is_refused(call):
    with pytest.raises(RuleViolation) as exc:
        _validate(call)
    assert exc.value.code == "approve_above_pull" and str(PULL) in exc.value.reason


@pytest.mark.parametrize("args", [[ROUTER, str(PULL)], [ROUTER, "$pullAmount"], [ROUTER, "0"]])
def test_r5_an_approval_of_the_vault_asset_up_to_the_pull_is_allowed(args):
    assert _validate({"target": USDG, "signature": "approve(address,uint256)", "args": args})


def test_r5_another_token_keeps_the_documented_hard_bound():
    """The plan says nothing about how much of another token the clone will hold, so the only
    bound there is 2^255 (2^159 for Permit2): 2^255-1 passes, 2^255 does not."""
    assert _validate({"target": TARGET, "signature": "approve(address,uint256)", "args": [ROUTER, str(2**255 - 1)]})
    with pytest.raises(RuleViolation) as exc:
        _validate({"target": TARGET, "signature": "approve(address,uint256)", "args": [ROUTER, str(2**255)]})
    assert exc.value.code == "unlimited_approve"


def test_a_patch_may_not_scale_a_balance_up_into_an_approving_call():
    settle = [{"target": TARGET, "signature": "approve(address,uint256)", "args": [ROUTER, "0"]}]
    big = [{"callIndex": 0, "argIndex": 1, "token": TARGET, "mulNum": str(2**200), "mulDen": "1"}]
    with pytest.raises(RuleViolation) as exc:
        _validate(settle=settle, patches=big)
    assert exc.value.code == "unlimited_approve" and "mulNum" in exc.value.reason
    for scales in ({}, {"mulNum": "1", "mulDen": "2"}, {"mulNum": "3", "mulDen": "3"}):
        patch = {"callIndex": 0, "argIndex": 1, "token": TARGET, **scales}
        assert _validate(settle=settle, patches=[patch])["settlePatches"][0]["argIndex"] == 1


# -- N1': deep nesting is a refusal, not a RecursionError ----------------------------------------------


def _nested(depth: int) -> object:
    value: object = "1"
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("signature,arg", [
    ("f(" + "(" * 500 + "uint256" + ")" * 500 + ")", _nested(500)),  # review N1': RecursionError before
    ("f(uint256" + "[]" * 500 + ")", _nested(500)),
    ("f(" + "(" * 16 + "uint256" + ")" * 16 + ")", _nested(16)),  # valid args, refused by the cap
    ("f(uint256" + "[]" * 16 + ")", _nested(16)),
])
def test_deeply_nested_signatures_are_refused_cleanly(signature, arg):
    with pytest.raises(RuleViolation) as exc:
        _validate({"target": TARGET, "signature": signature, "args": [arg]})
    assert exc.value.code == "bad_call" and "deeper than" in exc.value.reason


def test_ordinary_nesting_still_compiles():
    assert _validate({"target": TARGET, "signature": "f(((uint256)[2])[])", "args": [[[[["1"], ["2"]]]]]})


# -- the disclosure says what is refused, not "any form" ------------------------------------------


def test_the_disclosure_names_the_refused_forms_and_the_pull_bound():
    text = " ".join((Path(__file__).parent.parent / "docs" / "hackathon-disclosure.md").read_text().split())
    assert "unlimited approvals in any form" not in text
    readme = " ".join((Path(__file__).parent.parent / "README.md").read_text().split())
    for doc in (text, readme):
        assert "still pass" not in doc  # the three forms the 0.8.3 docs listed as passing are refused now
    for needle in ("multicall(bytes[])", "increaseApproval", "authorizeOperator", "setApprovalForAll",
                   "above the plan's pull", "2^255", "2^159"):
        assert needle in text, needle
