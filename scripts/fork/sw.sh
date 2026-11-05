#!/usr/bin/env bash
# FORK-ONLY wrapper for the pinned Sherwood CLI (scripts/fork/.cli, 0.102.0).
# Mainnet signing goes through the Sherwood prepare API and the app's /tg/sign page,
# never through this script.
#
#   SW_ROLE=host scripts/fork/sw.sh vault info --vault 0x...            # signed by an anvil dev key
#   scripts/fork/sw.sh --calldata-only vault deposit --vault 0x... ...  # unsigned JSON, no key at all
#
# Parameters (env): RPC_URL (or FORK_RPC_URL; default http://127.0.0.1:8545),
# SW_ROLE host|agent|memberA|memberB|weth|guardian, FORK_OUT_DIR (default scripts/fork/out).
#
# Hard guards (guard.sh):
#   - RPC_URL must be exactly http://127.0.0.1:<port> or http://localhost:<port>, and its
#     eth_chainId must be 4663. Signed commands also require web3_clientVersion = anvil/*.
#   - The CLI runs under `env -i` with an explicit minimal environment, from a fresh empty
#     working directory per call. Nothing is inherited: no PRIVATE_KEY, no .env (the CLI
#     calls dotenv.config() on the cwd), no API URL overrides. HOME is
#     $FORK_OUT_DIR/home/<role>, so ~/.sherwood/config.json (whose privateKey/rpc would win
#     over the env) is never visible.
#   - --calldata-only runs with NO key and only for the subcommands that honour it
#     (CALLDATA_OK below). `vault set-agent-fee`, `proposal open-reviews` and
#     `proposal resolve-reviews` ignore --calldata-only in CLI 0.102.0 and sign directly.
#   - Signed `vault create|add|join|approve` and `chat` are refused: they start an XMTP client
#     on the production XMTP network. Use --calldata-only and sign with cast (lib.sh).
#   - Every outbound URL the CLI reads from the env is forced to the dead port
#     http://127.0.0.1:9, and netguard.cjs blocks every non-loopback TCP connection and DNS
#     lookup from the Node process (hard-coded hosts included); attempts go to
#     $FORK_OUT_DIR/netlog.tsv.
#   - SIM_CHAIN_ID + SIM_ADDRESSES_FILE ('{}', written fresh per call) drop the CLI's
#     public-RPC fallback, so a dead fork fails instead of reaching rpc.mainnet.chain.robinhood.com.
#   - CLI stderr goes through a redactor.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/guard.sh"
export FOUNDRY_DISABLE_NIGHTLY_WARNING=1
CAST="${CAST:-$(command -v cast || echo "$HOME/.foundry/bin/cast")}"
RPC_URL="${RPC_URL:-${FORK_RPC_URL:-http://127.0.0.1:8545}}"
SW_ROLE="${SW_ROLE:-host}"
OUT="${FORK_OUT_DIR:-$HERE/out}"
CLI="$HERE/.cli/node_modules/.bin/sherwood"
NODE_BIN="$(command -v node || true)"
[ -n "$NODE_BIN" ] || guard_die "node not found"
[ -x "$CLI" ] || guard_die "CLI missing; run scripts/fork/install-cli.sh"

assert_local_rpc "$RPC_URL"
assert_fork_chain "$RPC_URL"
assert_clean_out_dir "$OUT"

# Subcommands that honour --calldata-only in CLI 0.102.0 (verified on the fork with no key:
# each printed calldata JSON and sent nothing). Anything else is refused under --calldata-only.
CALLDATA_OK="identity mint|vault create|vault add|vault approve-depositor|vault deposit|vault redeem|governor set-voting-period|strategy propose|proposal vote|proposal review-vote|proposal execute|proposal settle|proposal reclaim-bond|guardian stake"
# Commands that start an XMTP client when signed.
XMTP_CMDS="vault create|vault add|vault join|vault approve|chat"

calldata=0
args=("$@")
if [ "${1:-}" = "--calldata-only" ]; then calldata=1; args=("${@:2}"); fi
cmd2="${args[0]:-} ${args[1]:-}"
cmd1="${args[0]:-}"

if [ "$calldata" = 1 ]; then
  ok=0
  IFS='|' read -r -a allowed <<<"$CALLDATA_OK"
  for a in "${allowed[@]}"; do [ "$cmd2" = "$a" ] && ok=1; done
  [ "$ok" = 1 ] || guard_die "--calldata-only is not honoured by '$cmd2' in CLI 0.102.0 (it would sign). Allowed: ${CALLDATA_OK//|/, }"
  KEY=""
  SW_ROLE="calldata"
else
  IFS='|' read -r -a xmtp <<<"$XMTP_CMDS"
  for a in "${xmtp[@]}"; do
    if [ "$cmd2" = "$a" ] || [ "$cmd1" = "$a" ]; then
      guard_die "signed '$a' starts an XMTP client on the production XMTP network; use --calldata-only and sign with cast"
    fi
  done
  [ "$cmd2" != "config set" ] || guard_die "'config set' would persist settings into the role HOME; not allowed"
  assert_anvil "$RPC_URL"
  # Anvil's well-known dev keys (mnemonic "test test ... junk"). Public; fork-only.
  case "$SW_ROLE" in
    host)     KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 ;; # 0xf39F...2266
    agent)    KEY=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d ;; # 0x7099...79C8
    memberA)  KEY=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a ;; # 0x3C44...93BC
    memberB)  KEY=0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6 ;; # 0x90F7...b906
    weth)     KEY=0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a ;; # 0x15d3...6A65
    guardian) KEY=0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba ;; # 0x9965...A4dc
    *) guard_die "unknown SW_ROLE '$SW_ROLE' (host|agent|memberA|memberB|weth|guardian)" ;;
  esac
fi

ROLE_HOME="$OUT/home/$SW_ROLE"
mkdir -p "$ROLE_HOME/tmp"
CWD="$(mktemp -d "$OUT/cwd.XXXXXX")"
echo '{}' > "$CWD/sim-addresses.json"
cleanup() { rm -f "$CWD/sim-addresses.json"; rmdir "$CWD" 2>/dev/null || echo "sw.sh: CLI left files in $CWD" >&2; }
trap cleanup EXIT

DEAD=http://127.0.0.1:9
ENVV=(
  PATH="$(dirname "$NODE_BIN"):/usr/bin:/bin"
  HOME="$ROLE_HOME"
  TMPDIR="$ROLE_HOME/tmp"
  LANG=en_US.UTF-8
  NODE_OPTIONS="--require $HERE/netguard.cjs"
  SW_NETLOG="$OUT/netlog.tsv"
  ROBINHOOD_RPC_URL="$RPC_URL"
  SIM_CHAIN_ID="$FORK_CHAIN_ID"
  SIM_ADDRESSES_FILE="$CWD/sim-addresses.json"
  SHERWOOD_SKIP_XMTP=1
  SHERWOOD_API_URL="$DEAD"
  SHERWOOD_INDEXER_URL="$DEAD"
  SHERWOOD_METADATA_API_URL="$DEAD"
  SHERWOOD_SOURCES_API="$DEAD"
  SHERWOOD_EXPLORER_API_URL="$DEAD"
  SUBGRAPH_URL="$DEAD"
  SPECTATOR_URL="$DEAD"
  PINATA_GATEWAY="$DEAD"
  LIGHTER_API_URL="$DEAD"
  NO_COLOR=1
  FORCE_COLOR=0
)
[ -z "$KEY" ] || ENVV+=(PRIVATE_KEY="$KEY")

set +e
( cd "$CWD" && env -i "${ENVV[@]}" "$CLI" "${@}" 2>&1 1>&3 3>&- | redact 1>&2; exit "${PIPESTATUS[0]}" ) 3>&1
rc=$?
set -e
exit $rc
