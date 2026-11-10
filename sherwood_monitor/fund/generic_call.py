"""Generic Call proposal preparation (Sherwood CLI ``strategy propose generic-call``).

Checked against the published ``@sherwoodagent/cli@0.104.0`` command surface
(``--calls <plan.json>``, ``--amount``, ``--duration``, ``--max-drawdown-bps``,
``--vault``, ``--name``, ``--description``). The plugin does not encode init
data, predict the clone, sign, or broadcast. It checks the plan's shape, shows
an unsigned preview, and — only after the host ships a draft under
``proposal_signer: agent`` — writes the plan file and the exact CLI command.

A passing check here is not ``simulate_batch`` and not execution acceptance.
CLI 0.104.0 applies the governor's active-proposal state only when the simulate
API is given a proposal id; the eth_call fallback never does
(``NotActiveProposalStrategy``). Non-core clones are a guardian/protocol
matter: this module does not waive that gate.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, NoReturn

from .callspec import (
    MAX_SAFE_INTEGER,
    Approval,
    Binding,
    Function,
    SpecError,
    address_words,
    approvals_in,
    decode_check,
    encode_call,
    parse_signature,
    patch_word_index,
    wrapper_signature,
)
from .models import CHAIN_ID_ROBINHOOD, TOKEN_ADDRESSES_4663, Fund, Leg, ProposalRecord
from .rules import RuleViolation, deny, is_address
from .signed_cli import cli_duration, proposal_name
from .store import validate_fund_name
from .units import format_units

# CLI 0.104.0 ``strategy propose`` / ``strategy clone`` option, not ``strategy init``.
MIN_CLI_FOR_GENERIC_CALL = "0.104.0"
TEMPLATE_KEY = "generic-call"
# Address book in that CLI (``STRATEGY_TEMPLATES().GENERIC_CALL`` on 4663). Not a live code read.
TEMPLATE_ADDRESS_BOOK = {CHAIN_ID_ROBINHOOD: "0xee67db9630fCB4F69C013928b2113C5489B39833"}

MAX_CALLS_PER_LEG = 16
MAX_SETTLE_PATCHES = 32
MAX_SWEEP_TOKENS = 16
MAX_LEG_CALLDATA = 16384
_PLAN_KEYS = ("executeCalls", "settleCalls", "settlePatches", "sweepTokens")
_APPROVE = "095ea7b3"
_TRANSFER = "a9059cbb"
_HEX_RE = re.compile(r"0x[0-9a-fA-F]*")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_CALL_KEYS = ("target", "data", "signature", "args", "value", "note")
_PATCH_KEYS = ("callIndex", "argIndex", "wordIndex", "token", "mulNum", "mulDen")
_ZERO = "0x" + "0" * 40

# Fleet guardians (sherwood-guardian) treat a non-core clone as UNAUDITED_TEMPLATE,
# a warning, and do not Approve it unattended. Abstain emits nothing. Execute then
# stays blocked and the vault stays locked until a guardian Approves or the proposal
# is cancelled. That is a guardian-fleet and protocol rule; preparing a plan does not lift it.
GUARDIAN_NOTE = (
    "Generic Call is an unaudited sherwood-strategies template, tier 2 at full notional, never certified. "
    "A non-core clone is a warning (UNAUDITED_TEMPLATE) for the guardian fleet: it does not Approve unattended. "
    "Abstain emits nothing, so execute stays blocked and the vault stays locked until a guardian Approves or "
    "the proposal is cancelled. The plugin does not bypass that gate."
)
SIMULATION_NOTE = (
    "This preview checks the plan's shape only. It is not simulate_batch and not execution acceptance. "
    "In CLI 0.104.0, simulate_batch models the governor's active-proposal guard only when the simulate API "
    "is given a proposal id; the eth_call fallback does not, so a simulation that never applied that state "
    "does not show the call would pass NotActiveProposalStrategy."
)
EXIT_NOTE = (
    "Settlement counts only the vault asset. Anything else left on the clone is a realised loss. "
    "A settle patch scale (mulNum/mulDen) is frozen when the plan is proposed: it is a quote-time floor, "
    "not a live price, and a raw-balance patch (both scales omitted or zero) sets no price floor. "
    "The CLI warns never to leave a patched sell's minimum at zero. This preview does not prove the "
    "calls return the asset."
)
# proposal_signer: host co-signs prepare/propose for a portfolio or raw calls from a signing link.
# There is no prepare route for this template, so a Generic Call draft could never ship there.
HOST_SIGNER_REFUSAL = (
    "Generic Call is proposed with `sherwood strategy propose generic-call` under proposal_signer: agent. "
    "This fund's proposal_signer is host, which co-signs prepare/propose for a portfolio or raw calls, and "
    "there is no prepare route for this template"
)


def require_agent_signer(proposal_signer: str) -> None:
    """A Generic Call draft only makes sense where the agent's own CLI wallet proposes it."""
    if proposal_signer != "agent":
        _fail("agent_signer_only", HOST_SIGNER_REFUSAL + ", so no draft was recorded and the topic stays free.")


QUOTE_FLOOR_NOTE = (
    "raw balance at settle, no price floor; frozen at propose, not a live quote; unsafe as a sell minimum"
)


def generic_call_legs(max_capital: int) -> list[Leg]:
    """The governor batch the CLI builds: one capped execute, one zero-cap settle.

    Inner targets live on the plan, not on these legs, so a portfolio weight check
    cannot treat the draft as a stock basket.
    """
    return [
        Leg(summary="Generic Call execute (asset approve + clone.execute)", phase="execute", kind="call", cap=str(max_capital)),
        Leg(summary="Generic Call settle (clone.settle)", phase="settle", kind="call", cap="0"),
    ]


def _fail(code: str, reason: str) -> NoReturn:
    raise RuleViolation(deny(code, reason))


_PLACEHOLDER_ASSET = "$asset"


def _token_ref(value: Any, where: str, fund: Fund, *, code: str = "bad_target") -> tuple[str, str]:
    """A call target, sweep token or patch token: (as written, resolved lowercase key).

    CLI ``placeholder`` + ``isAddress``: a 0x address, or ``$asset`` (the vault asset). ``$clone``
    and ``$vault`` resolve too, but the CLI refuses both as a target or a sweep token
    (``forbiddenLabel``), so they are refused here. The resolved key of ``$asset`` is the asset
    address when the plugin knows it, else the literal ``$asset``.
    """
    text = value.strip() if isinstance(value, str) else ""
    low = text.lower()
    if low == _PLACEHOLDER_ASSET:
        asset = _asset_address(fund)
        return text, (asset.lower() if asset else _PLACEHOLDER_ASSET)
    if low == "$clone":
        _fail(code, f"{where} is $clone, the clone itself; the clone may not call or sweep itself")
    if low == "$vault":
        _fail(code, f"{where} is $vault, this fund's vault; the clone may not call it or sweep it")
    if not _ADDRESS_RE.fullmatch(text):
        _fail(code, f"{where} must be a 0x address or $asset, not {text[:66]!r}")
    if low == _ZERO:
        _fail(code, f"{where} is the zero address")
    forbidden = _local_forbidden(text, fund)
    if forbidden:
        _fail(code, f"{where} is {forbidden}; the clone may not call it")
    return text, low


def _asset_address(fund: Fund) -> str | None:
    if fund.chain_id != CHAIN_ID_ROBINHOOD:
        return None
    return TOKEN_ADDRESSES_4663.get((fund.asset or "").upper())


def _calldata(value: Any, where: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _HEX_RE.fullmatch(text) or len(text) < 10 or len(text) % 2 != 0:
        _fail("bad_calldata", f"{where} data must be 0x hex with at least a 4-byte selector")
    return text


def _asset_keys(fund: Fund) -> set[str]:
    asset = _asset_address(fund)
    return {_PLACEHOLDER_ASSET} | ({asset.lower()} if asset else set())


def _check_approvals(compiled: bytes, target_key: str, where: str, fund: Fund, pull_amount: int) -> None:
    """The plugin's approval rules, on the compiled bytes (raw data and signature + args are the
    same call), including every call inside a wrapper ``callspec.approvals_in`` opens:

    - an operator approval switched on is refused (setApprovalForAll, DAI permit, authorizeOperator);
    - an approval of the vault asset may not exceed the plan's pull (when the pull is known):
      the clone never receives more of the asset than the pull;
    - any other token's approval is refused at or above half its type's range (2^255, 2^159 for
      Permit2), the bound ``units.is_unlimited`` uses: the plan does not say how much of that
      token the clone will hold, so no tighter bound is known.
    """
    try:
        found = approvals_in(compiled, target_key)
    except SpecError as exc:
        _fail("bad_calldata", f"{where}: {exc}")
    assets = _asset_keys(fund)
    for a in found:
        label = f"{a.path}{a.label}"
        if a.amount is None:
            if a.on:
                _fail(
                    "unlimited_approve",
                    f"{where} {label} turns on an operator approval over every token; "
                    "operator approvals are never allowed",
                )
            continue
        if a.amount >= a.unlimited_bound:
            bound = "2^255" if a.bits == 256 else f"2^{a.bits - 1}"
            _fail(
                "unlimited_approve",
                f"{where} grants an unlimited allowance ({label} of {a.amount}, at or above {bound}, which the "
                "plugin treats as unlimited); unlimited approvals are never allowed. Approve the exact amount, "
                "or $pullAmount",
            )
        if pull_amount > 0 and a.token in assets and a.amount > pull_amount:
            _fail(
                "approve_above_pull",
                f"{where} {label} approves {a.amount} base units of the vault asset, above this plan's pull of "
                f"{pull_amount}; the clone never holds more of the asset than the pull. Approve at most the "
                "pull ($pullAmount)",
            )


def _check_patch_into_approval(
    approving: list[Approval], *, where: str, call_index: int, word_index: int, token_key: str,
    mul_num: int, mul_den: int,
) -> None:
    """A settle patch into a call that approves writes the amount at settle time, past the checks
    on the amount written in the plan. So such a patch may write only that approval's amount word,
    only from the approved token's own balance, and never scaled up: the allowance is then at most
    what the clone holds of that token. An operator approval (a flag) or an approval inside a
    wrapper takes no patch at all."""
    if mul_num > mul_den:
        # balance * mulNum / mulDen written into an approving call could be any size.
        _fail(
            "unlimited_approve",
            f"{where} scales a balance up (mulNum {mul_num} > mulDen {mul_den}) into settle call {call_index}, "
            "which approves; a patch into an approving call may write at most the balance (mulNum <= mulDen)",
        )
    wrapped = [a for a in approving if a.path]
    if wrapped:
        _fail(
            "approve_patch",
            f"{where} writes into settle call {call_index}, which approves inside a wrapper "
            f"({wrapped[0].path}{wrapped[0].label}); a patch may not write into a wrapped approval",
        )
    grant = approving[0]
    if grant.amount is None:
        _fail(
            "approve_patch",
            f"{where} writes into settle call {call_index}, an operator approval ({grant.label}); "
            "a patch may not write into one",
        )
    if word_index != grant.word:
        _fail(
            "approve_patch",
            f"{where} writes word {word_index} of settle call {call_index} ({grant.label}); in an approving call "
            f"only the amount word ({grant.word}) may be patched",
        )
    if token_key != grant.token:
        _fail(
            "approve_patch",
            f"{where} writes the balance of {token_key} into the amount of settle call {call_index} "
            f"({grant.label} of {grant.token}); a patched approval amount must be the approved token's own balance",
        )


def _json_index(value: Any, where: str) -> int:
    """CLI ``Number.isInteger(x) && x >= 0``: a JSON integer, not a string, float, bool or null."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("bad_plan", f"{where} must be a JSON integer from 0 (not a string, float or null), got {value!r}"[:200])
    return value


def _scale(value: Any, where: str) -> int | str:
    """CLI ``scaleOf``: a JSON integer up to 2^53-1, or a decimal string up to 2^256-1. Absent is 0."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SAFE_INTEGER:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,78}", value.strip()) and int(value.strip()) < 2**256:
        return value.strip()
    _fail(
        "bad_plan",
        f"{where} must be a uint256 in raw units: a decimal string, or a JSON integer up to 2^53-1, got {value!r}"[:200],
    )


def _value_is_zero(call: dict[str, Any], where: str) -> None:
    """CLI ``compileCall`` value: absent, a safe JSON integer or a decimal string; the template needs 0."""
    if "value" not in call:
        return
    value = call["value"]
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SAFE_INTEGER:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,78}", value.strip()):
        number = int(value.strip())
    else:
        _fail("bad_call", f"{where} value must be a non-negative integer, and 0 for this template")
    if number != 0:
        _fail("bad_call", f"{where} value must be 0; the vault holds no native token for this call")


def _asset_call_ok(compiled: bytes) -> bool:
    """CLI validateGenericCallInit: on the vault asset only approve/transfer, with both words present.

    Judged on the selector the calldata carries, never on the ``signature`` label beside it.
    """
    return len(compiled) >= 68 and compiled[:4].hex() in (_APPROVE, _TRANSFER)


def _local_forbidden(target: str, fund: Fund) -> str | None:
    low = target.lower()
    if fund.vault and low == fund.vault.lower():
        return "this fund's vault"
    if fund.governor and low == fund.governor.lower():
        return "this fund's governor"
    return None


def _compile_call(call: Any, where: str, fund: Fund, binding: Binding) -> tuple[dict[str, Any], str, bytes, Function | None]:
    """One plan call -> (the entry to store, resolved target key, calldata, parsed signature or None).

    Mirrors CLI ``compileCall``: ``data`` xor ``args``; with both ``data`` and ``signature`` the data
    must decode as that signature; ``signature`` + ``args`` encodes; ``value`` is 0; ``note`` is text.
    """
    if not isinstance(call, dict):
        _fail("bad_plan", f"{where} must be an object")
    extra = [k for k in call if k not in _CALL_KEYS]
    if extra:
        _fail("bad_plan", f"{where} has unknown field(s): {', '.join(extra)}. Allowed: {', '.join(_CALL_KEYS)}")
    target, target_key = _token_ref(call.get("target"), f"{where} target", fund)
    _value_is_zero(call, where)
    note = call.get("note")
    if note is not None and not isinstance(note, str):
        _fail("bad_plan", f"{where} note must be text")
    signature = call.get("signature")
    if signature is not None and not isinstance(signature, str):
        _fail("bad_call", f"{where} signature must be text like approve(address,uint256)")
    fn: Function | None = None
    if signature and signature.strip():
        try:
            fn = parse_signature(signature)
        except SpecError as exc:
            _fail("bad_call", f"{where}: {exc}")
    data = call.get("data")
    has_data = data not in (None, "")
    stored: dict[str, Any] = {"target": target}
    if has_data:
        if "args" in call:
            _fail("bad_calldata", f"{where} must give data or args, not both")
        text = _calldata(data, where)
        compiled = bytes.fromhex(text[2:])
        if fn is not None:
            try:
                decode_check(fn, compiled)
            except SpecError as exc:
                _fail("bad_calldata", f"{where}: {exc}")
        stored["data"] = text
    elif fn is not None:
        args = call.get("args")
        try:
            compiled = encode_call(fn, args, binding, where)
        except SpecError as exc:
            _fail("bad_call", str(exc))
        if args is not None:
            stored["args"] = args
    else:
        _fail("bad_calldata", f"{where} needs 0x data, or a signature with args")
    if fn is not None and isinstance(signature, str):
        stored["signature"] = signature.strip()
    if note and note.strip():
        stored["note"] = note.strip()[:300]
    _check_approvals(compiled, target_key, where, fund, binding.pull_amount)
    if target_key in (_PLACEHOLDER_ASSET, (_asset_address(fund) or "").lower()) and not _asset_call_ok(compiled):
        _fail(
            "bad_call",
            f"{where} calls the vault asset with selector 0x{compiled[:4].hex()}; "
            "on the asset only approve(address,uint256) and transfer(address,uint256) are allowed",
        )
    return stored, target_key, compiled, fn


def validate_generic_call_plan(raw: Any, fund: Fund, *, pull_amount: int = 0) -> dict[str, Any]:
    """Return the plan to store and hand to the CLI, or raise RuleViolation.

    Mirrors what ``@sherwoodagent/cli@0.104.0`` checks before it signs, so a plan accepted here
    is one the CLI compiles: ``parseGenericCallSpec``, ``compileGenericCallSpec`` and the offline
    part of ``validateGenericCallInit`` (dist/chunk-ADWPGCQW.js). Each call is compiled to calldata
    the way the CLI compiles it (``callspec``), so "no unlimited approvals" holds for ``signature``
    + ``args`` as well as raw ``data``. ``pull_amount`` (base units, the draft's max capital) is
    what ``$pullAmount`` stands for. The returned plan is a normalized copy: only the keys the CLI
    reads, integers as JSON integers, no ``null`` where the CLI would read a value.

    No chain read: the CLI also refuses the factory, registries, sWOOD and the ledger as targets
    once it reads them from the vault, a target with no code, and a pull above the tier-2 ceiling.
    The plugin adds its own rules: an exit (a settle leg or the asset swept), no duplicate sweep
    tokens, no unlimited approvals.
    """
    if not isinstance(raw, dict):
        _fail("bad_plan", "calls must be an object with executeCalls, settleCalls, settlePatches and sweepTokens")
    unknown = [k for k in raw if k not in _PLAN_KEYS]
    if unknown:
        _fail("bad_plan", f"unknown generic-call field(s): {', '.join(unknown)}. Allowed: {', '.join(_PLAN_KEYS)}")
    lists: dict[str, list[Any]] = {}
    for key in _PLAN_KEYS:
        value = raw.get(key, [])
        if not isinstance(value, list):
            _fail("bad_plan", f"{key} must be an array")
        lists[key] = value
    asset = _asset_address(fund)
    binding = Binding(
        clone=_ZERO,  # the CLI's first compile binds the clone to zero too (planGenericCallClone)
        vault=fund.vault if is_address(fund.vault) else _ZERO,
        asset=asset or _ZERO,
        pull_amount=max(0, int(pull_amount or 0)),
    )
    plan: dict[str, list[Any]] = {key: [] for key in _PLAN_KEYS}
    targets: set[str] = set()
    settle_compiled: list[tuple[bytes, Function | None]] = []
    settle_targets: list[str] = []
    for leg_name, key in (("execute", "executeCalls"), ("settle", "settleCalls")):
        calls = lists[key]
        if len(calls) > MAX_CALLS_PER_LEG:
            _fail("bad_plan", f"the {leg_name} leg has {len(calls)} calls; the template takes at most {MAX_CALLS_PER_LEG}")
        total = 0
        for i, call in enumerate(calls):
            stored, target_key, compiled, fn = _compile_call(call, f"{leg_name} call {i}", fund, binding)
            plan[key].append(stored)
            targets.add(target_key)
            total += len(compiled)
            if leg_name == "settle":
                settle_compiled.append((compiled, fn))
                settle_targets.append(target_key)
        if total > MAX_LEG_CALLDATA:
            _fail("bad_calldata", f"the {leg_name} leg carries {total} bytes of calldata; the template takes at most {MAX_LEG_CALLDATA}")
    if len(lists["sweepTokens"]) > MAX_SWEEP_TOKENS:
        _fail("bad_plan", f"{len(lists['sweepTokens'])} sweep tokens; the template takes at most {MAX_SWEEP_TOKENS}")
    sweeps: set[str] = set()
    for i, token in enumerate(lists["sweepTokens"]):
        text, key = _token_ref(token, f"sweep token {i}", fund)
        if key in sweeps:
            _fail("bad_plan", f"sweep token {text} is listed twice")
        sweeps.add(key)
        plan["sweepTokens"].append(text)
    if len(lists["settlePatches"]) > MAX_SETTLE_PATCHES:
        _fail("bad_plan", f"{len(lists['settlePatches'])} settle patches; the template takes at most {MAX_SETTLE_PATCHES}")
    asset_key = asset.lower() if asset else None
    patched: set[tuple[int, int]] = set()
    for i, patch in enumerate(lists["settlePatches"]):
        where = f"settle patch {i}"
        if not isinstance(patch, dict):
            _fail("bad_plan", f"{where} must be an object")
        extra = [k for k in patch if k not in _PATCH_KEYS]
        if extra:
            _fail("bad_plan", f"{where} has unknown field(s): {', '.join(extra)}. Allowed: {', '.join(_PATCH_KEYS)}")
        token, token_key = _token_ref(patch.get("token"), f"{where} token", fund, code="bad_plan")
        scales: dict[str, Any] = {}
        for scale in ("mulNum", "mulDen"):
            if scale in patch:
                scales[scale] = _scale(patch[scale], f"{where} {scale}")
        if "callIndex" not in patch:
            _fail("bad_plan", f"{where} callIndex is missing")
        call_index = _json_index(patch["callIndex"], f"{where} callIndex")
        if call_index >= len(settle_compiled):
            _fail("bad_plan", f"{where} names settle call {call_index}, but the settle leg has {len(settle_compiled)} call(s)")
        # The CLI counts a key that is present, even as null, as given.
        has_arg, has_word = "argIndex" in patch, "wordIndex" in patch
        if has_arg == has_word:
            _fail("bad_plan", f"{where} needs exactly one of argIndex (with the call's signature) or wordIndex")
        data, fn = settle_compiled[call_index]
        if has_word:
            word_index = _json_index(patch["wordIndex"], f"{where} wordIndex")
            position: dict[str, int] = {"wordIndex": word_index}
        else:
            arg_index = _json_index(patch["argIndex"], f"{where} argIndex")
            if fn is None:
                _fail(
                    "bad_plan",
                    f"{where}: settle call {call_index} is raw data with no signature, so argument {arg_index} "
                    "cannot be located; add its signature or pass a raw wordIndex",
                )
            try:
                word_index = patch_word_index(fn, arg_index)
            except SpecError as exc:
                _fail("bad_plan", f"{where}: settle call {call_index} ({fn.name}): {exc}")
            position = {"argIndex": arg_index}
        words = 0 if len(data) < 36 else (len(data) - 4) // 32
        if word_index >= words:
            _fail("bad_plan", f"{where} writes word {word_index} of settle call {call_index}, which has {words} argument word(s)")
        if token_key not in (asset_key, _PLACEHOLDER_ASSET) and token_key not in targets and token_key not in sweeps:
            _fail(
                "bad_target",
                f"{where} reads {token}, which is neither the vault asset ($asset), a call target nor a sweep token",
            )
        guarded = address_words(data)
        if word_index < guarded:
            _fail(
                "bad_plan",
                f"{where} writes word {word_index} of settle call {call_index} (selector 0x{data[:4].hex()}), an "
                f"address word; words below {guarded} cannot be patched",
            )
        mul_num = int(scales.get("mulNum", 0))
        mul_den = int(scales.get("mulDen", 0))
        if (mul_num == 0) != (mul_den == 0):
            _fail("bad_plan", f"{where} must set mulNum and mulDen both, or neither (a half-set scale is rejected)")
        wrapper = wrapper_signature(data)
        if wrapper:
            # A patch into a wrapper can rewrite an inner call (its selector, an aggregate row's
            # target) at settle time, after every check here ran on the calls as written.
            _fail(
                "approve_patch",
                f"{where} writes into settle call {call_index}, a {wrapper} wrapper; a patch may not write "
                "into a wrapper call (it could turn an inner call into an approval at settle)",
            )
        approving = approvals_in(data, settle_targets[call_index])
        if approving:
            _check_patch_into_approval(
                approving, where=where, call_index=call_index, word_index=word_index,
                token_key=token_key, mul_num=mul_num, mul_den=mul_den,
            )
        if (call_index, word_index) in patched:
            _fail("bad_plan", f"{where} writes word {word_index} of settle call {call_index}, which another patch already writes")
        patched.add((call_index, word_index))
        plan["settlePatches"].append({"callIndex": call_index, **position, "token": token, **scales})
    asset_swept = bool(sweeps & {asset_key, _PLACEHOLDER_ASSET} - {None})
    if not plan["settleCalls"] and not asset_swept:
        _fail(
            "no_exit",
            "a generic-call plan needs a settle leg, or the vault asset listed in sweepTokens, "
            "so settlement has a path back to the vault asset. This does not prove the calls return it",
        )
    if not plan["executeCalls"] and not plan["settleCalls"]:
        _fail("no_legs", "a generic-call plan needs at least one call")
    return plan


def _selector_of_call(call: dict[str, Any]) -> str:
    data = str(call.get("data") or "")
    if _HEX_RE.fullmatch(data) and len(data) >= 10:
        return "0x" + data[2:10].lower()
    try:
        return "0x" + parse_signature(call.get("signature")).selector
    except SpecError:
        return ""


def _call_preview(call: dict[str, Any], index: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "index": index,
        "target": call.get("target"),
        "value": "0",
        "signature": str(call.get("signature") or ""),
        "selector": _selector_of_call(call),
    }
    for key in ("data", "args", "note"):
        if key in call:
            row[key] = call[key]
    return row


def _patch_preview(patch: dict[str, Any]) -> dict[str, Any]:
    mul_num = int(patch.get("mulNum") or 0)
    mul_den = int(patch.get("mulDen") or 0)
    row = {
        "callIndex": patch.get("callIndex"),
        "argIndex": patch.get("argIndex"),
        "wordIndex": patch.get("wordIndex"),
        "token": patch.get("token"),
        "mulNum": str(mul_num),
        "mulDen": str(mul_den),
        "quote_time_floor": mul_den != 0,
    }
    if mul_den == 0:
        row["floor"] = QUOTE_FLOOR_NOTE
    return row


def generic_call_availability(discovered: dict[str, Any] | None, chain_id: int) -> str:
    """"" when this host's strategy list prints generic-call as available for this chain,
    "missing: ..." when it was read and does not, else a warning (list not read, or another network)."""
    from .scope import discovery_applies

    if discovered is None:
        return ""
    if not discovered.get("ok"):
        why = str(discovered.get("error") or "not read")
        return (
            f"`sherwood strategy list` could not be read ({why}), so whether this host's CLI can propose "
            f"generic-call (it needs {MIN_CLI_FOR_GENERIC_CALL} or later) is unknown."
        )
    if not discovery_applies(discovered, chain_id):
        return (
            f"`sherwood strategy list` printed network {discovered.get('network') or '(none)'}, not this fund's chain "
            f"{chain_id}, so it says nothing about Generic Call here."
        )
    available = {row.get("key") for row in discovered.get("available") or []}
    if TEMPLATE_KEY in available:
        return ""
    network = discovered.get("network") or "this network"
    unavailable = {row.get("key") for row in discovered.get("unavailable") or []}
    where = (
        f"lists generic-call under \"Not available on {network}\""
        if TEMPLATE_KEY in unavailable
        else "does not list generic-call at all"
    )
    return (
        f"missing:cli: `sherwood strategy list` on this host {where}. Proposing it needs the Sherwood CLI "
        f"{MIN_CLI_FOR_GENERIC_CALL} or later, where the template is deployed on {network}; upgrade the CLI "
        "that runs the propose command, then check the list again. Nothing was signed"
    )


def preview_generic_call(
    fund: Fund,
    plan: dict[str, Any],
    *,
    max_capital: int,
    max_drawdown_bps: int,
    strategy_duration: int,
    discovered: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Unsigned preview. ``ready`` means the command can be built, not that it may execute.

    ``discovered`` is this host's ``sherwood strategy list`` (``scope.discover_strategy_templates``).
    When it was read for this fund's chain and does not print generic-call as available, the
    preview is not ready and says what is missing: the CLI that runs the propose command cannot
    propose this template (below 0.104.0, or the template is absent there).
    """
    template = TEMPLATE_ADDRESS_BOOK.get(fund.chain_id)
    asset_address = TOKEN_ADDRESSES_4663.get((fund.asset or "").upper()) if fund.chain_id == CHAIN_ID_ROBINHOOD else None
    missing: list[str] = []
    if not is_address(fund.vault):
        missing.append("vault: the fund has no vault address yet")
    if strategy_duration <= 0:
        missing.append("strategyDuration: seconds the position stays open")
    if template is None:
        missing.append(
            f"template: Generic Call is in the CLI {MIN_CLI_FOR_GENERIC_CALL} address book on chain "
            f"{CHAIN_ID_ROBINHOOD} only; this fund's chain is {fund.chain_id}. "
            "`sherwood strategy list` is the live check. Nothing was signed"
        )
    warnings = [EXIT_NOTE]
    availability = generic_call_availability(discovered, fund.chain_id)
    if availability.startswith("missing:"):
        missing.append(availability[len("missing:"):])
    elif availability:
        warnings.append(availability)
    if any(not p.get("quote_time_floor") for p in (_patch_preview(p) for p in plan.get("settlePatches") or [])):
        warnings.append("At least one settle patch has no price floor. " + QUOTE_FLOOR_NOTE)
    if not plan.get("executeCalls"):
        warnings.append("The execute leg has no calls, so a pulled asset would sit on the clone until settle.")
    return {
        "route": TEMPLATE_KEY,
        "url": "",
        "ready": not missing,
        "signed": False,
        "execution_acceptance": False,
        "missing": missing,
        "body": {
            "chain_id": fund.chain_id,
            "chain": "Robinhood Chain" if fund.chain_id == CHAIN_ID_ROBINHOOD else f"chain {fund.chain_id}",
            "robinhood_brokerage": False,
            "template_key": TEMPLATE_KEY,
            "template_name": "Generic Call",
            "template_address": template,
            "template_source": f"@sherwoodagent/cli@{MIN_CLI_FOR_GENERIC_CALL} address book; not a live chain read",
            "asset": fund.asset,
            "asset_address": asset_address,
            "pull": {
                "human": format_units(max_capital, fund.asset_decimals),
                "base_units": str(max_capital),
            },
            "limits": {
                "max_capital_base_units": str(max_capital),
                "max_drawdown_bps": max_drawdown_bps,
                "strategy_duration_seconds": strategy_duration,
                "max_calls_per_leg": MAX_CALLS_PER_LEG,
                "max_settle_patches": MAX_SETTLE_PATCHES,
                "max_sweep_tokens": MAX_SWEEP_TOKENS,
                "max_leg_calldata_bytes": MAX_LEG_CALLDATA,
                "native_value": "0",
                "tier": "2 at full notional; the pull is the clone.execute() cap; never certified",
            },
            "execute_calls": [_call_preview(c, i) for i, c in enumerate(plan.get("executeCalls") or [])],
            "settle_calls": [_call_preview(c, i) for i, c in enumerate(plan.get("settleCalls") or [])],
            "sweep_tokens": list(plan.get("sweepTokens") or []),
            "settle_patches": [_patch_preview(p) for p in plan.get("settlePatches") or []],
            "exit": EXIT_NOTE,
            "guardian": GUARDIAN_NOTE,
            "simulation": SIMULATION_NOTE,
            "protocol_not_resolved_here": (
                "The CLI also refuses the factory, registries, sWOOD and the ledger once it reads them from the vault, "
                "and it refuses a target with no code. This preview did not make those reads."
            ),
            "warnings": warnings,
        },
    }


def generic_call_plan_path(backend: Any, fund_name: str, record: ProposalRecord) -> Path:
    """Where the plan goes, next to fund state: an absolute path (a relative ``fund_state_dir``
    would make ``--calls`` resolve against the one-shot's working directory). Writes nothing."""
    root = getattr(backend, "root", None)
    if root is None:
        _fail("draft_not_ready", "this state backend has nowhere to write the generic-call plan, so no command is handed out")
    name = validate_fund_name(fund_name)
    if not re.fullmatch(r"p-\d+", record.id):
        _fail("draft_not_ready", f"refusing to write a generic-call plan for id {record.id!r}")
    return (Path(root) / name / "generic-call" / f"{record.id}.json").resolve()


def write_generic_call_plan(backend: Any, fund_name: str, record: ProposalRecord) -> str:
    """Write the plan next to fund state (mode 0600). Returns the absolute path ``--calls`` takes."""
    path = generic_call_plan_path(backend, fund_name, record)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _fail("draft_not_ready", f"could not write the generic-call plan ({exc.__class__.__name__})")
    payload = json.dumps({k: record.generic_call.get(k, []) for k in _PLAN_KEYS}, indent=2)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        _fail("draft_not_ready", f"could not write the generic-call plan ({exc.__class__.__name__})")
    return str(path)


def propose_generic_call_command(fund: Fund, record: ProposalRecord, plan_path: str) -> str:
    """``sherwood strategy propose generic-call ...``. Raises RuleViolation when it must not be handed out."""
    if not is_address(fund.vault):
        _fail("no_vault", "the fund has no vault address yet")
    if fund.chain_id not in TEMPLATE_ADDRESS_BOOK:
        _fail(
            "draft_not_ready",
            f"Generic Call is not in the CLI {MIN_CLI_FOR_GENERIC_CALL} address book for chain {fund.chain_id}",
        )
    if not isinstance(record.strategy_duration, int) or record.strategy_duration <= 0:
        _fail("draft_not_ready", f"{record.id} has no strategy duration yet")
    try:
        capital = int(record.max_capital or 0)
    except (TypeError, ValueError):
        capital = 0
    if capital <= 0:
        _fail("draft_not_ready", f"{record.id} has no max capital yet")
    drawdown = record.max_drawdown_bps
    if isinstance(drawdown, bool) or not isinstance(drawdown, int) or not 0 <= drawdown <= 10_000:
        _fail("draft_not_ready", f"{record.id} has no valid max drawdown")
    if not plan_path or plan_path.startswith("-"):
        _fail("draft_not_ready", "the generic-call plan path is missing")
    try:
        duration = cli_duration(record.strategy_duration)
    except ValueError as exc:
        _fail("draft_not_ready", str(exc))
    args = [
        "sherwood", "strategy", "propose", TEMPLATE_KEY,
        "--vault", fund.vault,
        "--calls", plan_path,
        "--amount", format_units(capital, fund.asset_decimals),
        "--duration", duration,
        "--max-drawdown-bps", str(drawdown),
        "--name=" + " ".join(proposal_name(fund, record).split()),
        "--description=" + " ".join(record.thesis.split()),
    ]
    return shlex.join(args)
