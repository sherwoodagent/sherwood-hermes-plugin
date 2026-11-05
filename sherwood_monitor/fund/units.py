"""Exact decimal <-> base-unit conversion (no floats anywhere near money)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, localcontext

_DECIMAL_RE = re.compile(r"^-?\d+(\.\d+)?$")

UINT256_MAX = 2**256 - 1
# Any approval at or above 2**255 is "unlimited" for our purposes: no real
# quote is that large, and wallets treat such values as infinite allowances.
UNLIMITED_THRESHOLD = 2**255


class UnitsError(ValueError):
    pass


def to_base_units(amount: str | int | Decimal, decimals: int, *, allow_negative: bool = False) -> int:
    """`"1.5"` with 6 decimals -> 1500000. Rejects floats and excess precision."""
    if isinstance(amount, bool) or isinstance(amount, float):
        raise UnitsError("pass amounts as decimal strings, not floats")
    if isinstance(amount, int):
        value = Decimal(amount)
    elif isinstance(amount, Decimal):
        value = amount
    else:
        text = str(amount).strip().replace(",", "").replace("_", "")
        if not _DECIMAL_RE.match(text):
            raise UnitsError(f"not a decimal amount: {amount!r}")
        try:
            value = Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - regex guards this
            raise UnitsError(f"not a decimal amount: {amount!r}") from exc
    if value < 0 and not allow_negative:
        raise UnitsError(f"amount must not be negative: {amount!r}")
    with localcontext() as ctx:
        ctx.prec = 100  # uint256 needs 78 digits; the default 28 would round
        scaled = value.scaleb(decimals)
        if scaled != scaled.to_integral_value():
            raise UnitsError(f"{amount!r} has more than {decimals} decimal places")
        return int(scaled)


def parse_base_units(value: str | int, *, allow_negative: bool = False) -> int:
    """Parse an integer base-unit string ("1500000")."""
    if isinstance(value, bool) or isinstance(value, float):
        raise UnitsError("base units must be an integer string")
    text = str(value).strip()
    if not re.match(r"^-?\d+$", text):
        raise UnitsError(f"not an integer base-unit amount: {value!r}")
    out = int(text)
    if out < 0 and not allow_negative:
        raise UnitsError(f"amount must not be negative: {value!r}")
    return out


def format_units(units: int | str, decimals: int, *, max_places: int | None = None) -> str:
    """1500000 with 6 decimals -> "1.5". Trims trailing zeros."""
    with localcontext() as ctx:
        ctx.prec = 100
        value = Decimal(int(units)).scaleb(-decimals)
        if max_places is not None:
            value = value.quantize(Decimal(1).scaleb(-max_places))
        text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def is_unlimited(amount: int | str) -> bool:
    if isinstance(amount, str):
        text = amount.strip().lower()
        if text in {"max", "unlimited", "infinite", "uint256max", "type(uint256).max"}:
            return True
        if text.startswith("0x"):
            try:
                return int(text, 16) >= UNLIMITED_THRESHOLD
            except ValueError:
                return False
        if not text.isdigit():
            return False
        amount = int(text)
    return int(amount) >= UNLIMITED_THRESHOLD
