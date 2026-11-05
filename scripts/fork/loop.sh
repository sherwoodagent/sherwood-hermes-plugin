#!/usr/bin/env bash
# Full PortfolioStrategy loop on a local anvil fork of Robinhood Chain mainnet (4663):
# identity -> vault (USDG, whitelist) -> members deposit -> agent registered (cannot propose)
# -> P1 proposed by the owner and vetoed by member B (20%) -> P2 proposed, nobody objects
# -> guardian review (staked guardian Approves with a WOOD lock) -> execute -> settle
# -> member A redeems. Plus the WETH-vault finding.
#
#   FORK_RPC_URL=http://127.0.0.1:8545 scripts/fork/loop.sh            # every stage, in order
#   FORK_RPC_URL=http://127.0.0.1:8545 scripts/fork/loop.sh execute    # one stage
#
# Stages: identity create members agent p1 p2 review execute settle redeem weth
# Idempotent: each stage records STAGE_<name>=done (and the ids it made) in
# $FORK_OUT_DIR/state.env and is skipped on a rerun. Delete state.env (and restart anvil)
# to run from scratch. Every CLI call goes through sw.sh (isolated HOME, local RPC only).
# Output: $FORK_OUT_DIR/steps/<step>.out|.txs and $FORK_OUT_DIR/summary.tsv.
#
# Prereqs: start-anvil.sh (fork up), install-cli.sh, fund-accounts.sh.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_anvil "loop.sh (it warps time and uses dev keys; for mainnet use plan.sh)"
[ "$(cast chain-id --rpc-url "$RPC")" = "$CHAIN_ID" ] || { echo "loop: $RPC is not chain $CHAIN_ID" >&2; exit 2; }

STATE="$FORK_OUT_DIR/state.env"; touch "$STATE"
# shellcheck disable=SC1090
source "$STATE"
RES="${RESULTS_DIR:-$FORK_OUT_DIR/results}"; mkdir -p "$RES" "$STEPS_DIR"

# A syntactically valid CID that is never pinned: fork vaults/proposals must not reach the
# production IPFS API (sw.sh points SHERWOOD_API_URL at a dead port anyway).
META=ipfs://bafkreigh2akiscaildcqabsyg3dfr6chu3fgpregiymsck7e7aqa4s52zy
VOTING_PERIOD=1800
AGENT_FEE_BPS="${AGENT_FEE_BPS:-500}"     # 20% default = the governor cap -> CLI guardian review refuses Approve
GUARDIAN_STAKE="${GUARDIAN_STAKE:-150000000}"
GUARDIAN_LOCK="${GUARDIAN_LOCK:-300000}"  # WOOD; ~$258 at the ledger's haircut price on block 106883230
AMOUNT="${AMOUNT:-500}"                   # USDG deployed per proposal (vault holds 1,250)
TOKENS=NVDA,AAPL,SPY
ROUTES=v3:500,v3:500,v4:500:5             # sources get liquidity -> recommended.by_symbol[*].swap_extra_data_hint
MAX_SLIP=125                              # largest suggested_max_slippage_bps across the legs (AAPL)

setv() { sed -i '' "/^$1=/d" "$STATE"; echo "$1=$2" >> "$STATE"; eval "$1=\"\$2\""; }
done_stage() { grep -q "^STAGE_$1=done$" "$STATE"; }
mark() { echo "STAGE_$1=done" >> "$STATE"; echo "=== stage $1 done"; }
note() { echo "$*" | tee -a "$STEPS_DIR/notes.txt"; }
state_of() { c "$GOVERNOR" 'getProposalState(uint256)(uint8)' "$1"; }
bal() { c "$1" 'balanceOf(address)(uint256)' "$2" | awk '{print $1}'; }

# expect_revert <name> <want-selector> <from> <to> <sig> [args...]: eth_call, log PASS/FAIL
expect_revert() {
  local name="$1" want="$2" from="$3" to="$4"; shift 4
  local out got
  out="$(cast call --from "$from" --rpc-url "$RPC" "$to" "$@" 2>&1 || true)"
  got="$(grep -oE 'data: "0x[0-9a-fA-F]{8}' <<<"$out" | head -1 | cut -c8-)"
  if [ "$got" = "$want" ]; then note "[$name] PASS reverted $want"; else note "[$name] FAIL want $want got '${got:-no revert}' :: $(head -c 300 <<<"$out")"; fi
}

# calldata_tx <name> <role> <cli args...>: build the unsigned tx list with --calldata-only,
# then sign + send it with cast. Used for vault create / vault add because their signed
# paths also start an XMTP client on the production XMTP network.
calldata_tx() {
  local name="$1" role="$2"; shift 2
  local j="$RES/$name.calldata.json"
  SW_ROLE="$role" "$FORK_DIR/sw.sh" --calldata-only "$@" > "$j.raw" 2> "$STEPS_DIR/$name.calldata.stderr"
  sed -n '/^{/,$p' "$j.raw" > "$j"; rm -f "$j.raw"
  jq -e '.txs|length>0' "$j" >/dev/null || { echo "calldata_tx $name: no txs (see $STEPS_DIR/$name.calldata.stderr)" >&2; return 1; }
  send_calldata "$name" "$role" "$j"
}

vault_of() { # subdomain -> vault address (factory.syndicates(subdomainToSyndicate(sub)).vault)
  local id; id="$(c "$FACTORY" 'subdomainToSyndicate(string)(uint256)' "$1" | awk '{print $1}')"
  c "$FACTORY" 'syndicates(uint256)(uint256,address,address,string,uint256,bool,string)' "$id" | sed -n 2p
}

# propose_calldata <json-out> <name> <description> <weights>
propose_calldata() {
  SW_ROLE=host "$FORK_DIR/sw.sh" --calldata-only strategy propose portfolio --vault "$VAULT" --proposer "$HOST" \
    --name "$2" --description "$3" --duration 1d --amount "$AMOUNT" --tokens "$TOKENS" --weights "$4" \
    --swap-routes "$ROUTES" --max-slippage "$MAX_SLIP" --metadata-uri "$META" --no-attest \
    > "$1" 2> "$1.stderr"
}

stage_identity() {
  local r id
  for r in host agent guardian weth; do
    local var="ID_$(echo "$r" | tr a-z A-Z)"
    [ -n "${!var:-}" ] && continue
    step "01-identity-$r" "$r" identity mint --name "Fork $r" --description "SHE-375 fork $r"
    id="$(grep -oE 'identity registered: #[0-9]+' "$STEPS_DIR/01-identity-$r.out" | grep -oE '[0-9]+$')"
    [ -n "$id" ] || { echo "no agent id for $r" >&2; exit 1; }
    setv "$var" "$id"
  done
  note "identities: host=$ID_HOST agent=$ID_AGENT guardian=$ID_GUARDIAN weth=$ID_WETH"
}

stage_create() {
  local sub="${SUBDOMAIN:-she375-fork-$(date +%s)}"; setv SUBDOMAIN "$sub"
  # minOwnerStake() is 0 on 4663: no prepareOwnerStake needed before createSyndicate.
  note "sWOOD.minOwnerStake=$(c "$SWOOD" 'minOwnerStake()(uint256)') factory.creationFee=$(c "$FACTORY" 'creationFee()(uint256)')"
  # approve 1M WOOD creation fee + createSyndicate (USDG, whitelist deposits)
  calldata_tx 02-vault-create host vault create --subdomain "$sub" --name "SHE-375 fork USDG" --agent-id "$ID_HOST" \
    --asset USDG --description "SHE-375 fork loop" --metadata-uri "$META" --creator "$HOST" -y
  setv VAULT "$(vault_of "$sub")"
  setv GOVERNOR "$(c "$FACTORY" 'governorOf(address)(address)' "$VAULT")"
  # keyless create does not register the creator; proposing requires it (NotRegisteredAgent)
  calldata_tx 02-register-creator host vault add --vault "$VAULT" --wallet "$HOST" --agent-id "$ID_HOST"
  step 03-set-voting-period host governor set-voting-period --vault "$VAULT" --seconds "$VOTING_PERIOD"
  step 03-set-agent-fee host vault set-agent-fee --vault "$VAULT" --bps "$AGENT_FEE_BPS"
  step 03-governor-info host governor info --vault "$VAULT"
  # Guardian stakes BEFORE any propose: review vote weight is the stake at the propose snapshot.
  step 03-guardian-stake guardian guardian stake "$GUARDIAN_STAKE" --agent-id "$ID_GUARDIAN"
  note "vault=$VAULT governor=$GOVERNOR"
}

stage_members() {
  step 04-approve-A host vault approve-depositor --vault "$VAULT" --depositor "$MEMBER_A"
  step 04-approve-B host vault approve-depositor --vault "$VAULT" --depositor "$MEMBER_B"
  step 05-deposit-A memberA vault deposit --vault "$VAULT" --amount "${USDG_A:-1000}"
  step 05-deposit-B memberB vault deposit --vault "$VAULT" --amount "${USDG_B:-250}"
  local ts a b; ts="$(c "$VAULT" 'totalSupply()(uint256)' | awk '{print $1}')"
  a="$(bal "$VAULT" "$MEMBER_A")"; b="$(bal "$VAULT" "$MEMBER_B")"
  note "shares: A=$a B=$b supply=$ts (B = $((b * 10000 / ts)) bps)"
}

stage_agent() {
  calldata_tx 06-register-agent host vault add --vault "$VAULT" --wallet "$AGENT" --agent-id "$ID_AGENT"
  note "factory.ownerOnlyProposals=$(c "$FACTORY" 'ownerOnlyProposals()(bool)')"
  # CLI refuses before signing (preflight); expected exit 1.
  step 07-propose-as-agent agent strategy propose portfolio --vault "$VAULT" --name "agent attempt" \
    --description "agent cannot propose" --duration 1d --amount "$AMOUNT" --tokens "$TOKENS" --weights 4000,3000,3000 \
    --swap-routes "$ROUTES" --max-slippage "$MAX_SLIP" --metadata-uri "$META" --no-attest || true
  # On-chain proof: the host's exact propose calldata, sent from the agent.
  propose_calldata "$RES/p1-propose-portfolio.calldata.json" "Veto test basket" "P1: member B vetoes" 4000,3000,3000
  local j="$RES/p1-propose-portfolio.calldata.json" n
  n="$(jq '.txs|length' "$j")"
  expect_revert 07-propose-as-agent-onchain 0x5bcd0b20 "$AGENT" "$(jq -r ".txs[$((n-1))].to" "$j")" "$(jq -r ".txs[$((n-1))].data" "$j")"
}

stage_p1() {
  # Signed `strategy propose` always pins metadata to api.sherwood.sh (even with --metadata-uri),
  # so the fork uses the external-signer path: --calldata-only JSON, signed with cast.
  local j="$RES/p1-propose-portfolio.calldata.json"
  [ -s "$j" ] || propose_calldata "$j" "Veto test basket" "P1: member B vetoes" 4000,3000,3000
  send_calldata 08-propose-P1 host "$j"
  setv P1 "$(c "$GOVERNOR" 'proposalCount()(uint256)')"
  note "P1=$P1 state=$(state_of "$P1") requiredCoverage=$(c "$GOVERNOR" 'getRequiredCoverage(uint256)(uint256)' "$P1")"
  note "P1 vote weight in the propose second: B=$(c "$GOVERNOR" 'getVoteWeight(uint256,address)(uint256)' "$P1" "$MEMBER_B")"
  warp 2
  note "P1 vote weight +2s: A=$(c "$GOVERNOR" 'getVoteWeight(uint256,address)(uint256)' "$P1" "$MEMBER_A") B=$(c "$GOVERNOR" 'getVoteWeight(uint256,address)(uint256)' "$P1" "$MEMBER_B")"
  step 09-vote-B-against-P1 memberB proposal vote --vault "$VAULT" --id "$P1" --support against
  warp $((VOTING_PERIOD + 1))
  note "P1 state after voting window: $(state_of "$P1") (4 = Rejected)"
  step 09-reclaim-bond-P1 host proposal reclaim-bond --vault "$VAULT" --id "$P1"
}

stage_p2() {
  local j="$RES/p2-propose-portfolio.calldata.json"
  propose_calldata "$j" "Pass test basket" "P2: nobody objects" 3400,3300,3300
  if grep -q CooldownNotElapsed "$j.stderr"; then
    note "P2: CooldownNotElapsed after the vetoed P1 (governor cooldown 3600s): warping"
    warp 3601
    propose_calldata "$j" "Pass test basket" "P2: nobody objects" 3400,3300,3300
  fi
  send_calldata 10-propose-P2 host "$j"
  setv P2 "$(c "$GOVERNOR" 'proposalCount()(uint256)')"
  setv CLONE_P2 "$(jq -r .clone "$j")"
  note "P2=$P2 clone=$CLONE_P2 state=$(state_of "$P2") requiredCoverage=$(c "$GOVERNOR" 'getRequiredCoverage(uint256)(uint256)' "$P2")"
  warp 2
  # Snapshot semantics (sandboxed): a share transfer after the snapshot does not move vote weight.
  local s; s="$(rpc evm_snapshot | tr -d '"')"
  cast send --private-key "$(role_key memberA)" --rpc-url "$RPC" "$VAULT" 'transfer(address,uint256)' "$MEMBER_B" 100000000000000 >/dev/null 2>&1 \
    && note "snapshot probe: A->B share transfer ok; B balance $(bal "$VAULT" "$MEMBER_B"), B weight on P2 $(c "$GOVERNOR" 'getVoteWeight(uint256,address)(uint256)' "$P2" "$MEMBER_B")" \
    || note "snapshot probe: share transfer reverted"
  rpc evm_revert "$s" >/dev/null
  warp $((VOTING_PERIOD + 1))
  note "P2 state after voting window, no objections: $(state_of "$P2") (2 = GuardianReview)"
}

stage_review() {
  # Path A (sandboxed): nobody opens the review.
  expect_revert 11-execute-during-review 0xcc6633c5 "$HOST" "$GOVERNOR" 'executeProposal(uint256)' "$P2"
  local s; s="$(rpc evm_snapshot | tr -d '"')"
  warp $((VOTING_PERIOD + 1))
  note "no-review path: state=$(state_of "$P2") review(opened,resolved,blocked)=$(c "$GUARDIAN_REGISTRY" 'getReviewState(address,uint256)(bool,bool,bool)' "$GOVERNOR" "$P2" | tr '\n' ' ')"
  expect_revert 11-execute-unreviewed 0xf7448092 "$HOST" "$GOVERNOR" 'executeProposal(uint256)' "$P2"
  rpc evm_revert "$s" >/dev/null
  # Path B: the guardian (or any keeper) opens the review, then Approves with a WOOD lock.
  step 12-open-reviews guardian proposal open-reviews --vault "$VAULT"
  if step 12-review-vote-approve guardian proposal review-vote --vault "$VAULT" --id "$P2" --support approve \
       --lock-wood "$GUARDIAN_LOCK" --accept-warnings --reason "fork loop: core Portfolio clone, 3 stock legs"; then
    note "guardian Approve via CLI review-vote"
  else
    note "CLI review-vote refused (see steps/12-review-vote-approve.out); falling back to raw voteOnProposal"
    local out
    out="$(cast send --json --private-key "$GUARDIAN_KEY" --rpc-url "$RPC" "$GUARDIAN_REGISTRY" \
      'voteOnProposal(address,uint256,uint8,uint256)' "$GOVERNOR" "$P2" 1 "$(cast to-wei "$GUARDIAN_LOCK")")"
    printf '%s\tguardian\t%s\tvoteOnProposal\t%s\t%s\n' "$(jq -r .transactionHash <<<"$out")" "$GUARDIAN_REGISTRY" \
      "$(cast to-dec "$(jq -r .gasUsed <<<"$out")")" "$(jq -r .status <<<"$out")" > "$STEPS_DIR/12-review-vote-approve-raw.txs"
  fi
  step 12-show-P2 host proposal show "$P2" --vault "$VAULT"
  note "coverage: $(grep -E 'Status:' "$STEPS_DIR/12-show-P2.out" | head -1 | sed 's/^ *//')"
}

stage_execute() {
  warp $((VOTING_PERIOD + 1))   # GuardianRegistry.reviewPeriod() = 1800 on 4663
  step 13-resolve-reviews agent proposal resolve-reviews --vault "$VAULT"
  # --dry-run exits 1 on a fork even when execute succeeds (simulation API unreachable; the
  # eth_call fallback reverts NotActiveProposalStrategy). Record it, do not gate on it.
  step 13-execute-dryrun agent proposal execute --vault "$VAULT" --id "$P2" --dry-run || true
  note "pre-execute totalAssets=$(c "$VAULT" 'totalAssets()(uint256)')"
  step 13-execute agent proposal execute --vault "$VAULT" --id "$P2"
  note "post-execute state=$(state_of "$P2") totalAssets=$(c "$VAULT" 'totalAssets()(uint256)') clone NVDA=$(bal "$NVDA" "$CLONE_P2") AAPL=$(bal "$AAPL" "$CLONE_P2") SPY=$(bal "$SPY" "$CLONE_P2")"
  step 13-strategy-status host strategy status "$CLONE_P2"
}

stage_settle() {
  expect_revert 14-settle-before-1h 0x418c8bb9 "$HOST" "$GOVERNOR" 'settleProposal(uint256)' "$P2"
  warp 3601
  step 14-settle-dryrun host proposal settle --vault "$VAULT" --id "$P2" --dry-run
  step 14-settle host proposal settle --vault "$VAULT" --id "$P2"
  note "post-settle state=$(state_of "$P2") (7 = Settled) totalAssets=$(c "$VAULT" 'totalAssets()(uint256)') supply=$(c "$VAULT" 'totalSupply()(uint256)')"
  expect_revert 14-reclaim-bond-in-challenge-window 0xfa7bc547 "$HOST" "$GOVERNOR" 'reclaimProposerBond(uint256)' "$P2"
  step 14-reclaim-bond-P2 host proposal reclaim-bond --vault "$VAULT" --id "$P2" || true
}

stage_redeem() {
  local before; before="$(bal "$USDG" "$MEMBER_A")"
  step 15-redeem-A memberA vault redeem --vault "$VAULT"
  note "member A USDG: before=$before after=$(bal "$USDG" "$MEMBER_A") shares left=$(bal "$VAULT" "$MEMBER_A")"
}

stage_weth() {
  local sub="${WETH_SUBDOMAIN:-she375-fork-weth-$(date +%s)}"; setv WETH_SUBDOMAIN "$sub"
  calldata_tx 20-weth-vault-create weth vault create --subdomain "$sub" --name "SHE-375 fork WETH" --agent-id "$ID_WETH" \
    --asset WETH --description "WETH finding" --metadata-uri "$META" --creator "$WETH_HOST" -y
  setv WETH_VAULT "$(vault_of "$sub")"
  setv WETH_GOVERNOR "$(c "$FACTORY" 'governorOf(address)(address)' "$WETH_VAULT")"
  calldata_tx 20-weth-register-creator weth vault add --vault "$WETH_VAULT" --wallet "$WETH_HOST" --agent-id "$ID_WETH"
  step 20-weth-approve-self weth vault approve-depositor --vault "$WETH_VAULT" --depositor "$WETH_HOST"
  step 20-weth-deposit weth vault deposit --vault "$WETH_VAULT" --amount 0.2 --use-eth
  step 20-weth-propose weth --calldata-only strategy propose portfolio --vault "$WETH_VAULT" --proposer "$WETH_HOST" \
    --asset WETH --name "WETH basket" --description "WETH finding" --duration 1d --amount 0.1 --tokens NVDA,SPY \
    --weights 5000,5000 --swap-routes v3:500,v4:500:5 --max-slippage 125 --metadata-uri "$META" --no-attest || true
  note "WETH CLI propose: $(grep -oE 'AssetNotUsdPegged[^:]*\[0x[0-9a-f]{8}\]' "$STEPS_DIR/20-weth-propose.out" | head -1)"
  expect_revert 20-weth-ledger-bond-quote 0x7454c9a2 "$WETH_HOST" "$EXPOSURE_LEDGER" 'proposerBondWood(address,uint256)' "$WETH" 100000000000000000
  # Governor-level: a hand-built WETH propose (registered strategy, WETH approve leg) prices
  # coverage on the ledger and reverts FeedNotConfigured().
  expect_revert 20-weth-governor-propose 0x7454c9a2 "$WETH_HOST" "$WETH_GOVERNOR" \
    'propose(address,address,string,uint256,(uint256,uint16),(address,bytes,uint256)[],uint256[],(address,bytes,uint256)[],uint256[],(address,uint256)[])' \
    "$WETH_VAULT" "$CLONE_P2" "$META" 86400 "(100000000000000000,2000)" \
    "[($WETH,$(cast calldata 'approve(address,uint256)' "$CLONE_P2" 100000000000000000),0),($CLONE_P2,0x61461954,0)]" \
    "[0,100000000000000000]" "[($CLONE_P2,$(cast calldata 'settle()'),0)]" "[0]" "[]"
}

ALL=(identity create members agent p1 p2 review execute settle redeem weth)
STAGES=("$@"); [ ${#STAGES[@]} -gt 0 ] || STAGES=("${ALL[@]}")
for s in "${STAGES[@]}"; do
  if done_stage "$s"; then echo "=== stage $s already done (state.env); skipping"; continue; fi
  echo "=== stage $s"
  "stage_$s"
  mark "$s"
done

# Flat table of every tx the run mined: step, hash, from/role, to, selector, gas, status
for f in "$STEPS_DIR"/*.txs; do
  [ -s "$f" ] || continue
  sed "s|^|$(basename "$f" .txs)\t|" "$f"
done > "$FORK_OUT_DIR/summary.tsv"
echo "summary: $FORK_OUT_DIR/summary.tsv ($(wc -l < "$FORK_OUT_DIR/summary.tsv" | tr -d ' ') txs); notes: $STEPS_DIR/notes.txt"
