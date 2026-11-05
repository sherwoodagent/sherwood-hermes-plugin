#!/usr/bin/env bash
# Shared guards for the fork-only scripts. Source it; do not run it.
#
# These scripts are FORK-ONLY tooling for a local anvil fork of Robinhood Chain (4663).
# Mainnet signing goes through the Sherwood prepare API and the app's /tg/sign page, never
# through anything in scripts/fork/.

FORK_CHAIN_ID=4663

guard_die() { echo "fork guard: $*" >&2; exit 2; }

# The RPC must be exactly http://127.0.0.1:<port> or http://localhost:<port> (optional
# trailing slash). Whole-string match on the parsed form: no userinfo, no path, no other host.
assert_local_rpc() {
  local url="$1"
  [[ "$url" =~ ^http://(127\.0\.0\.1|localhost):([0-9]{1,5})/?$ ]] \
    || guard_die "RPC must be http://127.0.0.1:<port> or http://localhost:<port> (fork only); refusing"
}

# eth_chainId must be 4663. The URL reaches cast through ETH_RPC_URL, never argv.
assert_fork_chain() {
  local cid
  cid="$(ETH_RPC_URL="$1" FOUNDRY_DISABLE_NIGHTLY_WARNING=1 "${CAST:-cast}" chain-id 2>/dev/null || true)"
  [ "$cid" = "$FORK_CHAIN_ID" ] || guard_die "RPC answered eth_chainId '${cid}', want $FORK_CHAIN_ID"
}

# Before anything is signed: the node must be anvil (web3_clientVersion), not a real 4663 node.
assert_anvil() {
  local v
  v="$(ETH_RPC_URL="$1" FOUNDRY_DISABLE_NIGHTLY_WARNING=1 "${CAST:-cast}" client 2>/dev/null || true)"
  case "$v" in anvil/*) ;; *) guard_die "web3_clientVersion is '${v}', not anvil; refusing to sign" ;; esac
}

# An output dir from an older version (or someone else's run) may hold a populated
# sim-addresses.json that would silently redirect the CLI's contract addresses. These
# scripts never write one there (sw.sh writes '{}' into a per-call temp dir), so refuse.
assert_clean_out_dir() {
  [ ! -e "$1/sim-addresses.json" ] || guard_die "$1 has a sim-addresses.json (old or foreign output dir); use a fresh FORK_OUT_DIR"
}

# Mask RPC keys and credentials in anything printed: path after /v2/ or /v3/, long
# key-looking path segments, and basic-auth userinfo.
redact() {
  sed -u -E \
    -e 's#(https?|wss?)://[^/@[:space:]"'"'"']+@#\1://<redacted>@#g' \
    -e 's#(/v[23]/)[^/[:space:]"'"'"']+#\1<redacted>#g' \
    -e 's#((https?|wss?)://[^/[:space:]"'"'"']+/)[A-Za-z0-9_-]{20,}#\1<redacted>#g'
}
