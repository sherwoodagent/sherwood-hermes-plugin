"""Monitor-wallet key and address helpers (the gas watchdog's address).

Validates the declared ``SHERWOOD_PRIVATE_KEY`` / ``SHERWOOD_AGENT_ADDRESS``
values and derives the 0x address from the key when only the key is set.
"""
from __future__ import annotations

# eth-keys is imported when an address is derived, not at import time, so the
# plugin still registers (fund tools, Telegram cards) in a Hermes venv that has
# not installed its crypto deps yet.

import re

_PRIVATE_KEY_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{64})$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_private_key(raw: str | None) -> str | None:
    """``0x`` + 64 hex (the ``0x`` may be missing), else ``None``. Never echoes the input."""
    match = _PRIVATE_KEY_RE.match((raw or "").strip())
    return "0x" + match.group(1).lower() if match else None


def normalize_address(raw: str | None) -> str | None:
    """``0x`` + 40 hex, else ``None``."""
    value = (raw or "").strip()
    return value if _ADDRESS_RE.match(value) else None


def address_from_private_key(private_key_hex: str) -> str:
    """Return the 0x-checksummed Ethereum address for a private key."""
    from eth_keys import keys

    pk_bytes = bytes.fromhex(private_key_hex.removeprefix("0x"))
    pk = keys.PrivateKey(pk_bytes)
    return pk.public_key.to_checksum_address()
