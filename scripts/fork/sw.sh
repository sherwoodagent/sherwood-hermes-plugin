#!/usr/bin/env bash
# Run the pinned Sherwood CLI (scripts/fork/.cli, 0.102.0) with an isolated HOME.
#
# Parameters (env):
#   RPC_URL    RPC to use (FORK_RPC_URL is accepted too). Default http://127.0.0.1:8545.
#   CHAIN_ID   chain id the RPC must answer. Default 4663.
#   SIGNER     anvil (default): fork only. Signs with an anvil dev key chosen by SW_ROLE,
#                and refuses any RPC that is not 127.0.0.1/localhost.
#              external: no key, ever. Every command must be --calldata-only (unsigned
#                EIP-5792 JSON for an external wallet to send) or a read-only command.
#                Any RPC, including Robinhood mainnet.
#   SW_ROLE    host|agent|memberA|memberB|weth|guardian (SIGNER=anvil only)
#
#   SW_ROLE=host scripts/fork/sw.sh vault info --vault 0x...                       # fork
#   SIGNER=external RPC_URL=$MAINNET_RPC scripts/fork/sw.sh --calldata-only vault deposit ...   # mainnet
#
# Isolation, so a run can never pick up a real key or a real RPC by accident:
#   - HOME points at $FORK_OUT_DIR/home/<role> (external: home/external). The CLI reads
#     ~/.sherwood/config.json and a config privateKey / rpc entry WINS over the env vars,
#     so the real ~/.sherwood must never be visible to it.
#   - SIGNER=anvil: PRIVATE_KEY is an anvil dev key (public, worthless off the fork).
#     SIGNER=external: PRIVATE_KEY is removed from the environment.
#   - ROBINHOOD_RPC_URL is $RPC_URL. SIM_CHAIN_ID + SIM_ADDRESSES_FILE={} make the CLI drop
#     its public-RPC fallback (getRpcUrls in the CLI's network.ts), so a dead RPC fails
#     loudly instead of falling through to rpc.mainnet.chain.robinhood.com.
#   - SHERWOOD_SKIP_XMTP=1 (honoured by `session check` only; see README for XMTP).
#   - SHERWOOD_METADATA_API_URL / SHERWOOD_API_URL default to a dead local port, so nothing
#     is published to the production metadata mirror or the IPFS pinning API. Pin metadata
#     separately and pass --metadata-uri. `sources get` uses SHERWOOD_SOURCES_API and still
#     reads the live, open feed.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RPC_URL="${RPC_URL:-${FORK_RPC_URL:-http://127.0.0.1:8545}}"
CHAIN_ID="${CHAIN_ID:-4663}"
SIGNER="${SIGNER:-anvil}"
SW_ROLE="${SW_ROLE:-host}"
OUT="${FORK_OUT_DIR:-$HERE/out}"
CLI="$HERE/.cli/node_modules/.bin/sherwood"
CAST="${CAST:-$(command -v cast || echo "$HOME/.foundry/bin/cast")}"
export FOUNDRY_DISABLE_NIGHTLY_WARNING=1

is_local() { case "$1" in http://127.0.0.1:*|http://localhost:*) return 0 ;; *) return 1 ;; esac; }

case "$SIGNER" in
  anvil)
    is_local "$RPC_URL" || { echo "sw.sh: SIGNER=anvil refuses a non-local RPC (use SIGNER=external for mainnet)" >&2; exit 2; } ;;
  external)
    # Only unsigned calldata or reads. The first non-flag word decides.
    if [ "${1:-}" != "--calldata-only" ]; then
      case "${1:-} ${2:-}" in
        "vault info"*|"vault balance"*|"vault list"*|"proposal show"*|"proposal status"*|"proposal list"*|\
        "governor info"*|"strategy status"*|"strategy list"*|"sources get"*|"guardian status"*|\
        "identity status"*|"identity find"*|"proposal execute"*|"proposal settle"*|"--version"*|"help"*) ;;
        *) echo "sw.sh: SIGNER=external runs only --calldata-only or read-only commands (got: $*)" >&2; exit 2 ;;
      esac
      # execute/settle are allowed bare only as --dry-run (eth_call, never signs)
      case "${1:-} ${2:-}" in
        "proposal execute"*|"proposal settle"*)
          case " $* " in *" --dry-run "*) ;; *) echo "sw.sh: SIGNER=external: proposal execute/settle need --calldata-only or --dry-run" >&2; exit 2 ;; esac ;;
      esac
    fi
    SW_ROLE=external ;;
  *) echo "sw.sh: SIGNER must be anvil or external (got '$SIGNER')" >&2; exit 2 ;;
esac

if [ "${SW_SKIP_CHAIN_CHECK:-0}" != "1" ]; then
  cid="$("$CAST" chain-id --rpc-url "$RPC_URL" 2>/dev/null || true)"
  if [ "$cid" != "$CHAIN_ID" ]; then
    echo "sw.sh: RPC answered chain id '${cid}', want $CHAIN_ID" >&2; exit 2
  fi
fi
[ -x "$CLI" ] || { echo "sw.sh: CLI missing; run scripts/fork/install-cli.sh" >&2; exit 2; }

KEY=""
if [ "$SIGNER" = "anvil" ]; then
  # Anvil's well-known dev keys (mnemonic "test test ... junk"). Public; fork-only.
  case "$SW_ROLE" in
    host)     KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 ;; # 0xf39F...2266
    agent)    KEY=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d ;; # 0x7099...79C8
    memberA)  KEY=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a ;; # 0x3C44...93BC
    memberB)  KEY=0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6 ;; # 0x90F7...b906
    weth)     KEY=0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a ;; # 0x15d3...6A65
    guardian) KEY=0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba ;; # 0x9965...A4dc
    *) echo "sw.sh: unknown SW_ROLE '$SW_ROLE' (host|agent|memberA|memberB|weth|guardian)" >&2; exit 2 ;;
  esac
fi

mkdir -p "$OUT/home/$SW_ROLE"
[ -f "$OUT/sim-addresses.json" ] || echo '{}' > "$OUT/sim-addresses.json"

ENVV=(
  HOME="$OUT/home/$SW_ROLE"
  ROBINHOOD_RPC_URL="$RPC_URL"
  SIM_CHAIN_ID="$CHAIN_ID"
  SIM_ADDRESSES_FILE="$OUT/sim-addresses.json"
  SHERWOOD_SKIP_XMTP=1
  SHERWOOD_METADATA_API_URL="${SHERWOOD_METADATA_API_URL:-http://127.0.0.1:9}"
  SHERWOOD_API_URL="${SHERWOOD_API_URL:-http://127.0.0.1:9}"
  NO_COLOR=1 FORCE_COLOR=0
)
if [ -n "$KEY" ]; then
  exec env "${ENVV[@]}" PRIVATE_KEY="$KEY" "$CLI" "$@"
else
  exec env -u PRIVATE_KEY "${ENVV[@]}" "$CLI" "$@"
fi
