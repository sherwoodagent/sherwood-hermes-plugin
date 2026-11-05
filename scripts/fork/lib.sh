#!/usr/bin/env bash
# Shared constants + helpers for the 4663 fork scripts. Source it; do not run it.
# Addresses: sherwoodagent/skill ADDRESSES.md (chain 4663), same table the CLI ships.
export FOUNDRY_DISABLE_NIGHTLY_WARNING=1
export PATH="$PATH:$HOME/.foundry/bin:/usr/sbin:/sbin"
FORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RPC="${FORK_RPC_URL:-http://127.0.0.1:8545}"
FORK_OUT_DIR="${FORK_OUT_DIR:-$FORK_DIR/out}"
export FORK_RPC_URL="$RPC" FORK_OUT_DIR
mkdir -p "$FORK_OUT_DIR"

case "$RPC" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "lib.sh: refusing non-local RPC" >&2; return 2 2>/dev/null || exit 2 ;;
esac

# --- core (run 1 / run 2) ---
FACTORY=0x619af1488F32B4797f6faDa3C3a8De72a5B693E0
GUARDIAN_REGISTRY=0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72
SWOOD=0xdDf2B4C52B9575e0e6D03a3A57D75D06a4a9e203
STRATEGY_FACTORY=0xb06788F027268a9A06c3AD41a88559530D2E54b1
EXPOSURE_LEDGER=0x99D789E7C8E65dB5597dCa692F082B47BE30AB33
BOND_ESCROW=0x7d25058c891d3AC5C8bc8Da6aA937DFeE648B5cF
PORTFOLIO_TEMPLATE=0xAA5872009c527cCb80343E41C52840CEdb095eb0
IDENTITY_REGISTRY=0x8004A169FB4a3325136EB29fA0ceB6D2e539a432
# --- tokens ---
WOOD=0xF8BC08092C06dB6148114DCf82AF881F1085f92b
USDG=0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168
WETH=0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73
NVDA=0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC
AAPL=0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9
SPY=0x117cc2133c37B721F49dE2A7a74833232B3B4C0C
USDG_USD_FEED=0x61B7e5650328764B076A108EFF5fa7282a1B9aD2

# --- anvil dev accounts (public keys/addresses; fork-only) ---
HOST=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
AGENT=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
MEMBER_A=0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC
MEMBER_B=0x90F79bf6EB2c4f870365E785982E1f101E93b906
WETH_HOST=0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65
GUARDIAN=0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc
GUARDIAN_KEY=0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba
ROLE_ADDRS=("$HOST" "$AGENT" "$MEMBER_A" "$MEMBER_B" "$WETH_HOST" "$GUARDIAN")

role_key() {
  case "$1" in
    host)     echo 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 ;;
    agent)    echo 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d ;;
    memberA)  echo 0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a ;;
    memberB)  echo 0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6 ;;
    weth)     echo 0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a ;;
    guardian) echo "$GUARDIAN_KEY" ;;
    *) echo "unknown role $1" >&2; return 2 ;;
  esac
}

sw() { "$FORK_DIR/sw.sh" "$@"; }
c() { cast call --rpc-url "$RPC" "$@"; }
rpc() { cast rpc --rpc-url "$RPC" "$@"; }
now_ts() { cast block latest --field timestamp --rpc-url "$RPC"; }

# Advance chain time by N seconds and mine one block.
warp() { rpc evm_increaseTime "$1" >/dev/null; rpc evm_mine >/dev/null; }

# Append a row to the step log: step | status | tx | gas | note
STEP_LOG="${STEP_LOG:-$FORK_OUT_DIR/steps.tsv}"
logstep() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "${3:-}" "${4:-}" "${5:-}" | tee -a "$STEP_LOG"; }

# gas used of a tx hash on the fork
gas_of() { cast receipt "$1" gasUsed --rpc-url "$RPC" 2>/dev/null || echo "?"; }

head_block() { cast block-number --rpc-url "$RPC"; }

# Print every tx mined after block $1 (anvil automines one tx per block):
#   hash  from  to  selector  gasUsed  status
txs_since() {
  local from="$1" to b h
  to="$(head_block)"
  b="$from"
  while [ "$b" -lt "$to" ]; do
    b=$((b + 1))
    for h in $(cast block "$b" --json --rpc-url "$RPC" | jq -r '.transactions[] | if type=="string" then . else .hash end'); do
      local tx rc
      tx="$(cast tx "$h" --json --rpc-url "$RPC")"
      rc="$(cast receipt "$h" --json --rpc-url "$RPC")"
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$h" \
        "$(jq -r .from <<<"$tx")" "$(jq -r '.to // "create"' <<<"$tx")" \
        "$(jq -r '.input // .data' <<<"$tx" | cut -c1-10)" \
        "$(cast to-dec "$(jq -r .gasUsed <<<"$rc")")" \
        "$(jq -r .status <<<"$rc")"
    done
  done
}

# Run a CLI step as a role, capture its output, and log every tx it mined.
#   step <name> <role> <sherwood args...>
STEPS_DIR="${STEPS_DIR:-$FORK_OUT_DIR/steps}"
step() {
  local name="$1" role="$2"; shift 2
  mkdir -p "$STEPS_DIR"
  local before rc=0
  before="$(head_block)"
  echo ">>> [$name] ($role) sherwood $*" | tee "$STEPS_DIR/$name.out"
  SW_ROLE="$role" "$FORK_DIR/sw.sh" "$@" >>"$STEPS_DIR/$name.out" 2>&1 || rc=$?
  txs_since "$before" > "$STEPS_DIR/$name.txs" || true
  echo "<<< [$name] exit=$rc txs=$(wc -l < "$STEPS_DIR/$name.txs" | tr -d ' ')" | tee -a "$STEPS_DIR/$name.out"
  return $rc
}

# Sign + send every tx of a CLI `--calldata-only` JSON ({txs:[{to,data,value,chainId}]}) as <role>.
# This is the external-signer path: the CLI builds, cast signs. A StrategyFactory clone tx is
# skipped when the JSON's .clone already has code (the clone address is CREATE2-deterministic,
# so re-running calldata-only after a half-finished propose re-emits the same clone tx).
#   send_calldata <name> <role> <json-file>
send_calldata() {
  local name="$1" role="$2" json="$3" key n i to data value cid clone rc=0
  key="$(role_key "$role")"
  mkdir -p "$STEPS_DIR"
  : > "$STEPS_DIR/$name.txs"
  echo ">>> [$name] ($role) send $(jq '.txs|length' "$json") txs from $json" | tee "$STEPS_DIR/$name.out"
  clone="$(jq -r '.clone // empty' "$json")"
  n="$(jq '.txs|length' "$json")"
  for i in $(jot - 0 $((n - 1))); do
    to="$(jq -r ".txs[$i].to" "$json")"; data="$(jq -r ".txs[$i].data" "$json")"
    value="$(jq -r ".txs[$i].value // \"0x0\"" "$json")"; cid="$(jq -r ".txs[$i].chainId // 4663" "$json")"
    [ "$cid" = "4663" ] || { echo "tx $i chainId $cid != 4663" >&2; return 2; }
    if [ -n "$clone" ] && [ "$(echo "$to" | tr A-F a-f)" = "$(echo "$STRATEGY_FACTORY" | tr A-F a-f)" ] \
       && [ "$(cast code "$clone" --rpc-url "$RPC")" != "0x" ]; then
      echo "  tx $i: clone $clone already deployed, skipped" | tee -a "$STEPS_DIR/$name.out"; continue
    fi
    local out h st g
    if out="$(cast send --json --private-key "$key" --rpc-url "$RPC" --value "$(cast to-dec "$value")" "$to" "$data" 2>&1)"; then
      h="$(jq -r .transactionHash <<<"$out")"; st="$(jq -r .status <<<"$out")"; g="$(cast to-dec "$(jq -r .gasUsed <<<"$out")")"
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$h" "$role" "$to" "${data:0:10}" "$g" "$st" | tee -a "$STEPS_DIR/$name.txs"
    else
      echo "  tx $i ${data:0:10} -> $to REVERTED: $(echo "$out" | grep -oE 'execution reverted[^,]*(, data: "[^"]*")?' | head -1)" | tee -a "$STEPS_DIR/$name.out"
      rc=1; break
    fi
  done
  echo "<<< [$name] exit=$rc" | tee -a "$STEPS_DIR/$name.out"
  return $rc
}
