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
from typing import Any, NoReturn

from .callspec import Binding, SpecError, encode_call, parse_signature, unlimited_grant
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
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]*$")
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


def _address(value: Any, where: str) -> str:
    text = str(value or "").strip()
    if not is_address(text):
        _fail("bad_target", f"{where} must be a 0x address, not {text[:66]!r}")
    if text.lower() == _ZERO:
        _fail("bad_target", f"{where} is the zero address")
    return text


def _calldata(value: Any, where: str) -> str:
    text = str(value or "").strip()
    if not _HEX_RE.match(text) or len(text) < 10 or len(text) % 2 != 0:
        _fail("bad_calldata", f"{where} data must be 0x hex with at least a 4-byte selector")
    return text


def _no_unlimited_grant(compiled: bytes, where: str) -> None:
    """The plugin's rule, on the compiled bytes: raw data and signature + args are the same call."""
    grant = unlimited_grant(compiled)
    if grant:
        _fail(
            "unlimited_approve",
            f"{where} grants an unlimited allowance ({grant}); unlimited approvals are never allowed. "
            "Approve the exact amount, or $pullAmount",
        )


def _uint(value: Any, where: str, *, allow_missing: bool = False) -> int | None:
    if value in (None, ""):
        if allow_missing:
            return None
        _fail("bad_plan", f"{where} is missing")
    if isinstance(value, bool) or isinstance(value, float):
        _fail("bad_plan", f"{where} must be an integer, not a float or bool")
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,78}", text):
        _fail("bad_plan", f"{where} must be a non-negative integer")
    return int(text)


def _value_is_zero(value: Any, where: str) -> None:
    if value in (None, "", 0, "0", "0x0", "0x"):
        return
    _fail("bad_call", f"{where} value must be 0; the vault holds no native token for this call")


def _selector(data: str) -> str:
    return data.lower()[2:10]


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


def validate_generic_call_plan(raw: Any, fund: Fund, *, pull_amount: int = 0) -> dict[str, Any]:
    """Return the plan to store and hand to the CLI, or raise RuleViolation.

    Structural only: addresses, calldata, caps the template documents, and an
    exit leg. Each call is compiled to calldata the way the CLI compiles it
    (``callspec``), so a rule like "no unlimited approvals" holds for
    ``signature`` + ``args`` as well as raw ``data``. ``pull_amount`` (base
    units, the draft's max capital) is what ``$pullAmount`` stands for. No
    chain read, so a target with no code is not rejected here; the CLI refuses
    that at propose.
    """
    if not isinstance(raw, dict):
        _fail("bad_plan", "calls must be an object with executeCalls, settleCalls, settlePatches and sweepTokens")
    unknown = [k for k in raw if k not in _PLAN_KEYS]
    if unknown:
        _fail("bad_plan", f"unknown generic-call field(s): {', '.join(unknown)}. Allowed: {', '.join(_PLAN_KEYS)}")
    plan: dict[str, list[Any]] = {}
    for key in _PLAN_KEYS:
        value = raw.get(key, [])
        if not isinstance(value, list):
            _fail("bad_plan", f"{key} must be an array")
        plan[key] = value
    asset = TOKEN_ADDRESSES_4663.get((fund.asset or "").upper()) if fund.chain_id == CHAIN_ID_ROBINHOOD else None
    binding = Binding(
        clone=_ZERO,  # the CLI's first compile binds the clone to zero too (planGenericCallClone)
        vault=fund.vault if is_address(fund.vault) else _ZERO,
        asset=asset or _ZERO,
        pull_amount=max(0, int(pull_amount or 0)),
    )
    for leg_name, calls in (("execute", plan["executeCalls"]), ("settle", plan["settleCalls"])):
        if len(calls) > MAX_CALLS_PER_LEG:
            _fail("bad_plan", f"the {leg_name} leg has {len(calls)} calls; the template takes at most {MAX_CALLS_PER_LEG}")
        total = 0
        for i, call in enumerate(calls):
            where = f"{leg_name} call {i}"
            if not isinstance(call, dict):
                _fail("bad_plan", f"{where} must be an object")
            extra = [k for k in call if k not in ("target", "data", "signature", "args", "value")]
            if extra:
                _fail("bad_plan", f"{where} has unknown field(s): {', '.join(extra)}")
            target = _address(call.get("target"), where)
            forbidden = _local_forbidden(target, fund)
            if forbidden:
                _fail("bad_target", f"{where} targets {forbidden}; the clone may not call it")
            _value_is_zero(call.get("value"), where)
            data = call.get("data")
            signature = str(call.get("signature") or "")
            has_data = data not in (None, "")
            has_args = "args" in call
            if has_data and has_args:
                _fail("bad_calldata", f"{where} must give data or args, not both")
            if has_data:
                compiled = bytes.fromhex(_calldata(data, where)[2:])
            elif signature.strip():
                if has_args and call.get("args") is not None and not isinstance(call.get("args"), list):
                    _fail("bad_plan", f"{where} args must be an array")
                try:
                    compiled = encode_call(parse_signature(signature), call.get("args"), binding, where)
                except SpecError as exc:
                    _fail("bad_call", str(exc) if str(exc).startswith(where) else f"{where}: {exc}")
            else:
                _fail("bad_calldata", f"{where} needs 0x calldata or a signature")
            total += len(compiled)
            _no_unlimited_grant(compiled, where)
            if asset and target.lower() == asset.lower() and not _asset_call_ok(compiled):
                _fail(
                    "bad_call",
                    f"{where} calls the vault asset with selector 0x{compiled[:4].hex()}; "
                    "on the asset only approve(address,uint256) and transfer(address,uint256) are allowed",
                )
        if total > MAX_LEG_CALLDATA:
            _fail("bad_calldata", f"the {leg_name} leg carries {total} bytes of calldata; the template takes at most {MAX_LEG_CALLDATA}")
    if len(plan["sweepTokens"]) > MAX_SWEEP_TOKENS:
        _fail("bad_plan", f"{len(plan['sweepTokens'])} sweep tokens; the template takes at most {MAX_SWEEP_TOKENS}")
    sweeps: list[str] = []
    seen_sweep: set[str] = set()
    for i, token in enumerate(plan["sweepTokens"]):
        addr = _address(token, f"sweep token {i}")
        forbidden = _local_forbidden(addr, fund)
        if forbidden:
            _fail("bad_target", f"sweep token {i} is {forbidden}")
        if addr.lower() in seen_sweep:
            _fail("bad_plan", f"sweep token {addr} is listed twice")
        seen_sweep.add(addr.lower())
        sweeps.append(addr)
    plan["sweepTokens"] = sweeps
    if len(plan["settlePatches"]) > MAX_SETTLE_PATCHES:
        _fail("bad_plan", f"{len(plan['settlePatches'])} settle patches; the template takes at most {MAX_SETTLE_PATCHES}")
    patched: set[tuple[int, int]] = set()
    for i, patch in enumerate(plan["settlePatches"]):
        where = f"settle patch {i}"
        if not isinstance(patch, dict):
            _fail("bad_plan", f"{where} must be an object")
        extra = [k for k in patch if k not in ("callIndex", "argIndex", "wordIndex", "token", "mulNum", "mulDen")]
        if extra:
            _fail("bad_plan", f"{where} has unknown field(s): {', '.join(extra)}")
        token = _address(patch.get("token"), where)
        call_index = _uint(patch.get("callIndex"), f"{where} callIndex")
        assert call_index is not None
        if call_index >= len(plan["settleCalls"]):
            _fail("bad_plan", f"{where} names settle call {call_index}, but the settle leg has {len(plan['settleCalls'])} call(s)")
        has_arg = "argIndex" in patch and patch.get("argIndex") not in (None, "")
        has_word = "wordIndex" in patch and patch.get("wordIndex") not in (None, "")
        if has_arg == has_word:
            _fail("bad_plan", f"{where} needs exactly one of argIndex or wordIndex")
        word_index = _uint(patch.get("wordIndex"), f"{where} wordIndex") if has_word else None
        if has_arg:
            arg_index = _uint(patch.get("argIndex"), f"{where} argIndex")
            assert arg_index is not None
            if arg_index < 0:
                _fail("bad_plan", f"{where} argIndex must be >= 0")
        mul_num = _uint(patch.get("mulNum"), f"{where} mulNum", allow_missing=True)
        mul_den = _uint(patch.get("mulDen"), f"{where} mulDen", allow_missing=True)
        if (mul_num in (None, 0)) != (mul_den in (None, 0)):
            _fail("bad_plan", f"{where} must set mulNum and mulDen both, or neither (a half-set scale is rejected)")
        call = plan["settleCalls"][call_index]
        data = str(call.get("data") or "")
        if word_index is not None and _HEX_RE.match(data) and len(data) >= 10 and len(data) % 2 == 0:
            words = 0 if (len(data) - 2) // 2 < 36 else ((len(data) - 2) // 2 - 4) // 32
            if word_index >= words:
                _fail("bad_plan", f"{where} writes word {word_index} of a call with {words} argument word(s)")
            selector = _selector(data)
            if word_index == 0 and selector in (_APPROVE, _TRANSFER):
                _fail("bad_plan", f"{where} would overwrite an approve/transfer address word")
        if word_index is not None:
            key = (call_index, word_index)
            if key in patched:
                _fail("bad_plan", f"{where} writes word {word_index} of settle call {call_index}, which another patch already writes")
            patched.add(key)
        if asset and token.lower() != asset.lower():
            targets = {str(c.get("target") or "").lower() for c in plan["executeCalls"] + plan["settleCalls"] if isinstance(c, dict)}
            if token.lower() not in targets and token.lower() not in seen_sweep:
                _fail("bad_target", f"{where} reads {token}, which is not the vault asset, a call target, or a sweep token")
    asset_swept = bool(asset) and any(t.lower() == asset.lower() for t in sweeps)
    if not plan["settleCalls"] and not asset_swept:
        _fail(
            "no_exit",
            "a generic-call plan needs a settle leg, or the vault asset listed in sweepTokens, "
            "so settlement has a path back to the vault asset. This does not prove the calls return it",
        )
    if not plan["executeCalls"] and not plan["settleCalls"]:
        _fail("no_legs", "a generic-call plan needs at least one call")
    return plan


def _call_preview(call: dict[str, Any], index: int) -> dict[str, Any]:
    data = str(call.get("data") or "")
    row: dict[str, Any] = {
        "index": index,
        "target": call.get("target"),
        "value": "0",
        "signature": str(call.get("signature") or ""),
    }
    if data:
        row["selector"] = "0x" + _selector(data) if _HEX_RE.match(data) and len(data) >= 10 else ""
        row["data"] = data
    return row


def _patch_preview(patch: dict[str, Any]) -> dict[str, Any]:
    mul_den = _uint(patch.get("mulDen"), "mulDen", allow_missing=True) or 0
    mul_num = _uint(patch.get("mulNum"), "mulNum", allow_missing=True) or 0
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


def preview_generic_call(
    fund: Fund,
    plan: dict[str, Any],
    *,
    max_capital: int,
    max_drawdown_bps: int,
    strategy_duration: int,
) -> dict[str, Any]:
    """Unsigned preview. ``ready`` means the command can be built, not that it may execute."""
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


def write_generic_call_plan(backend: Any, fund_name: str, record: ProposalRecord) -> str:
    """Write the plan next to fund state. Returns the absolute path the CLI ``--calls`` flag takes."""
    root = getattr(backend, "root", None)
    if root is None:
        _fail("draft_not_ready", "this state backend has nowhere to write the generic-call plan, so no command is handed out")
    name = validate_fund_name(fund_name)
    if not re.fullmatch(r"p-\d+", record.id):
        _fail("draft_not_ready", f"refusing to write a generic-call plan for id {record.id!r}")
    directory = root / name / "generic-call"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.id}.json"
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
    import shlex

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
