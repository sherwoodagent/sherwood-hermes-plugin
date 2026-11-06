# Governance Reference

Each vault has its own `SyndicateGovernor` (a `BeaconProxy`, resolved with `factory.governorOf(vault)`), so every governance command takes `--vault`. Proposal lifecycle:

1. **Propose** — the vault owner (the only proposer while `ownerOnlyProposals` is on) submits a strategy with pre-committed execute + settle calls; a WOOD proposer bond is locked
2. **Vote** — vault shareholders vote weighted by shares at the snapshot; only `Against` counts, and the proposal is rejected if `Against` reaches `vetoThresholdBps` of the votable supply
3. **Guardian review** — staked guardians Approve (underwriting coverage) or Block; a block quorum rejects
4. **Execute** — permissionless within the execution window; deploys capital, locks the vault
5. **Settle** — proposer from 1h after execute, anyone after the strategy duration; owner emergency unwind is bonded and guardian-reviewed (`emergencySettleWithCalls` → `finalizeEmergencySettle`); `unstick` replays the voted settlement calls
6. **Reclaim** — the proposer bond comes back with `proposal reclaim-bond` once the proposal is terminal

States, in order: `Draft, Pending, GuardianReview, Approved, Rejected, Expired, Executed, Settled, Cancelled`.

> For deeper protocol context, see the [Governance docs](https://docs.sherwood.sh/protocol/governance/overview).

Fees are charged at settlement on the whole vault: the management fee (2%/yr at launch, accrued only while the strategy is live) first, then the performance fee on profit above the high-water mark. The agent fee is a **vault-owner property**, not a per-proposal parameter: the owner sets one fee for the vault via `sherwood vault set-agent-fee --vault <addr> --bps <bps>` (on-chain `vault.setAgentFeeBps(bps)`). It defaults to **20% (2000 bps)** and the vault caps it at **25% (2500 bps)**. The governor snapshots the vault's `agentFeeBps` onto each proposal at propose time and clamps it to its own `maxPerformanceFeeBps` (2000 bps on a factory-created vault) at settlement. `propose()` takes no fee argument.

## Create a proposal

Gather all inputs from the operator before running the command. For a template strategy, `sherwood strategy propose <template>` builds the calls and submits in one step; `proposal create` is the raw form.

While the factory's `ownerOnlyProposals` launch flag is on, only the vault owner can propose, and collaborative proposals are refused (`ProposerNotOwner`, `CollaborationDisabled`). Signed `proposal create` refuses any other proposer before pinning metadata or locking the bond; keyless (`--calldata-only`) `proposal create` refuses only when given `--proposer`, and otherwise warns and still prints the calldata. See "Single-operator vaults" in [SKILL.md](SKILL.md#single-operator-vaults).

If the proposal's strategy comes from `sherwood-strategies` (or from neither source repository), it is unaudited: run the checks in [Strategies from `sherwood-strategies` are unaudited](SKILL.md#strategies-from-sherwood-strategies-are-unaudited) and tell the operator before proposing.

`propose` locks a WOOD proposer bond (1% of required coverage, valued at the ledger's half-price WOOD and quoted in WOOD by the CLI; about 2% of deployed capital at market for an uncertified strategy; the CLI approves the quote plus a 5% buffer and re-quotes before propose). Why: [Proposer bond](https://docs.sherwood.sh/explanations/proposer-bond). Every template on 4663 is tier 2, so each strategy call's cap must also fit `totalAssets × tier2CallCapBps / 10,000` or propose reverts `Tier2CallCapExceedsCeiling(i)`. Show the operator the quoted bond and every parameter, and wait for an explicit yes before running the command (see "Ask before funds move" in [SKILL.md](SKILL.md#ask-before-funds-move)). For stock-token baskets, keep the execution window and the settle time out of the weekend feed gap (≈ Sat 18:40 → Mon 00:00 UTC).

```bash
sherwood proposal create \
  --vault 0x... \
  --strategy 0xCLONE \
  --name "TSLA/AMZN basket" \
  --description "60/40 TSLA and AMZN for 7 days" \
  --duration 7d \
  --max-capital 1000 \
  --execute-calls ./execute-calls.json \
  --settle-calls ./settle-calls.json
```

| Flag | Required | Description |
|------|----------|-------------|
| `--vault` | yes | Vault address the proposal targets |
| `--strategy` | yes | Strategy clone the calls target, registered on the `StrategyFactory` (`strategy propose --write-calls` prints it as `Clone address`). Required from CLI 0.96.2; the governor refuses an unregistered or zero address (`StrategyNotRegistered`) |
| `--name` | yes* | Strategy name (skipped if `--metadata-uri` provided) |
| `--description` | yes* | Strategy rationale and risk summary (skipped if `--metadata-uri`) |
| `--duration` | yes | Strategy duration. Accepts seconds or human format (`7d`, `24h`, `1h`); 1h–30d |
| `--max-capital` | yes | Ceiling the execute batch may deploy, in vault-asset units; at most `totalAssets × maxCapitalBps` |
| `--execute-calls` | yes | Path to JSON file with execute Call[] array (open positions) |
| `--settle-calls` | yes | Path to JSON file with settlement Call[] array (close positions) |
| `--metadata-uri` | no | Override — skip IPFS upload and use this URI directly |
| `--proposer` | keyless only | Wallet that will send the tx; enables the owner, bond and registration checks |

Execute calls run at proposal execution (open positions). Settlement calls run at proposal settlement (close positions). Each file is a JSON array of `[{ target, data, value }]`. Every target must be the vault asset or a strategy registered on `StrategyFactory`; at most 64 calls per batch.

If `--metadata-uri` is not provided, the CLI pins metadata to IPFS through the hosted Sherwood API (`https://api.sherwood.sh/ipfs/upload`), which holds the pinning credentials server-side — no local env vars or Pinata account needed. A failed upload is fatal: the CLI stops with `IPFS upload failed` (`IPFS pin failed` from `strategy propose`) before sending anything (no inline `data:` fallback). Retry, or run `sherwood pin-metadata <file>` and pass the printed URI as `--metadata-uri`. Sherwood reads metadata only through its own pinning, so always let the CLI pin it.

## Set the agent fee (vault owner)

```bash
sherwood vault set-agent-fee --vault 0x... --bps 2000   # 20% of profit at settlement
```

Defaults to 2000 bps (20%); the vault caps it at 2500 bps (25%). A factory-created vault's governor starts at `maxPerformanceFeeBps` 2000 bps, so anything above that is clamped down at settlement until the owner raises it with `governor set-max-fee`.

## List proposals

```bash
sherwood proposal list --vault <addr> [--state <filter>]
```

Filter by state: `draft`, `pending`, `guardianreview`, `approved`, `rejected`, `expired`, `executed`, `settled`, `cancelled`, `all` (default: `all`). The owner's veto works only while `Pending`; once a proposal is in `GuardianReview`, only guardian block-quorum stops it.

## Show proposal detail

```bash
sherwood proposal show <id> --vault <addr>
```

Displays metadata, state, the strategy and where it came from (see "Where a proposal's strategy came from" in [SKILL.md](SKILL.md#where-a-proposals-strategy-came-from)), timestamps, vote breakdown, decoded calls, capital snapshot (if executed), and P&L/fees (if settled).

## Vote on a proposal

```bash
sherwood proposal vote --vault 0x... --id <proposalId> --support <for|against|abstain>
```

`--vault` is required. Caller must have voting power: vault shares at the snapshot (the propose block's timestamp minus one), excluding shares sitting in the withdrawal queue. Delegation is disabled; every holder votes their own shares. Only `Against` counts toward the veto bar.

Before voting, run `sherwood proposal show <id> --vault <vault>` and read where the strategy came from (signed `proposal vote` prints the same lines; `--calldata-only` prints none). For an unaudited or hand-written strategy, run the checks in [Strategies from `sherwood-strategies` are unaudited](SKILL.md#strategies-from-sherwood-strategies-are-unaudited).

A vote cannot land in the second its proposal was created. The governor refuses votes while `block.timestamp <= snapshotTimestamp + 1` (`NotWithinVotingPeriod`), and `getVoteWeight` reads 0 until then. The signed CLI waits on the chain's clock before voting, and stops with `No block has been produced since this proposal …` if no later block has landed; rerun once one has. With `--calldata-only` it does not wait: broadcast the vote only after a block at least one second past the propose block, and resend it if it reverts `NotWithinVotingPeriod`.

## Guardian review

After the voting window, staked guardians review the calls for `reviewPeriod` (30 min on mainnet 4663; protocol-set in the GuardianRegistry, not by the vault owner): `sherwood proposal review-vote` casts Approve (declaring a WOOD lock that underwrites the book) or Block. Anyone may open and resolve the review. A block quorum (30% of staked WOOD at the snapshot) rejects the proposal and slashes the approvers; a shortfall of approving coverage scales the capital the proposal may deploy rather than blocking it (zero coverage reverts `InsufficientApproveCoverage`). Details in the `guardian` skill; why guardians lock WOOD and why a shortfall scales: [Tiers and coverage](https://docs.sherwood.sh/explanations/tiers-and-coverage). The review length is read live (`reviewPeriod()` on the registry); a review nobody opens resolves Cleared, and the last 10% (3 min) is locked against votes ([Governance timing](https://docs.sherwood.sh/explanations/governance-timing)).

**Check coverage before execute.** During `GuardianReview`, run `sherwood proposal show <id> --vault <vault>` and read its **Guardian Coverage** section: each approving guardian's WOOD lock and its USD value, the total WOOD locked, and covered vs required (required is in the vault asset, e.g. `60 USDG`). `Status: FULL` means execute deploys the full requested capital; `PARTIAL` deploys a proportional share; `NONE` means execute reverts `InsufficientApproveCoverage`. Do not read the governor's `effectiveMaxCapital` for this: it stays 0 until execute. Check again before Review End, while guardians can still Approve, and run `sherwood proposal execute --vault <vault> --id <id> --dry-run` before the real execute: it fails when coverage is short (pass `--allow-partial-coverage` to accept a partial deploy). If review ended with no coverage, cancel, wait out the cooldown that `proposal cancel` prints, and repropose.

## Execute an approved proposal

```bash
sherwood proposal execute --vault <addr> --id <proposalId>
```

Anyone can call. Verifies the proposal is Approved and within its execution window, that no other strategy is live, that the owner bond is live, that the tier and coverage have not regressed, and that `--max-capital` still fits `totalAssets × maxCapitalBps`. A proposal not executed inside the window becomes `Expired`. The CLI checks guardian coverage only with `--dry-run`; a plain `proposal execute` sends without checking, so dry-run first.

## Settle an executed proposal

```bash
sherwood proposal settle --vault <addr> --id <proposalId> [--calls <path-to-json>]
```

Auto-routes to the correct settlement path:
- **Proposer:** `settleProposal` — from 1 hour after execute (`StrategyDurationNotElapsed` before that)
- **Duration elapsed:** `settleProposal` — permissionless, anyone can call after the strategy duration
- **Vault owner emergency:** `emergencySettleWithCalls`. The owner commits new unwind calldata, must hold a bond covering `requiredOwnerBond` (never below 1,000 WOOD; an owner who bonded nothing before create cannot use it), and opens a guardian emergency review. Calls do **not** execute until `finalizeEmergencySettle`. A blocked review burns the owner bond. (`unstick` only replays already-voted settlement calls.)

Settlement reverts `StrategyNotSettled` if the strategy still reports `executed()` afterwards, and `SettlePriceBelowFloor` if the whole vault's share price fell more than the declared `maxDrawdownBps` (capped at 9000) since execute. The floor is on price per share, not on the deployed amount, so idle assets dilute the basket's loss. Below it only the owner's `unstick` (90% floor) or bonded emergency settle closes the proposal. Why: [Settlement and drawdown](https://docs.sherwood.sh/explanations/settlement-and-drawdown).

Output: P&L, fees distributed, redemptions unlocked, queued redeems stamped.

## Reclaim the proposer bond

```bash
sherwood proposal reclaim-bond --vault <addr> --id <proposalId>
```

Permissionless, pays the proposer. Immediately after Rejected, Expired or Cancelled; for Settled, after `executedAt + strategyDuration + 14 days`, because the bond must still exist while a challenge could be filed. Why: [Challenge window](https://docs.sherwood.sh/explanations/challenge-window).

## Cancel a proposal

```bash
sherwood proposal cancel --vault <addr> --id <proposalId> [--emergency]
```

Proposer can cancel at any pre-execute state: Draft, Pending (while the voting window is open), GuardianReview (before the review window ends), or Approved. Vault owner can emergency cancel (`--emergency`) from Draft or Pending **only** — once a proposal reaches GuardianReview, the owner loses unilateral cancel authority and only the proposer can cancel. Cancelling starts the cooldown.

## Veto a proposal (vault owner)

The CLI has no veto command. The owner calls `vetoProposal(proposalId)` on the governor (address from `governor info`) while the proposal is `Pending`; it becomes `Rejected`.

## Governor info

```bash
sherwood governor info --vault 0x...
```

Displays the governor address and its parameters: voting period, execution window, veto threshold, max performance fee, max strategy duration, cooldown period.

## Governor parameter setters (owner only)

```bash
sherwood governor set-voting-period --vault 0x... --seconds <n>      # 30 min–3d (default 24h; production 24h+ so depositors can veto or queue a redeem); testing: 1800. The floor is the governor's live MIN_VOTING_PERIOD() = 1800; the CLI help's "1h" is stale
sherwood governor set-execution-window --vault 0x... --seconds <n>   # 1h–7d
sherwood governor set-veto-threshold --vault 0x... --bps <n>         # 2000–8000
sherwood governor set-max-fee --vault 0x... --bps <n>                # ≤ 2500
sherwood governor set-max-duration --vault 0x... --seconds <n>       # ≤ 30d
sherwood governor set-cooldown --vault 0x... --seconds <n>           # 1h–30d
sherwood governor set-max-capital --vault 0x... --bps <n>        # share of TVL one proposal may deploy; seed 8000
sherwood governor set-tier2-call-cap --vault 0x... --bps <n>     # per-call cap for uncertified calls; leave at default until certified
sherwood vault set-min-buffer --vault 0x... --bps <n>            # idle reserve kept after any batch; seed 500, ≤ 5000
```

Each validates against the governor's bounds before submitting, and all governor setters revert `ParamsFrozenDuringProposal` while the vault has an open proposal (Drafts count). Seed `set-max-capital` and `set-min-buffer` right after `vault create` and before the first proposal; never raise `minBufferBps` while a proposal is open. Every template on 4663 is tier 2 and `strategy propose` puts the whole `--amount` on one call, so a proposal needs `--amount ≤ totalAssets × tier2CallCapBps / 10,000`: leave the tier-2 cap at its default (100%) until the strategy is certified, and ignore the 200 bps that signed `vault create` printed before CLI 0.96.2. Why: [Tiers and coverage](https://docs.sherwood.sh/explanations/tiers-and-coverage). Protocol-wide fee splits (`governor set-mgmt-split` / `set-perf-split`) live on `ProtocolConfig` and are protocol-owner only.

## Participation crons

`sherwood session cron <subdomain>` registers, shows (`--status`) or removes (`--remove`) the OpenClaw cron jobs that poll a vault's events and chat. Crons are not removed when you leave a vault; clean them up with `--remove`.
