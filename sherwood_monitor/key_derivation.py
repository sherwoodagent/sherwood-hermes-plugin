"""Derive sidecar wallet key deterministically from the Sherwood primary key.

Mirrors CLAUDE.md's XMTP DB key derivation pattern:
    xmtpDbKey = keccak256(privateKey + "xmtp-db-key")

We use a different salt so the sidecar wallet is a distinct identity:
    sidecarKey = keccak256(privateKey + salt)
    where salt defaults to "sherwood-monitor-sidecar-v1"
"""
from __future__ import annotations

# eth-keys / eth-utils are imported when a key is derived, not at import time,
# so the plugin still registers (fund tools, Telegram cards) in a Hermes venv
# that has not installed the XMTP sidecar's crypto deps yet.

import re

DEFAULT_SALT = "sherwood-monitor-sidecar-v1"

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


def derive_sidecar_key(primary_key_hex: str, salt: str = DEFAULT_SALT) -> str:
    """Return 0x-prefixed 32-byte hex private key for the sidecar.

    Uses keccak-256 (Ethereum-compatible) over ``primary_key_bytes || salt_utf8``.
    Relies on ``eth-utils`` (backed by ``eth-hash[pycryptodome]``) for the hash.
    """
    from eth_utils import keccak

    pk_bytes = bytes.fromhex(primary_key_hex.removeprefix("0x"))
    data = pk_bytes + salt.encode("utf-8")
    digest = keccak(data)
    return "0x" + digest.hex()


def address_from_private_key(private_key_hex: str) -> str:
    """Return the 0x-checksummed Ethereum address for a private key."""
    from eth_keys import keys

    pk_bytes = bytes.fromhex(private_key_hex.removeprefix("0x"))
    pk = keys.PrivateKey(pk_bytes)
    return pk.public_key.to_checksum_address()
