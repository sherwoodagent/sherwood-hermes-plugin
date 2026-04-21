"""Tests for sherwood_monitor.key_derivation."""
from __future__ import annotations

import pytest

from sherwood_monitor.key_derivation import (
    DEFAULT_SALT,
    address_from_private_key,
    derive_sidecar_key,
)

# ---------------------------------------------------------------------------
# Pinned test vector — computed once:
#   primary = "0x" + "11" * 32
#   data = pk_bytes + DEFAULT_SALT.encode("utf-8")
#   expected = keccak256(data)
# ---------------------------------------------------------------------------
_KNOWN_PRIMARY = "0x" + "11" * 32
_KNOWN_EXPECTED_SIDECAR_KEY = (
    "0xc667a0494ee5cea5a509e194179f43484584283ce6ef607210dd843eadbf0957"
)


def test_derive_sidecar_key_deterministic():
    """Same primary key + salt always produces the same sidecar key."""
    key1 = derive_sidecar_key(_KNOWN_PRIMARY, DEFAULT_SALT)
    key2 = derive_sidecar_key(_KNOWN_PRIMARY, DEFAULT_SALT)
    assert key1 == key2


def test_derive_sidecar_key_different_salts():
    """Different salts produce different sidecar keys."""
    key_a = derive_sidecar_key(_KNOWN_PRIMARY, "salt-a")
    key_b = derive_sidecar_key(_KNOWN_PRIMARY, "salt-b")
    assert key_a != key_b


def test_derived_key_format():
    """Derived key is 0x-prefixed and represents a 32-byte value (66 hex chars total)."""
    key = derive_sidecar_key(_KNOWN_PRIMARY)
    assert key.startswith("0x")
    assert len(key) == 66  # "0x" + 64 hex chars


def test_known_input_produces_known_output():
    """Pinned test vector — ensures keccak implementation is Ethereum-compatible."""
    result = derive_sidecar_key(_KNOWN_PRIMARY, DEFAULT_SALT)
    assert result == _KNOWN_EXPECTED_SIDECAR_KEY


def test_derived_address_format():
    """address_from_private_key returns 0x-prefixed 42-char EIP-55 checksum address."""
    addr = address_from_private_key(_KNOWN_PRIMARY)
    assert addr.startswith("0x")
    assert len(addr) == 42
    # EIP-55 checksum: mixed case (not all lower, not all upper for non-trivial keys)
    hex_part = addr[2:]
    assert hex_part != hex_part.lower() or hex_part == hex_part.upper(), (
        "address should be EIP-55 checksummed"
    )


def test_derive_strips_0x_prefix():
    """Keys passed with or without '0x' prefix produce identical results."""
    with_prefix = derive_sidecar_key("0x" + "aa" * 32)
    without_prefix = derive_sidecar_key("aa" * 32)
    assert with_prefix == without_prefix


def test_address_strips_0x_prefix():
    """address_from_private_key handles keys with and without '0x' prefix."""
    addr_with = address_from_private_key("0x" + "11" * 32)
    addr_without = address_from_private_key("11" * 32)
    assert addr_with == addr_without
