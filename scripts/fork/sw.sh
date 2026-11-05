#!/usr/bin/env bash
# Run the pinned Sherwood CLI (scripts/fork/.cli, 0.102.0) against the LOCAL fork only.
#
#   SW_ROLE=host scripts/fork/sw.sh vault info --vault 0x...
#
# Isolation, so a fork run can never pick up a real key or a real RPC:
#   - HOME points at scripts/fork/out/home/<role>. The CLI reads ~/.sherwood/config.json
#     and a config privateKey / rpc entry WINS over the env vars, so the real ~/.sherwood
#     must never be visible to it.
#   - PRIVATE_KEY is an anvil dev key (public, worthless off the fork), chosen by SW_ROLE.
#   - ROBINHOOD_RPC_URL is the local fork. SIM_CHAIN_ID=4663 + SIM_ADDRESSES_FILE={} make
#     the CLI drop its public-RPC fallback (getRpcUrls in the CLI's network.ts), so a
#     dead fork fails loudly instead of falling through to rpc.mainnet.chain.robinhood.com.
#   - Refuses any RPC that is not 127.0.0.1/localhost or whose chain id is not 4663.
#   - SHERWOOD_SKIP_XMTP=1: no production XMTP groups for dev keys.
#   - SHERWOOD_METADATA_API_URL / SHERWOOD_API_URL point at a dead local port, so fork
#     identities and vaults are never published to the production metadata mirror or the
#     IPFS pinning API (pass --metadata-uri / --no-attest where a command would pin).
#     `sources get` uses SHERWOOD_SOURCES_API and still reads the live, open feed.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORK_RPC_URL="${FORK_RPC_URL:-http://127.0.0.1:8545}"
SW_ROLE="${SW_ROLE:-host}"
OUT="${FORK_OUT_DIR:-$HERE/out}"
CLI="$HERE/.cli/node_modules/.bin/sherwood"
CAST="${CAST:-$(command -v cast || echo "$HOME/.foundry/bin/cast")}"

case "$FORK_RPC_URL" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "sw.sh: refusing non-local RPC" >&2; exit 2 ;;
esac
if [ "${SW_SKIP_CHAIN_CHECK:-0}" != "1" ]; then
  cid="$("$CAST" chain-id --rpc-url "$FORK_RPC_URL" 2>/dev/null || true)"
  if [ "$cid" != "4663" ]; then
    echo "sw.sh: fork at $FORK_RPC_URL answered chain id '${cid}', want 4663" >&2; exit 2
  fi
fi
[ -x "$CLI" ] || { echo "sw.sh: CLI missing; run scripts/fork/install-cli.sh" >&2; exit 2; }

# Anvil's well-known dev keys (mnemonic "test test ... junk"). Public; fork-only.
case "$SW_ROLE" in
  host)    KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 ;; # 0xf39F...2266
  agent)   KEY=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d ;; # 0x7099...79C8
  memberA) KEY=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a ;; # 0x3C44...93BC
  memberB) KEY=0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6 ;; # 0x90F7...b906
  weth)    KEY=0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a ;; # 0x15d3...6A65
  guardian) KEY=0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba ;; # 0x9965...A4dc
  *) echo "sw.sh: unknown SW_ROLE '$SW_ROLE' (host|agent|memberA|memberB|weth|guardian)" >&2; exit 2 ;;
esac

mkdir -p "$OUT/home/$SW_ROLE"
[ -f "$OUT/sim-addresses.json" ] || echo '{}' > "$OUT/sim-addresses.json"

exec env \
  HOME="$OUT/home/$SW_ROLE" \
  PRIVATE_KEY="$KEY" \
  ROBINHOOD_RPC_URL="$FORK_RPC_URL" \
  SIM_CHAIN_ID=4663 \
  SIM_ADDRESSES_FILE="$OUT/sim-addresses.json" \
  SHERWOOD_SKIP_XMTP=1 \
  SHERWOOD_METADATA_API_URL="${SHERWOOD_METADATA_API_URL:-http://127.0.0.1:9}" \
  SHERWOOD_API_URL="${SHERWOOD_API_URL:-http://127.0.0.1:9}" \
  NO_COLOR=1 FORCE_COLOR=0 \
  "$CLI" "$@"
