"""Derive sidecar wallet key deterministically from the Sherwood primary key.

Mirrors CLAUDE.md's XMTP DB key derivation pattern:
    xmtpDbKey = keccak256(privateKey + "xmtp-db-key")

We use a different salt so the sidecar wallet is a distinct identity:
    sidecarKey = keccak256(privateKey + salt)
    where salt defaults to "sherwood-monitor-sidecar-v1"
"""
from __future__ import annotations

from eth_keys import keys
from eth_utils import keccak

DEFAULT_SALT = "sherwood-monitor-sidecar-v1"


def derive_sidecar_key(primary_key_hex: str, salt: str = DEFAULT_SALT) -> str:
    """Return 0x-prefixed 32-byte hex private key for the sidecar.

    Uses keccak-256 (Ethereum-compatible) over ``primary_key_bytes || salt_utf8``.
    Relies on ``eth-utils`` (backed by ``eth-hash[pycryptodome]``) for the hash.
    """
    pk_bytes = bytes.fromhex(primary_key_hex.removeprefix("0x"))
    data = pk_bytes + salt.encode("utf-8")
    digest = keccak(data)
    return "0x" + digest.hex()


def address_from_private_key(private_key_hex: str) -> str:
    """Return the 0x-checksummed Ethereum address for a private key."""
    pk_bytes = bytes.fromhex(private_key_hex.removeprefix("0x"))
    pk = keys.PrivateKey(pk_bytes)
    return pk.public_key.to_checksum_address()
