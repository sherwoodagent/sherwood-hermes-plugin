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
    """Ethereum keccak-256 (eth-utils, else pycryptodome: the plugin's declared deps).

    Only a plan call given as ``signature`` needs it, and only when that plan is checked:
    importing the plugin never hashes (the selectors below are constants), so a Hermes
    runtime without a keccak backend still loads the plugin. There, a signature call is
    refused, never guessed.
    """
    try:
        from eth_utils import keccak  # type: ignore[import-not-found]

        return bytes(keccak(data))
    except Exception:  # eth-hash raises when no backend is installed
        pass
    try:
        from Crypto.Hash import keccak as _k  # type: ignore[import-not-found]

        return bytes(_k.new(digest_bits=256, data=data).digest())
    except Exception:
        raise SpecError(
            "this Hermes has no keccak backend (the plugin's eth-hash[pycryptodome] dependency), so a call "
            "given as a signature cannot be checked; give raw data, or install the plugin's dependencies"
        ) from None


def selector_of(signature: str) -> str:
    """4-byte selector (8 hex chars, no 0x) of a canonical ``name(type,...)`` signature."""
    return keccak256(signature.encode("ascii"))[:4].hex()


# Selectors the checks below use, as constants: keccak256(signature)[:4]. tests pin each one
# against eth-utils (test_generic_call_and_scope.py::test_selector_constants_match_keccak).
SELECTORS = {
    "approve(address,uint256)": "095ea7b3",
    "increaseAllowance(address,uint256)": "39509351",
    "approve(address,address,uint160,uint48)": "87517c45",  # Permit2 AllowanceTransfer
    "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)": "d505accf",  # EIP-2612
    "permit(address,address,uint256,uint256,bool,uint8,bytes32,bytes32)": "8fcbaf0c",  # DAI
    "setApprovalForAll(address,bool)": "a22cb465",
    "transfer(address,uint256)": "a9059cbb",
    "transferFrom(address,address,uint256)": "23b872dd",
    "safeTransferFrom(address,address,uint256)": "42842e0e",
    "safeTransferFrom(address,address,uint256,bytes)": "b88d4fde",
    "increaseApproval(address,uint256)": "d73dd623",  # the older OpenZeppelin name
    "authorizeOperator(address)": "959b8c3f",  # ERC777
    "approveAndCall(address,uint256,bytes)": "cae9ca51",
    # Wrappers whose inner calls run as the caller (a delegatecall into the same contract).
    "multicall(bytes[])": "ac9650d8",  # OpenZeppelin Multicall, Uniswap periphery
    "multicall(uint256,bytes[])": "5ae401dc",  # Uniswap SwapRouter02 (deadline)
    "multicall(bytes32,bytes[])": "1f0464d1",  # Uniswap SwapRouter02 (previous block hash)
    "batch(bytes[],bool)": "d2423b51",  # BoringBatchable
    # Multicall3: each (target, ..., callData) is called by the aggregator itself.
    "aggregate((address,bytes)[])": "252dba42",
    "aggregate3((address,bool,bytes)[])": "82ad56cb",
    "aggregate3Value((address,bool,uint256,bytes)[])": "174dea71",
    "tryAggregate(bool,(address,bytes)[])": "bce38bd7",
    "blockAndAggregate((address,bytes)[])": "c3077fa9",
    "tryBlockAndAggregate(bool,(address,bytes)[])": "399542e9",
}


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


MAX_TYPE_NESTING = 16


def _nesting(text: str) -> int:
    """Deepest tuple nesting plus the longest run of array dimensions: what the parser, ``coerce``
    and the encoder recurse on. Capped so a pathological signature is refused, not a RecursionError."""
    depth = deepest = 0
    for ch in text:
        if ch == "(":
            depth += 1
            deepest = max(deepest, depth)
        elif ch == ")":
            depth -= 1
    dims = max((m.group(0).count("[") for m in re.finditer(r"(?:\[[0-9]*\])+", text)), default=0)
    return deepest + dims


def parse_signature(signature: Any) -> Function:
    """CLI ``parseCallSignature``: ``name(type,...)`` with or without the ``function`` keyword."""
    text = str(signature or "").strip()
    if not text or len(text) > 1024:
        raise SpecError("the signature is empty or longer than 1024 characters")
    if any(not (" " <= ch <= "~") for ch in text):
        raise SpecError("the signature must be printable ASCII")
    if _nesting(text) > MAX_TYPE_NESTING:
        raise SpecError(f"the signature nests tuples or arrays deeper than {MAX_TYPE_NESTING} levels")
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
        body = _encode_tuple(list(fn.inputs), coerced)
    except SpecError as exc:
        raise SpecError(f"{where}: {exc}") from None
    except RecursionError:
        raise SpecError(f"{where}: the arguments nest too deeply to encode") from None
    return bytes.fromhex(fn.selector) + body


# -- ABI decoding check (raw data given with a signature) -------------------------------------------
def _read_word(buf: bytes, pos: int) -> int:
    if pos < 0 or pos + 32 > len(buf):
        raise SpecError("it is shorter than its arguments need")
    return int.from_bytes(buf[pos : pos + 32], "big")


def _safe(n: int, what: str) -> int:
    if n > MAX_SAFE_INTEGER:
        raise SpecError(f"{what} {n} is not a safe integer")
    return n


def _decode_tuple(params: list[Param], buf: bytes, base: int, depth: int) -> list[Any]:
    pos = base
    out: list[Any] = []
    for p in params:
        if is_dynamic(p):
            offset = _safe(_read_word(buf, pos), "an offset")
            out.append(_decode_one(p, buf, base + offset, depth + 1))
            pos += 32
        else:
            out.append(_decode_one(p, buf, pos, depth + 1))
            pos += 32 * static_words(p)
    return out


def _decode_one(p: Param, buf: bytes, pos: int, depth: int) -> Any:
    if depth > 32:
        raise SpecError("it nests deeper than 32 levels")
    if not is_dynamic(p) and pos + 32 * static_words(p) > len(buf):
        raise SpecError("it is shorter than its arguments need")
    arr = _ARRAY_RE.fullmatch(p.type)
    if arr:
        inner = Param(type=arr[1], components=p.components)
        if arr[2] == "":
            length = _safe(_read_word(buf, pos), "an array length")
            if length * 32 > len(buf):
                raise SpecError("an array length runs past the data")
            return _decode_tuple([inner] * length, buf, pos + 32, depth)
        if int(arr[2]) * 32 > len(buf):
            raise SpecError("a fixed array runs past the data")
        return _decode_tuple([inner] * int(arr[2]), buf, pos, depth)
    if p.type == "tuple":
        return _decode_tuple(list(p.components), buf, pos, depth)
    if p.type in ("bytes", "string"):
        length = _safe(_read_word(buf, pos), "a length")
        if pos + 32 + length > len(buf):
            raise SpecError("a bytes or string length runs past the data")
        raw = buf[pos + 32 : pos + 32 + length]
        return raw if p.type == "bytes" else raw.decode("utf-8", errors="replace")
    word = _read_word(buf, pos)
    if p.type == "bool":
        if word > 1:
            raise SpecError("a bool word is not 0 or 1")
        return bool(word)
    if p.type == "address":
        return "0x" + buf[pos + 12 : pos + 32].hex()
    if _INT_TYPE_RE.fullmatch(p.type):
        value = word - 2**256 if not p.type.startswith("u") and word >= 2**255 else word
        if int(p.type.split("int", 1)[1] or 256) <= 48:
            # viem reads uint8..uint48 (and int) through bytesToNumber, which refuses an unsafe number.
            _safe(abs(value), "a small integer")
        return value
    return buf[pos : pos + int(p.type[len("bytes"):] or 32)]  # bytesN


def decode_args(fn: Function, data: bytes) -> list[Any]:
    """The arguments of ``data`` (selector included) as ``fn`` types them, or SpecError."""
    try:
        return _decode_tuple(list(fn.inputs), data[4:], 0, 0)
    except RecursionError:
        raise SpecError("it nests too deeply to decode") from None


def decode_check(fn: Function, data: bytes) -> None:
    """CLI ``compileCall`` with both ``data`` and ``signature``: viem ``decodeFunctionData`` must accept it.

    The selector must match the signature, and the arguments must decode: in bounds, bool words
    0 or 1, offsets, lengths and small integers safe numbers. Stricter than viem where they could differ.
    """
    if data[:4].hex() != fn.selector:
        raise SpecError(
            f"data does not decode as {fn.canonical}: its selector is 0x{data[:4].hex()}, "
            f"the signature's is 0x{fn.selector}"
        )
    if not fn.inputs:
        return
    try:
        decode_args(fn, data)
    except SpecError as exc:
        raise SpecError(f"data does not decode as {fn.canonical}: {exc}") from None


GENERIC_CALL_ADDRESS_WORDS = {
    # CLI genericCallAddressWords: words below this count are spender/operator/recipient addresses.
    SELECTORS["approve(address,uint256)"]: 1,
    SELECTORS["setApprovalForAll(address,bool)"]: 1,
    SELECTORS["transfer(address,uint256)"]: 1,
    SELECTORS["transferFrom(address,address,uint256)"]: 2,
    SELECTORS["safeTransferFrom(address,address,uint256)"]: 2,
    SELECTORS["safeTransferFrom(address,address,uint256,bytes)"]: 2,
}


def address_words(data: bytes) -> int:
    return GENERIC_CALL_ADDRESS_WORDS.get(data[:4].hex(), 0)


def patch_word_index(fn: Function, arg_index: int) -> int:
    """CLI ``patchWordIndex``: the head word of a static uint argument, or SpecError."""
    inputs = fn.inputs
    if arg_index < 0 or arg_index >= len(inputs):
        raise SpecError(f"{fn.name} takes {len(inputs)} argument(s); there is no argument {arg_index}")
    target = inputs[arg_index]
    label = f"{target.name or f'arg{arg_index}'} ({target.type})"
    if is_dynamic(target):
        raise SpecError(
            f"argument {arg_index} {label} is dynamic: its head word is an offset. Pass a raw wordIndex for a value inside it"
        )
    if not re.fullmatch(r"uint[0-9]*", target.type):
        raise SpecError(f"argument {arg_index} {label} is not a uint; a balance patch writes an amount")
    return sum(head_words(p) for p in inputs[:arg_index])


# -- what an approval grants ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Grant:
    label: str
    word: int | None  # argument word that carries the amount or the on/off flag; None: always on
    bits: int  # amount width; 0 for an operator approval (a flag)
    token_word: int | None = None  # argument word naming the token (Permit2); None: the call's target


_GRANTS: dict[str, _Grant] = {
    SELECTORS["approve(address,uint256)"]: _Grant("approve", 1, 256),
    SELECTORS["increaseAllowance(address,uint256)"]: _Grant("increaseAllowance", 1, 256),
    SELECTORS["increaseApproval(address,uint256)"]: _Grant("increaseApproval", 1, 256),
    SELECTORS["approveAndCall(address,uint256,bytes)"]: _Grant("approveAndCall", 1, 256),
    # Permit2 AllowanceTransfer.approve(token, spender, uint160 amount, uint48 expiration)
    SELECTORS["approve(address,address,uint160,uint48)"]: _Grant("Permit2 approve", 2, 160, token_word=0),
    # EIP-2612 permit(owner, spender, value, deadline, v, r, s)
    SELECTORS["permit(address,address,uint256,uint256,uint8,bytes32,bytes32)"]: _Grant("permit", 2, 256),
    # DAI-style permit(holder, spender, nonce, expiry, bool allowed, v, r, s): allowed=true is unlimited
    SELECTORS["permit(address,address,uint256,uint256,bool,uint8,bytes32,bytes32)"]: _Grant("DAI permit", 4, 0),
    SELECTORS["setApprovalForAll(address,bool)"]: _Grant("setApprovalForAll", 1, 0),
    # ERC777 authorizeOperator(operator): an operator over every token the holder has
    SELECTORS["authorizeOperator(address)"]: _Grant("authorizeOperator", None, 0),
}

# Wrappers whose calls the scan opens: (signature, how to read the inner calls). "self": a
# bytes[] argument of calls into the same target (a delegatecall, so they run as the clone);
# "aggregate": (target, ..., callData) tuples the aggregator calls itself.
_WRAPPERS: dict[str, tuple[str, str, int]] = {
    SELECTORS["multicall(bytes[])"]: ("multicall(bytes[])", "self", 0),
    SELECTORS["multicall(uint256,bytes[])"]: ("multicall(uint256,bytes[])", "self", 1),
    SELECTORS["multicall(bytes32,bytes[])"]: ("multicall(bytes32,bytes[])", "self", 1),
    SELECTORS["batch(bytes[],bool)"]: ("batch(bytes[],bool)", "self", 0),
    SELECTORS["aggregate((address,bytes)[])"]: ("aggregate((address,bytes)[])", "aggregate", 0),
    SELECTORS["aggregate3((address,bool,bytes)[])"]: ("aggregate3((address,bool,bytes)[])", "aggregate", 0),
    SELECTORS["aggregate3Value((address,bool,uint256,bytes)[])"]: (
        "aggregate3Value((address,bool,uint256,bytes)[])", "aggregate", 0,
    ),
    SELECTORS["tryAggregate(bool,(address,bytes)[])"]: ("tryAggregate(bool,(address,bytes)[])", "aggregate", 1),
    SELECTORS["blockAndAggregate((address,bytes)[])"]: ("blockAndAggregate((address,bytes)[])", "aggregate", 0),
    SELECTORS["tryBlockAndAggregate(bool,(address,bytes)[])"]: (
        "tryBlockAndAggregate(bool,(address,bytes)[])", "aggregate", 1,
    ),
}
MAX_WRAPPER_DEPTH = 3
APPROVAL_FORMS = tuple(sorted({g.label for g in _GRANTS.values()}))
WRAPPER_FORMS = tuple(sig for sig, _, _ in _WRAPPERS.values())


@dataclass(frozen=True)
class Approval:
    """One approval a call makes: whose token, how much (None for an operator flag), and where."""

    label: str
    token: str  # lowercase token address, or the caller's target key (e.g. "$asset")
    amount: int | None
    bits: int
    on: bool
    path: str  # "" at the top level, else the wrapper chain, e.g. "multicall(bytes[]) call 1: "

    @property
    def unlimited_bound(self) -> int:
        return 2 ** (self.bits - 1)


def _wrapper_calls(selector: str, data: bytes, target: str) -> list[tuple[str, bytes]]:
    signature, kind, arg = _WRAPPERS[selector]
    try:
        decoded = decode_args(parse_signature(signature), data)
    except SpecError as exc:
        raise SpecError(f"{signature} wraps calls that do not decode ({exc}), so they cannot be checked for approvals") from None
    if kind == "self":
        return [(target, bytes(inner)) for inner in decoded[arg]]
    return [(str(row[0]).lower(), bytes(row[-1])) for row in decoded[arg]]


def approvals_in(data: bytes, target: str, *, depth: int = 0, path: str = "") -> list[Approval]:
    """Every approval ``data`` makes when called on ``target``, opening the wrappers in ``_WRAPPERS``.

    Raises SpecError for a wrapper whose calls do not decode or that nests deeper than
    ``MAX_WRAPPER_DEPTH``: a call the plugin cannot read is not a call it can vouch for.
    """
    selector = data[:4].hex() if len(data) >= 4 else ""
    grant = _GRANTS.get(selector)
    if grant is not None:
        def word(i: int) -> int | None:
            start = 4 + 32 * i
            return int.from_bytes(data[start : start + 32], "big") if len(data) >= start + 32 else None

        value = word(grant.word) if grant.word is not None else 1
        if value is None:
            return []  # truncated: the token's ABI decoder reverts, nothing is granted
        token = target
        if grant.token_word is not None:
            token_word = word(grant.token_word)
            token = "0x" + (token_word or 0).to_bytes(32, "big")[12:].hex()
        if grant.bits == 0:
            return [Approval(grant.label, token, None, 0, bool(value), path)]
        return [Approval(grant.label, token, value, grant.bits, True, path)]
    if selector in _WRAPPERS:
        signature = _WRAPPERS[selector][0]
        if depth >= MAX_WRAPPER_DEPTH:
            raise SpecError(f"{path}{signature} nests wrappers deeper than {MAX_WRAPPER_DEPTH} levels")
        found: list[Approval] = []
        for i, (inner_target, inner) in enumerate(_wrapper_calls(selector, data, target)):
            found += approvals_in(inner, inner_target, depth=depth + 1, path=f"{path}{signature} call {i}: ")
        return found
    return []
