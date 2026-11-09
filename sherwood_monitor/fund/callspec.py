"""Generic Call plan calls, compiled to calldata the way the Sherwood CLI 0.104.0 compiles them.

A Python mirror of ``@sherwoodagent/cli@0.104.0`` ``dist/chunk-ADWPGCQW.js``: ``placeholder``,
``coerce``, ``parseCallSignature`` and ``compileCall`` (the ``--calls <plan.json>`` compiler), with
the signature grammar of abitype 1.x ``parseAbiParameter`` / ``splitParameters`` (what viem's
``parseAbiItem`` runs) and standard ABI encoding. A call this module compiles is one the CLI
compiles to the same calldata. Where the two could differ in an edge case this module is the
stricter one: printable-ASCII signatures, whole-byte hex, integer JSON numbers only.

The plugin needs the bytes because a rule the CLI does not enforce (no unlimited approvals) has
to hold for every form of a call: ``signature`` + ``args`` compiles to the same calldata as raw
``data``, so both are checked on the compiled bytes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1  # JavaScript Number.MAX_SAFE_INTEGER: the CLI's bound on JSON numbers

# abitype 1.x runtime/signatures.ts + runtime/utils.ts (the function signature and parameter grammar).
_FUNCTION_RE = re.compile(
    r"function (?P<name>[a-zA-Z$_][a-zA-Z0-9$_]*)\((?P<parameters>.*?)\)"
    r"(?: (?P<scope>external|public))?(?: (?P<mutability>pure|view|nonpayable|payable))?"
    r"(?: returns\s?\((?P<returns>.*?)\))?"
)
_PARAM_RE = re.compile(
    r"(?P<type>[a-zA-Z$_][a-zA-Z0-9$_]*(?:\spayable)?)(?P<array>(?:\[\d*?\])+?)?"
    r"(?:\s(?P<modifier>calldata|indexed|memory|storage))?(?:\s(?P<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?"
)
_TUPLE_PARAM_RE = re.compile(
    r"\((?P<type>.+?)\)(?P<array>(?:\[\d*?\])+?)?"
    r"(?:\s(?P<modifier>calldata|indexed|memory|storage))?(?:\s(?P<name>[a-zA-Z$_][a-zA-Z0-9$_]*))?"
)
_IS_TUPLE_RE = re.compile(r"\(.+?\).*?")
_BYTES_TYPE_RE = re.compile(r"bytes(?:[1-9]|1[0-9]|2[0-9]|3[0-2])?")
_INT_TYPE_RE = re.compile(
    r"u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|"
    r"208|216|224|232|240|248|256)?"
)
_PROTECTED = frozenset(
    "after alias anonymous apply auto byte calldata case catch constant copyof default defined error event "
    "external false final function immutable implements in indexed inline internal let mapping match memory "
    "mutable null of override partial private promise public pure reference relocatable return returns sizeof "
    "static storage struct super supports switch this true try typedef typeof var view virtual".split()
)
_FUNCTION_MODIFIERS = frozenset({"calldata", "memory", "storage"})
# CLI ``coerce``: the outer array dimension is the last bracket pair (greedy ``(.*)``).
_ARRAY_RE = re.compile(r"(.*)\[([0-9]*)\]")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]*")
_DECIMAL_RE = re.compile(r"-?[0-9]{1,78}")


class SpecError(ValueError):
    """A call the CLI would refuse to compile. The message names the call and the reason."""


def keccak256(data: bytes) -> bytes:
    """Ethereum keccak-256 (eth-utils, else pycryptodome: both are the plugin's declared deps)."""
    try:
        from eth_utils import keccak  # type: ignore[import-not-found]

        return bytes(keccak(data))
    except ImportError:  # pragma: no cover - eth-utils is a declared dependency
        from Crypto.Hash import keccak as _k  # type: ignore[import-not-found]

        return _k.new(digest_bits=256, data=data).digest()


def selector_of(signature: str) -> str:
    """4-byte selector (8 hex chars, no 0x) of a canonical ``name(type,...)`` signature."""
    return keccak256(signature.encode("ascii"))[:4].hex()


@dataclass(frozen=True)
class Param:
    type: str  # canonical: "uint256", "address[]", "tuple[2]", ...
    name: str = ""
    components: tuple["Param", ...] = ()


@dataclass(frozen=True)
class Function:
    name: str
    inputs: tuple[Param, ...]

    @property
    def canonical(self) -> str:
        return f"{self.name}({','.join(canonical_type(p) for p in self.inputs)})"

    @property
    def selector(self) -> str:
        return selector_of(self.canonical)


@dataclass(frozen=True)
class Binding:
    """What the CLI substitutes for ``$clone``, ``$vault``, ``$asset`` and ``$pullAmount``."""

    clone: str
    vault: str
    asset: str
    pull_amount: int


def canonical_type(p: Param) -> str:
    if p.type.startswith("tuple"):
        return "(" + ",".join(canonical_type(c) for c in p.components) + ")" + p.type[len("tuple"):]
    return p.type


# -- signature ---------------------------------------------------------------------------------------
def _split_parameters(params: str) -> list[str]:
    """abitype ``splitParameters``: split on depth-0 commas, trim each part, drop an all-space tail."""
    result: list[str] = []
    current = ""
    depth = 0
    i = 0
    while params[i:].strip():
        char = params[i]
        if char == "," and depth == 0:
            result.append(current.strip())
            current = ""
        else:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            current += char
        i += 1
    if current == "":
        return result
    if depth != 0:
        raise SpecError("unbalanced parentheses in the signature")
    result.append(current.strip())
    return result


def _is_keyword(name: str) -> bool:
    return (
        name in ("address", "bool", "function", "string", "tuple")
        or bool(_BYTES_TYPE_RE.fullmatch(name))
        or bool(_INT_TYPE_RE.fullmatch(name))
        or name in _PROTECTED
    )


def _is_solidity_type(name: str) -> bool:
    return (
        name in ("address", "bool", "function", "string")
        or bool(_BYTES_TYPE_RE.fullmatch(name))
        or bool(_INT_TYPE_RE.fullmatch(name))
    )


def _parse_param(param: str, *, function_input: bool) -> Param:
    is_tuple = bool(_IS_TUPLE_RE.fullmatch(param))
    match = (_TUPLE_PARAM_RE if is_tuple else _PARAM_RE).fullmatch(param)
    if not match:
        raise SpecError(f"invalid parameter {param[:80]!r}")
    name = match["name"] or ""
    if name and _is_keyword(name):
        raise SpecError(f"parameter name {name!r} is a Solidity keyword")
    base = match["type"]
    array = match["array"] or ""
    components: tuple[Param, ...] = ()
    if is_tuple:
        kind = "tuple"
        components = tuple(_parse_param(p, function_input=False) for p in _split_parameters(base))
    elif base in ("int", "uint"):
        kind = base + "256"
    elif base == "address payable":
        kind = "address"
    else:
        if not _is_solidity_type(base):
            raise SpecError(f"unknown Solidity type {base!r}")
        kind = base
    modifier = match["modifier"]
    if modifier:
        if not function_input or modifier not in _FUNCTION_MODIFIERS:
            raise SpecError(f"modifier {modifier!r} is not allowed on {param[:80]!r}")
        if not (array or kind in ("bytes", "string", "tuple")):
            raise SpecError(f"data location {modifier!r} is only for arrays, bytes, string or tuples")
    return Param(type=kind + array, name=name, components=components)


def parse_signature(signature: Any) -> Function:
    """CLI ``parseCallSignature``: ``name(type,...)`` with or without the ``function`` keyword."""
    text = str(signature or "").strip()
    if not text or len(text) > 1024:
        raise SpecError("the signature is empty or longer than 1024 characters")
    if any(not (" " <= ch <= "~") for ch in text):
        raise SpecError("the signature must be printable ASCII")
    full = text if text.startswith("function ") else f"function {text}"
    match = _FUNCTION_RE.fullmatch(full)
    if not match:
        raise SpecError(f"{text[:120]!r} is not a Solidity function signature")
    inputs = tuple(_parse_param(p, function_input=True) for p in _split_parameters(match["parameters"]))
    if match["returns"]:
        for p in _split_parameters(match["returns"]):
            _parse_param(p, function_input=True)
    return Function(name=match["name"], inputs=inputs)


# -- argument coercion (CLI ``placeholder`` + ``coerce``) -------------------------------------------------
def placeholder(raw: Any, b: Binding) -> str | int | None:
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if key == "$clone":
        return b.clone
    if key == "$vault":
        return b.vault
    if key == "$asset":
        return b.asset
    if key in ("$pullamount", "$pull_amount"):
        return b.pull_amount
    return None


def _no_constant(name: str) -> Any:
    raise ValueError(name)


def _parse_json(raw: str, path: str) -> Any:
    try:
        return json.loads(raw, parse_constant=_no_constant)
    except ValueError:
        raise SpecError(f"{path} is not valid JSON") from None


def _int_bounds(kind: str) -> tuple[int, int]:
    bits = int(kind.split("int", 1)[1] or 256)
    if kind.startswith("u"):
        return 0, 2**bits - 1
    return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1


def coerce(p: Param, raw: Any, b: Binding, path: str) -> Any:
    """One argument, as the CLI coerces it before viem encodes it. Raises SpecError where it would."""
    arr = _ARRAY_RE.fullmatch(p.type)
    if arr:
        value = _parse_json(raw, path) if isinstance(raw, str) else raw
        if not isinstance(value, list):
            raise SpecError(f"{path} must be a JSON array for {p.type}")
        if arr[2] != "" and len(value) != int(arr[2]):
            raise SpecError(f"{path} needs exactly {arr[2]} items for {p.type}")
        inner = Param(type=arr[1], name=p.name, components=p.components)
        return [coerce(inner, x, b, f"{path}[{i}]") for i, x in enumerate(value)]
    if p.type == "tuple":
        comps = p.components
        value = _parse_json(raw, path) if isinstance(raw, str) else raw
        if isinstance(value, list):
            if len(value) != len(comps):
                raise SpecError(f"{path} needs {len(comps)} tuple members")
            return [coerce(c, value[i], b, f"{path}.{c.name or i}") for i, c in enumerate(comps)]
        if isinstance(value, dict):
            out = []
            for i, c in enumerate(comps):
                key = c.name or str(i)
                if key not in value:
                    raise SpecError(f"{path}.{key} is missing")
                out.append(coerce(c, value[key], b, f"{path}.{key}"))
            return out
        raise SpecError(f"{path} must be a JSON array or object for a tuple")
    sub = placeholder(raw, b)
    if p.type == "address":
        if isinstance(sub, str):
            return sub
        if not isinstance(raw, str) or not _ADDRESS_RE.fullmatch(raw.strip()):
            raise SpecError(f"{path} must be an address or one of $clone, $vault, $asset")
        return raw.strip()
    if _INT_TYPE_RE.fullmatch(p.type):
        if isinstance(sub, int) and not isinstance(sub, bool):
            number = sub
        elif isinstance(raw, int) and not isinstance(raw, bool) and abs(raw) <= MAX_SAFE_INTEGER:
            number = raw
        elif isinstance(raw, str) and _DECIMAL_RE.fullmatch(raw.strip()):
            number = int(raw.strip())
        else:
            raise SpecError(
                f"{path} must be an integer in raw base units (a decimal string, or a JSON number up to "
                f"2^53-1) or $pullAmount for {p.type}"
            )
        lo, hi = _int_bounds(p.type)
        if not lo <= number <= hi:
            raise SpecError(f"{path} is out of range for {p.type}")
        return number
    if p.type == "bool":
        if raw is True or raw == "true":
            return True
        if raw is False or raw == "false":
            return False
        raise SpecError(f"{path} must be true or false")
    if p.type == "string":
        if not isinstance(raw, str):
            raise SpecError(f"{path} must be a string")
        return raw
    if _BYTES_TYPE_RE.fullmatch(p.type):
        text = raw.strip() if isinstance(raw, str) else ""
        if not _HEX_RE.fullmatch(text):
            raise SpecError(f"{path} must be 0x hex for {p.type}")
        if len(text) % 2:
            raise SpecError(f"{path} must be whole bytes (an even number of hex digits) for {p.type}")
        data = bytes.fromhex(text[2:])
        size = p.type[len("bytes"):]
        if size and len(data) != int(size):
            raise SpecError(f"{path} must be exactly {size} bytes for {p.type}")
        return data
    raise SpecError(f"{path}: unsupported type {p.type}")


# -- ABI encoding (standard head/tail; what viem's encodeFunctionData produces) --------------------------
def _word(n: int) -> bytes:
    return (n % 2**256).to_bytes(32, "big")


def _pad_right(data: bytes) -> bytes:
    return data + bytes(-len(data) % 32)


def is_dynamic(p: Param) -> bool:
    if p.type in ("string", "bytes"):
        return True
    arr = _ARRAY_RE.fullmatch(p.type)
    if arr:
        return arr[2] == "" or is_dynamic(Param(type=arr[1], components=p.components))
    if p.type == "tuple":
        return any(is_dynamic(c) for c in p.components)
    return False


def static_words(p: Param) -> int:
    arr = _ARRAY_RE.fullmatch(p.type)
    if arr and arr[2] != "":
        return int(arr[2]) * static_words(Param(type=arr[1], components=p.components))
    if p.type == "tuple":
        return sum(static_words(c) for c in p.components)
    return 1


def head_words(p: Param) -> int:
    return 1 if is_dynamic(p) else static_words(p)


def _encode_one(p: Param, value: Any) -> bytes:
    arr = _ARRAY_RE.fullmatch(p.type)
    if arr:
        inner = Param(type=arr[1], components=p.components)
        body = _encode_tuple([inner] * len(value), value)
        return _word(len(value)) + body if arr[2] == "" else body
    if p.type == "tuple":
        return _encode_tuple(list(p.components), value)
    if p.type == "address":
        return bytes(12) + bytes.fromhex(value[2:])
    if p.type == "bool":
        return _word(1 if value else 0)
    if _INT_TYPE_RE.fullmatch(p.type):
        return _word(value)
    if p.type == "string":
        data = value.encode("utf-8")
        return _word(len(data)) + _pad_right(data)
    if p.type == "bytes":
        return _word(len(value)) + _pad_right(value)
    return value + bytes(32 - len(value))  # bytesN


def _encode_tuple(params: list[Param], values: list[Any]) -> bytes:
    heads: list[bytes] = []
    tails: list[bytes] = []
    offset = 32 * sum(head_words(p) for p in params)
    for p, v in zip(params, values, strict=True):
        encoded = _encode_one(p, v)
        if is_dynamic(p):
            heads.append(_word(offset))
            tails.append(encoded)
            offset += len(encoded)
        else:
            heads.append(encoded)
    return b"".join(heads) + b"".join(tails)


def encode_call(fn: Function, args: Any, b: Binding, where: str) -> bytes:
    """``signature`` + ``args`` -> calldata (selector + arguments). CLI: ``args ?? []``, arity must match."""
    values = [] if args is None else args
    if not isinstance(values, list) or len(values) != len(fn.inputs):
        got = len(values) if isinstance(values, list) else "a non-array"
        raise SpecError(f"{where}: {fn.name} takes {len(fn.inputs)} argument(s), got {got}")
    try:
        coerced = [coerce(p, values[k], b, f"argument {k}") for k, p in enumerate(fn.inputs)]
    except SpecError as exc:
        raise SpecError(f"{where}: {exc}") from None
    return bytes.fromhex(fn.selector) + _encode_tuple(list(fn.inputs), coerced)


# -- what an approval grants ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Grant:
    label: str
    word: int  # argument word that carries the grant
    bits: int  # amount width; 0 for an on/off flag


_GRANTS: dict[str, _Grant] = {
    selector_of("approve(address,uint256)"): _Grant("approve", 1, 256),
    selector_of("increaseAllowance(address,uint256)"): _Grant("increaseAllowance", 1, 256),
    # Permit2 AllowanceTransfer.approve(token, spender, uint160 amount, uint48 expiration)
    selector_of("approve(address,address,uint160,uint48)"): _Grant("Permit2 approve", 2, 160),
    # EIP-2612 permit(owner, spender, value, deadline, v, r, s)
    selector_of("permit(address,address,uint256,uint256,uint8,bytes32,bytes32)"): _Grant("permit", 2, 256),
    # DAI-style permit(holder, spender, nonce, expiry, bool allowed, v, r, s): allowed=true is unlimited
    selector_of("permit(address,address,uint256,uint256,bool,uint8,bytes32,bytes32)"): _Grant("DAI permit", 4, 0),
    selector_of("setApprovalForAll(address,bool)"): _Grant("setApprovalForAll", 1, 0),
}


def unlimited_grant(data: bytes) -> str | None:
    """Why this calldata grants an unlimited allowance, or None.

    An amount of at least half its type's range counts as unlimited: 2^255 for a uint256
    approve, the same bound ``units.is_unlimited`` and ``rules.check_legs`` use, and 2^159 for
    Permit2's uint160. An operator approval (setApprovalForAll, DAI permit) is unlimited when on.
    """
    grant = _GRANTS.get(data[:4].hex())
    if grant is None:
        return None
    start = 4 + 32 * grant.word
    if len(data) < start + 32:
        return None  # truncated: the token's ABI decoder reverts, nothing is granted
    value = int.from_bytes(data[start : start + 32], "big")
    if grant.bits == 0:
        return f"{grant.label} turns on an operator approval over every token" if value else None
    if value >= 2 ** (grant.bits - 1):
        bound = "2^255" if grant.bits == 256 else f"2^{grant.bits - 1}"
        return f"{grant.label} of {value} (at or above {bound}, which the plugin treats as unlimited)"
    return None
