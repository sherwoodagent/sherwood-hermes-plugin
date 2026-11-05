#!/usr/bin/env bash
# FORK ONLY. Build unsigned calldata for loop steps against the LOCAL fork and write them to
# an ordered "send these in order" list, to inspect what each step sends. Nothing is signed or
# sent, and the CLI runs with no key (sw.sh --calldata-only). Mainnet signing goes through the
# Sherwood prepare API and the app's /tg/sign page, NOT this script: calldata built here
# targets fork state (fork-created vault/governor addresses, fork bond quotes).
#
#   RPC_URL=http://127.0.0.1:8545 scripts/fork/plan.sh <step> <args...> [+ <step> <args...> ...]
#
# Every run writes a NEW directory $FORK_OUT_DIR/plans/<UTC time>-b<block>/ holding plan.md
# (stamped with block number + hash, chain id, RPC host, client version, CLI version) and
# NN-<step>.json. Existing plans are never appended to.
#
# Steps (signer in brackets):
#   identity       <wallet> <name>                                   [that wallet]
#   create         <host> <agentId> <subdomain> <ipfs://metadata>    [host]
#   register       <vault> <wallet> <agentId>                        [host]
#   voting-period  <vault> <seconds>                                 [host]
#   approve        <vault> <depositor>                               [host]
#   deposit        <vault> <member> <usdg>                           [member]
#   stake          <guardian> <wood> <agentId>                       [guardian]
#   propose        <vault> <host> <ipfs://metadata> <usdg> <tokens> <weights> <routes> <maxSlipBps> [duration]   [host]
#   vote           <vault> <proposalId> <against|for|abstain>        [member]
#   review-vote    <vault> <proposalId> <lockWood> <reason>          [guardian] (--accept-warnings: the fork has no simulation API)
#   execute        <vault> <proposalId>                              [anyone]
#   settle         <vault> <proposalId>                              [proposer]
#   reclaim-bond   <vault> <proposalId>                              [anyone]
#   redeem         <vault> <member> <shares>                         [member]
# Not producible with CLI 0.102.0 --calldata-only (the CLI signs directly): vault set-agent-fee,
# proposal open-reviews, proposal resolve-reviews. sw.sh refuses them.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

[ $# -gt 0 ] || { sed -n '2,33p' "$0"; exit 2; }

# Validate every step name before creating the plan directory.
expect_step=1
for a in "$@"; do
  if [ "$expect_step" = 1 ]; then
    case "$a" in
      identity|create|register|voting-period|approve|deposit|stake|propose|vote|review-vote|execute|settle|reclaim-bond|redeem) ;;
      agent-fee|open-review|resolve-review)
        guard_die "$a cannot be built: the CLI 0.102.0 command ignores --calldata-only and signs directly" ;;
      *) guard_die "unknown step '$a' (see the header of $0)" ;;
    esac
    expect_step=0
  fi
  [ "$a" != "+" ] || expect_step=1
done

BLOCK_NO="$(head_block)"
BLOCK_HASH="$(cast block "$BLOCK_NO" --field hash)"
CLIENT="$(cast client 2>/dev/null || echo unknown)"
CLI_VERSION="$("$FORK_DIR/.cli/node_modules/.bin/sherwood" --version 2>/dev/null || echo unknown)"
RPC_HOST="$(echo "$RPC" | sed -E 's#^http://##; s#/$##')"
PLAN="$FORK_OUT_DIR/plans/$(date -u +%Y%m%dT%H%M%SZ)-b$BLOCK_NO"
[ ! -e "$PLAN" ] || guard_die "$PLAN already exists; refusing to append to an existing plan"
mkdir -p "$PLAN"
{
  echo "# FORK ONLY: do not sign on mainnet"
  echo
  echo "Unsigned calldata built against a local anvil fork. Mainnet signing goes through the"
  echo "Sherwood prepare API and the app's /tg/sign page."
  echo
  echo "| Field | Value |"
  echo "|---|---|"
  echo "| Chain id | $FORK_CHAIN_ID |"
  echo "| RPC host | $RPC_HOST |"
  echo "| Client | $CLIENT |"
  echo "| Block | $BLOCK_NO |"
  echo "| Block hash | $BLOCK_HASH |"
  echo "| CLI | @sherwoodagent/cli $CLI_VERSION |"
  echo "| Built (UTC) | $(date -u +%Y-%m-%dT%H:%M:%SZ) |"
  echo
  echo "| # | Step | Signer | To | Selector | Value | Calldata | Note |"
  echo "|---|---|---|---|---|---|---|---|"
} > "$PLAN/plan.md"
K=0

# write_rows <name> <signer> <json>
write_rows() {
  local name="$1" who="$2" json="$3" n i to data value cid clone note deployed
  clone="$(jq -r '.clone // empty' "$json")"
  n="$(jq '.txs|length' "$json")"
  i=0
  while [ "$i" -lt "$n" ]; do
    to="$(jq -r ".txs[$i].to" "$json")"; data="$(jq -r ".txs[$i].data" "$json")"
    value="$(jq -r ".txs[$i].value // \"0x0\"" "$json")"; cid="$(jq -r ".txs[$i].chainId // \"\"" "$json")"
    [ "$cid" = "$FORK_CHAIN_ID" ] || guard_die "$name tx $i has chainId '$cid', want $FORK_CHAIN_ID"
    note=""
    if [ -n "$clone" ] && [ "$(echo "$to" | tr A-F a-f)" = "$(echo "$STRATEGY_FACTORY" | tr A-F a-f)" ]; then
      deployed="$(has_code "$clone")" || guard_die "cannot read code at clone $clone; refusing to guess (fail closed)"
      [ "$deployed" = 0 ] || note="SKIP: clone $clone already deployed"
    fi
    printf '| %02d.%d | %s | %s | `%s` | `%s` | %s | `%02d-%s.json` .txs[%d] | %s |\n' \
      "$K" "$((i + 1))" "$name" "$who" "$to" "${data:0:10}" "$value" "$K" "$name" "$i" "$note" >> "$PLAN/plan.md"
    i=$((i + 1))
  done
}

# build <step-name> <signer-label> <cli args...>
build() {
  local name="$1" who="$2"; shift 2
  K=$((K + 1))
  local j; j="$PLAN/$(printf '%02d' "$K")-$name.json"
  local before; before="$(head_block)"
  "$FORK_DIR/sw.sh" --calldata-only "$@" > "$j.raw" 2> "$PLAN/$(printf '%02d' "$K")-$name.stderr" \
    || { echo "plan.sh: CLI refused $name:" >&2; tail -20 "$PLAN/$(printf '%02d' "$K")-$name.stderr" >&2; exit 1; }
  sed -n '/^{/,$p' "$j.raw" > "$j"; rm -f "$j.raw"
  jq -e '.txs|length>0' "$j" >/dev/null || guard_die "$name produced no calldata JSON"
  [ "$(head_block)" = "$before" ] || guard_die "$name: the head block moved while building calldata; something was sent"
  write_rows "$name" "$who" "$j"
  echo "plan.sh: $name -> $j ($(jq '.txs|length' "$j") txs, signer: $who)"
}

run_step() {
  local s="$1"; shift
  case "$s" in
    identity)       build identity "$1" identity mint --name "$2" ;;
    create)         build create "$1" vault create --creator "$1" --agent-id "$2" --subdomain "$3" --name "$3" \
                      --asset USDG --metadata-uri "$4" -y ;;
    register)       build "register-$2" host vault add --vault "$1" --wallet "$2" --agent-id "$3" ;;
    voting-period)  build voting-period host governor set-voting-period --vault "$1" --seconds "$2" ;;
    approve)        build "approve-$2" host vault approve-depositor --vault "$1" --depositor "$2" ;;
    deposit)        build "deposit-$2" "$2" vault deposit --vault "$1" --receiver "$2" --amount "$3" ;;
    stake)          build "stake-$1" "$1" guardian stake "$2" --agent-id "$3" ;;
    propose)        build propose "$2" strategy propose portfolio --vault "$1" --proposer "$2" --metadata-uri "$3" \
                      --amount "$4" --tokens "$5" --weights "$6" --swap-routes "$7" --max-slippage "$8" \
                      --duration "${9:-1d}" --name "fund basket" --description "fund basket" --no-attest ;;
    vote)           build "vote-$2-$3" member proposal vote --vault "$1" --id "$2" --support "$3" ;;
    review-vote)    build "review-vote-$2" guardian proposal review-vote --vault "$1" --id "$2" --support approve \
                      --lock-wood "$3" --reason "$4" --accept-warnings ;;
    execute)        build "execute-$2" anyone proposal execute --vault "$1" --id "$2" ;;
    settle)         build "settle-$2" proposer proposal settle --vault "$1" --id "$2" ;;
    reclaim-bond)   build "reclaim-bond-$2" anyone proposal reclaim-bond --vault "$1" --id "$2" ;;
    redeem)         build "redeem-$2" "$2" vault redeem --vault "$1" --owner "$2" --receiver "$2" --shares "$3" ;;
    agent-fee|open-review|resolve-review)
      guard_die "$s cannot be built: the CLI 0.102.0 command ignores --calldata-only and signs directly" ;;
    *) guard_die "unknown step '$s' (see the header of $0)" ;;
  esac
}

# Split "step args... + step args..." and run each in order.
cur=()
for a in "$@"; do
  if [ "$a" = "+" ]; then run_step "${cur[@]}"; cur=(); else cur+=("$a"); fi
done
[ ${#cur[@]} -eq 0 ] || run_step "${cur[@]}"
echo "plan.sh: ordered list (FORK ONLY): $PLAN/plan.md"
