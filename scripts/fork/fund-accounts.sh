#!/usr/bin/env bash
# FORK ONLY. Fund the anvil dev accounts on the 4663 fork. Idempotent: it SETS balances.
#
#   RPC_URL=http://127.0.0.1:8545 scripts/fork/fund-accounts.sh
#
# 1. Clears code on every role account. On the real chain 4663, anvil's well-known dev
#    accounts carry an EIP-7702 delegation (0xef0100 8a67b5...7e408a, a sweeper). On the
#    fork that code would run on ETH receipt / ERC-721 safeMint callbacks, so it is wiped.
# 2. ETH: 100 each (anvil_setBalance).
# 3. WOOD and USDG via anvil_setStorageAt on the balance mapping. anvil_dealERC20 cannot find
#    either slot, so the slots are fixed here (verified on block 106883230):
#      WOOD 0xF8BC...f92b  balances = mapping at slot 0
#      USDG 0x5fc5...1d168 (proxy) balances = mapping at slot 1
#    totalSupply is not adjusted (nothing in the loop reads it for these tokens).
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

assert_anvil "$RPC"   # the anvil_* cheat RPCs only exist on anvil

set_erc20() { # token slot holder amountWei
  local key; key="$(cast index address "$3" "$2")"
  rpc anvil_setStorageAt "$1" "$key" "$(cast to-uint256 "$4")" >/dev/null
  local got; got="$(c "$1" 'balanceOf(address)(uint256)' "$3" | awk '{print $1}')"
  [ "$got" = "$4" ] || { echo "balance set failed for $1 / $3: got $got want $4" >&2; exit 1; }
}

WOOD_HOST="${WOOD_HOST:-5000000}"          # 1,000,000 creation fee + proposer bonds
WOOD_AGENT="${WOOD_AGENT:-1000000}"
WOOD_WETH_HOST="${WOOD_WETH_HOST:-2000000}"
WOOD_GUARDIAN="${WOOD_GUARDIAN:-200000000}" # stake large enough to cover a full-notional book
USDG_A="${USDG_A:-1000}"
USDG_B="${USDG_B:-250}"
USDG_HOST="${USDG_HOST:-0}"

for a in "${ROLE_ADDRS[@]}"; do
  rpc anvil_setCode "$a" 0x >/dev/null
  rpc anvil_setBalance "$a" "$(cast to-hex "$(cast to-wei 100)")" >/dev/null
done

set_erc20 "$WOOD" 0 "$HOST"      "$(cast to-wei "$WOOD_HOST")"
set_erc20 "$WOOD" 0 "$AGENT"     "$(cast to-wei "$WOOD_AGENT")"
set_erc20 "$WOOD" 0 "$WETH_HOST" "$(cast to-wei "$WOOD_WETH_HOST")"
set_erc20 "$WOOD" 0 "$GUARDIAN"  "$(cast to-wei "$WOOD_GUARDIAN")"
set_erc20 "$USDG" 1 "$MEMBER_A"  "$((USDG_A * 1000000))"
set_erc20 "$USDG" 1 "$MEMBER_B"  "$((USDG_B * 1000000))"
set_erc20 "$USDG" 1 "$HOST"      "$((USDG_HOST * 1000000))"

printf '%-10s %-44s %8s %14s %10s %s\n' role address ETH WOOD USDG code
for pair in host:$HOST agent:$AGENT memberA:$MEMBER_A memberB:$MEMBER_B weth:$WETH_HOST guardian:$GUARDIAN; do
  r="${pair%%:*}"; a="${pair#*:}"
  printf '%-10s %-44s %8s %14s %10s %s\n' "$r" "$a" \
    "$(cast from-wei "$(cast balance "$a")" | cut -d. -f1)" \
    "$(cast from-wei "$(c "$WOOD" 'balanceOf(address)(uint256)' "$a" | awk '{print $1}')" | cut -d. -f1)" \
    "$(c "$USDG" 'balanceOf(address)(uint256)' "$a" | awk '{printf "%.2f", $1/1e6}')" \
    "$(cast code "$a")"
done
