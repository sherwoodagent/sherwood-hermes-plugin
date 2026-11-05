#!/usr/bin/env bash
# External-signer driver: build the unsigned txs for ONE step of the fund loop and append
# them to an ordered "send these in order" list. Nothing is signed or sent; no key is read.
# The same command drives a fork (RPC_URL=http://127.0.0.1:8545) or Robinhood mainnet.
#
#   export SIGNER=external CHAIN_ID=4663 RPC_URL=<rpc>        # mainnet: an RPC you trust
#   export FORK_OUT_DIR=/some/dir                             # plan.md + plan/NN-<step>.json land here
#   scripts/fork/plan.sh <step> <args...>
#
# Steps (signer in brackets; addresses are the real wallets that will sign):
#   identity       <wallet> <name>                                   [that wallet]
#   create         <host> <agentId> <subdomain> <ipfs://metadata>    [host]  approve 1M WOOD fee (unless sponsored) + createSyndicate(USDG, whitelist)
#   register       <vault> <wallet> <agentId>                        [host]  registerAgent (run for the host itself, then for the agent)
#   voting-period  <vault> <seconds>                                 [host]
#   agent-fee      <vault> <bps>                                     [host]  keep < 1600 or the CLI guardian review will not Approve
#   approve        <vault> <depositor>                               [host]
#   deposit        <vault> <member> <usdg>                           [member] USDG approve + deposit
#   propose        <vault> <host> <ipfs://metadata> <usdg> <tokens> <weights> <routes> <maxSlipBps> [duration]
#                                                                    [host]  WOOD bond approve (quote+5%) + clone + propose
#   vote           <vault> <proposalId> <against|for|abstain>        [member]
#   open-review    <vault>                                           [anyone: keeper]
#   review-vote    <vault> <proposalId> <lockWood> <reason>          [guardian] Approve with a WOOD lock
#   resolve-review <vault>                                           [anyone: keeper]
#   execute        <vault> <proposalId>                              [anyone]
#   settle         <vault> <proposalId>                              [proposer before the duration ends]
#   reclaim-bond   <vault> <proposalId>                              [anyone; pays the proposer]
#   redeem         <vault> <member> <shares>                         [member] whole-share units (calldata-only cannot default to all)
#
# Reads that need no signer: `SIGNER=external scripts/fork/sw.sh proposal show <id> --vault <v>`,
# `... proposal execute --vault <v> --id <id> --dry-run`, `... sources get liquidity`.
# Several steps only make sense after a wait (voting period, review period, 1h after execute):
# check `proposal status` before building the next step. Calldata is built against the RPC's
# current state (bond quote, clone existence), so build each step right before sending it.
set -euo pipefail
export SIGNER="${SIGNER:-external}"
[ "$SIGNER" = "external" ] || { echo "plan.sh: run with SIGNER=external" >&2; exit 2; }
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
[ "$(cast chain-id --rpc-url "$RPC")" = "$CHAIN_ID" ] || { echo "plan.sh: RPC is not chain $CHAIN_ID" >&2; exit 2; }
mkdir -p "$STEPS_DIR"

# build <step-name> <signer-label> <cli args...>
build() {
  local name="$1" who="$2"; shift 2
  local raw="$STEPS_DIR/$name.calldata.raw" j="$STEPS_DIR/$name.calldata.json"
  "$FORK_DIR/sw.sh" --calldata-only "$@" > "$raw" 2> "$STEPS_DIR/$name.calldata.stderr" || {
    echo "plan.sh: CLI refused $name:" >&2; tail -20 "$STEPS_DIR/$name.calldata.stderr" >&2; exit 1; }
  sed -n '/^{/,$p' "$raw" > "$j"
  jq -e '.txs|length>0' "$j" >/dev/null || { echo "plan.sh: $name produced no txs (see $STEPS_DIR/$name.calldata.stderr)" >&2; exit 1; }
  grep -iE 'bond|warning|cooldown|preflight' "$STEPS_DIR/$name.calldata.stderr" | sed 's/^/  cli: /' || true
  plan_calldata "$name" "$who" "$j"
}

s="${1:-}"; shift || true
case "$s" in
  identity)       build identity "$1" identity mint --name "$2" ;;
  create)         build create "$1" vault create --creator "$1" --agent-id "$2" --subdomain "$3" --name "$3" \
                    --asset USDG --metadata-uri "$4" -y ;;
  register)       build "register-$2" host vault add --vault "$1" --wallet "$2" --agent-id "$3" ;;
  voting-period)  build voting-period host governor set-voting-period --vault "$1" --seconds "$2" ;;
  agent-fee)      build agent-fee host vault set-agent-fee --vault "$1" --bps "$2" ;;
  approve)        build "approve-$2" host vault approve-depositor --vault "$1" --depositor "$2" ;;
  deposit)        build "deposit-$2" "$2" vault deposit --vault "$1" --receiver "$2" --amount "$3" ;;
  propose)        build propose "$2" strategy propose portfolio --vault "$1" --proposer "$2" --metadata-uri "$3" \
                    --amount "$4" --tokens "$5" --weights "$6" --swap-routes "$7" --max-slippage "$8" \
                    --duration "${9:-1d}" --name "fund basket" --description "fund basket" --no-attest ;;
  vote)           build "vote-$2-$3" member proposal vote --vault "$1" --id "$2" --support "$3" ;;
  open-review)    build open-review keeper proposal open-reviews --vault "$1" ;;
  review-vote)    build "review-vote-$2" guardian proposal review-vote --vault "$1" --id "$2" --support approve \
                    --lock-wood "$3" --reason "$4" ;;
  resolve-review) build resolve-review keeper proposal resolve-reviews --vault "$1" ;;
  execute)        build "execute-$2" anyone proposal execute --vault "$1" --id "$2" ;;
  settle)         build "settle-$2" proposer proposal settle --vault "$1" --id "$2" ;;
  reclaim-bond)   build "reclaim-bond-$2" anyone proposal reclaim-bond --vault "$1" --id "$2" ;;
  redeem)         build "redeem-$2" "$2" vault redeem --vault "$1" --owner "$2" --receiver "$2" --shares "$3" ;;
  *) sed -n '2,32p' "$0"; exit 2 ;;
esac
