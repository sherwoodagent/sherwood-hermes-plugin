"""Tests for sherwood_monitor.key_derivation."""
from __future__ import annotations

from sherwood_monitor.key_derivation import (
    address_from_private_key,
    normalize_address,
    normalize_private_key,
)

_KNOWN_PRIMARY = "0x" + "11" * 32


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


def test_address_strips_0x_prefix():
    """address_from_private_key handles keys with and without '0x' prefix."""
    addr_with = address_from_private_key("0x" + "11" * 32)
    addr_without = address_from_private_key("11" * 32)
    assert addr_with == addr_without


def test_normalize_private_key_and_address():
    assert normalize_private_key("AA" * 32) == "0x" + "aa" * 32
    assert normalize_private_key("not-a-key") is None
    assert normalize_address("0x" + "12" * 20) == "0x" + "12" * 20
    assert normalize_address("0x123") is None
