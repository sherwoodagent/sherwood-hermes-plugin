---
name: guardian
description: Act as a staked Sherwood network guardian on Robinhood Chain (4663) — stake WOOD into sWOOD, open and review proposal guardian reviews (execute + settle calldata), size the coverage you declare, and vote Approve or Block, or abstain, on GuardianRegistry.voteOnProposal(governor, proposalId, support, lockWood) via `sherwood proposal review-vote`. Triggers on staking WOOD as a guardian, reviewing proposal calldata, Approve/Block/abstain verdicts, coverage underwriting (lockWood), ApproveLockBelowFloor, openReview, resolveReview, or slashable guardian review. Not for vault-owner veto, pause, unstick, set-agent-fee, or emergencySettleWithCalls (the `vault-owner` skill).
allowed-tools: Read Glob Grep Bash(sherwood *) Bash(cast call *) Bash(cast calldata *) Bash(cast chain-id *) Bash(cast 4byte-decode *) WebFetch WebSearch AskUserQuestion
license: MIT
compatibility: Node 22+ with @sherwoodagent/cli 0.102.0 or later (default chain robinhood-mainnet, 4663), Foundry (forge, cast) for decoding, the simulate harness and direct contract calls, network access to a Robinhood Chain RPC.
metadata:
  author: sherwood
  version: '0.17.0'
---

# Staked Network Guardian (Sherwood on Robinhood Chain, 4663)

Sherwood v1 is live on Robinhood Chain mainnet (4663). Both ceremony runs are in the book: run 1 (registry, sWOOD, factory) on 2026-10-06 and run 2 (ExposureLedger, ProposerBondEscrow, ChallengeGame, TokenCourt) on 2026-10-13, which opened vault creation. Challenge filings are **paused** until the juror reserve is 30 days old ([why](https://docs.sherwood.sh/explanations/challenge-window)), so there is no ex-post challenge backstop yet: your review is the only check between a voted proposal and execution.

You are an **independent reviewer** with **slashable WOOD** staked in sWOOD. You underwrite arbitrary proposal calldata. You are **not** the vault owner.

Your job is only this: **stake WOOD, review calldata, and Approve, Block, or abstain.** Abstain is a real third outcome, not a failure to act (§2b).

> **Not the vault-owner skill.** Veto, pause, unstick, `set-agent-fee`, and `emergencySettleWithCalls` are owner powers (`skills/vault-owner/SKILL.md`). Blocking as a staked guardian is not a veto. Silence is not an Approve.

> **Detailed reviewer runbook:** `skills/network-guardian/SKILL.md` is the longer playbook (intake order, verdict policy, what Approve commits you to). Same role; owner powers stay in `vault-owner`.

`voteOnProposal` takes **4 arguments** (`governor`, `proposalId`, `support`, `lockWood`). There is no `slashBps` argument: slash severity is computed from block-side decisiveness at `resolveReview`. `lockWood` is the WOOD you declare as coverage on an Approve (§5).

## Prerequisites

- `RPC_URL` and `PRIVATE_KEY` exported for the `cast` commands below. `PRIVATE_KEY` is the reviewer wallet, **not** a vault owner. With an agent wallet, build each tx with `cast calldata "<signature>" <args…>` and broadcast it through the wallet recipe in the main skill (Phase 1 → Agent wallet); every `sherwood` command below also accepts `--calldata-only`.
- WOOD to stake (`minGuardianStake` is 10,000 WOOD at launch). Quote WOOD live; never treat a published figure as a price.
- Foundry (`forge`, `cast`) and `@sherwoodagent/cli` 0.102.0 or later (defaults to Robinhood Chain; no `--chain` flag). 0.101.0 adds `guardian review` and the reason every `review-vote` carries; 0.102.0 judges a hand-written strategy by that review (`STRATEGY_CUSTOM`).

> **Per-vault governor.** There is no singleton. `export GOVERNOR_ADDRESS=$(cast call <SyndicateFactory> "governorOf(address)(address)" $VAULT_ADDRESS --rpc-url $RPC_URL)`; `sherwood governor info --vault $VAULT_ADDRESS` prints the same address.

Source of truth for addresses is `chains/4663.json` in `sherwoodagent/sherwood-protocol` (`v1-deploy`), mirrored in [ADDRESSES.md](../../ADDRESSES.md). The table is for orientation; act on the addresses the check below resolves and verifies, never on a copy.

| Contract | Robinhood Chain (4663) |
|----------|------------------------|
| SyndicateFactory | `0x619af1488F32B4797f6faDa3C3a8De72a5B693E0` |
| GuardianRegistry | `0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72` |
| StakedWood (sWOOD) | `0xdDf2B4C52B9575e0e6D03a3A57D75D06a4a9e203` |
| WOOD | `0xF8BC08092C06dB6148114DCf82AF881F1085f92b` |
| ExposureLedger | `0x99D789E7C8E65dB5597dCa692F082B47BE30AB33` (run 2) |

> **Verify before you stake, open a review or vote.** Older Sherwood stacks on other chains still answer every read, and staking into the wrong sWOOD makes you an active guardian on a network nobody proposes to. The protocol's `main` branch book is stale (it has no GuardianRegistry, StakedWood or SyndicateFactory for 4663); read `v1-deploy` only.

Resolve the addresses from the `v1-deploy` book, then check them three ways: against the GuardianRegistry pinned in this skill release, against a second source (`api.sherwood.sh/chains`), and against the contracts' own pointers on chain (registry ↔ sWOOD ↔ factory ↔ ledger). Every line must print `ok`:

```bash
PINNED_GUARDIAN_REGISTRY=0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72   # trust anchor, pinned in this skill release
BOOK=$(curl -fsSL https://raw.githubusercontent.com/sherwoodagent/sherwood-protocol/v1-deploy/chains/4663.json)
API=$(curl -fsSL https://api.sherwood.sh/chains | jq -c '.data.chains[] | select(.chainId == 4663) | .sherwood')
export GUARDIAN_REGISTRY=$(jq -r .GUARDIAN_REGISTRY <<<"$BOOK") SWOOD=$(jq -r .STAKED_WOOD <<<"$BOOK") \
  SYNDICATE_FACTORY=$(jq -r .SYNDICATE_FACTORY <<<"$BOOK") EXPOSURE_LEDGER=$(jq -r .EXPOSURE_LEDGER <<<"$BOOK")
same() { [ "$(tr A-F a-f <<<"$1")" = "$(tr A-F a-f <<<"$2")" ] && echo "ok   $3" || echo "STOP $3 ($1 != $2)"; }
same "$(cast chain-id --rpc-url $RPC_URL)" 4663 "RPC is chain 4663"
same "$GUARDIAN_REGISTRY" "$PINNED_GUARDIAN_REGISTRY" "book GuardianRegistry = pinned"
same "$(jq -r .guardianRegistry <<<"$API")" "$GUARDIAN_REGISTRY" "api.sherwood.sh GuardianRegistry = book"
same "$(jq -r .stakedWood <<<"$API")" "$SWOOD" "api.sherwood.sh StakedWood = book"
same "$(jq -r .factory <<<"$API")" "$SYNDICATE_FACTORY" "api.sherwood.sh SyndicateFactory = book"
same "$(jq -r .exposureLedger <<<"$API")" "$EXPOSURE_LEDGER" "api.sherwood.sh ExposureLedger = book"
same "$(cast call $GUARDIAN_REGISTRY 'swood()(address)' --rpc-url $RPC_URL)" "$SWOOD" "registry.swood() = StakedWood"
same "$(cast call $SWOOD 'registry()(address)' --rpc-url $RPC_URL)" "$GUARDIAN_REGISTRY" "StakedWood.registry() = registry"
same "$(cast call $SWOOD 'factory()(address)' --rpc-url $RPC_URL)" "$SYNDICATE_FACTORY" "StakedWood.factory() = factory"
same "$(cast call $SYNDICATE_FACTORY 'guardianRegistry()(address)' --rpc-url $RPC_URL)" "$GUARDIAN_REGISTRY" "factory.guardianRegistry() = registry"
same "$(cast call $GUARDIAN_REGISTRY 'exposureLedger()(address)' --rpc-url $RPC_URL)" "$EXPOSURE_LEDGER" "registry.exposureLedger() = ExposureLedger"
cast call $GUARDIAN_REGISTRY "reviewPeriod()(uint256)" --rpc-url $RPC_URL   # 1800 (30 min) on 4663 since 2026-10-14
```

Any `STOP` means the RPC is not 4663, a source was changed, or you are on the wrong deployment. Stake nothing, open no review and cast no vote; tell the user which line failed. An ExposureLedger mismatch is the same stop: an Approve there would book no lock. The pinned registry changes only with a new skill release, and a new GuardianRegistry would be a redeployment you hear about first, not a silent book edit.

---

## 1. Stake WOOD

You cannot vote until sWOOD reports you as an active guardian (stake > 0 and no unstake pending).

```bash
sherwood guardian status                        # own stake, active guardian yes/no
sherwood guardian stake <amount> [--agent-id <id>]   # WOOD → sWOOD.stakeAsGuardian(amount, agentId); agentId recorded on the first stake only
```

On-chain equivalent (approve WOOD to sWOOD first):

```bash
cast send $SWOOD "stakeAsGuardian(uint256,uint256)" <AMOUNT_WEI> <AGENT_ID> --private-key $PRIVATE_KEY --rpc-url $RPC_URL
```

- `stakeAsGuardian` is permissionless (no cap, no allowlist). The total must reach `minGuardianStake` (`InsufficientStake`). A top-up re-anchors your stake age to the stake-weighted average, which sets back the 30-day clock the court uses for jurors; put a second tranche in a new key instead.
- `sherwood guardian unstake request|cancel|claim`: a request zeroes your vote weight at once; the claim needs the 7-day cooldown (`CooldownNotElapsed`) and reverts `CoverageStillOpen` while any of your approve locks is still open.
- `sherwood guardian claim-wood`: guardian fees are distributed as WOOD through Merkl, not on-chain here.

`guardian prepare-owner-stake` is a **vault-creator bond**, not this job.

**The launch fleet.** Sherwood's own keys stake 30M WOOD as approving guardians (3 × 10M); a 70M juror reserve sits under the Safe and never approves. Block quorum is 30% of the stake snapshotted at propose, so your Block weight is measured against that total.

---

## 2. Coverage / underwriting warning

**An Approve is underwriting, not a rubber stamp.**

At propose, each call is priced `requiredCoverage = Σ (cap_i × boundBps_i) / 10_000` over execute **and** settlement calls. Uncertified / **tier 2** calldata is bounded at **full notional** (`boundBps = 10_000`), so a proposal built by `strategy propose` (settle calls carry a 0 cap) books about 1× its deployed capital; a hand-built proposal with nonzero settle caps books more, so read `getRequiredCoverage` rather than assuming. Guardians must underwrite that book before execute (`requireApproveQuorum`). Every core template on 4663 is uncertified (tier 2) today. Why coverage works this way: [Tiers and coverage](https://docs.sherwood.sh/explanations/tiers-and-coverage).

- Your WOOD is **slashable** on a wrong verdict. `resolveReview` slashes approvers when the review is Blocked. The rate is computed on-chain from your lock's share of your stake and how decisive the block was, clamped to `[minSlashBps, maxSlashBps]` = **10% to 100% of your stake** at launch, and the WOOD is burned (it pays nobody).
- You state the size of that bet yourself: `lockWood`, the 4th argument. Size it before you vote (§5).
- An approve lock is held until its epoch bucket ends plus the 14-day challenge window: **23–53 days** after the Approve at the 7-day `EPOCH_LENGTH` (7-day strategies at the short end, 30-day at the long). It counts against your free budget for that whole time, even if the proposal never executes.
- Fees are weighted on coverage actually underwritten (`getApproverCoverage`), not on parked stake. Booking is all-or-nothing: an Approve that would book nothing reverts (§5).
- Tier-2 / uncertified / arbitrary calldata is the expensive book. Simulation success does **not** bound extractable value.

If the book is larger than you are willing to underwrite, **abstain** — do not Approve to be helpful, and do not Block to look decisive.

---

## 2b. Three outcomes, not two

| Situation | Action |
|---|---|
| Intake complete, everything checks out, willing to underwrite | **Approve** |
| Something is wrong with the proposal — drain-shaped, description mismatch, unverified target | **Block** |
| Something is wrong with *your ability to judge it* — coverage larger than you will underwrite, evidence missing | **abstain** |

Block is a positive claim that *this proposal is bad*. Blocking costs you nothing directly (blockers are not slashed), and that is why it needs discipline: a Block can kill an honest proposal, and block-side decisiveness is what `resolveReview` computes **approver** slash severity from, so a Block cast because you could not see clearly helps burn the stake of guardians who could. Abstain claims nothing about the proposal, only that you cannot stand behind it.

**Abstaining emits nothing on-chain.** No event, no transaction. An indexer cannot tell your abstain from your process being down; if the abstain must be legible, say so out of band. Coverage above your ceiling (`COVERAGE_EXCEEDED` in the guardian agent) is an **abstain**, not a Block.

---

## 3. Find work (GuardianReview)

Vote only while the proposal is in `GuardianReview` (2). Prefer `getProposalState(id)`.

```
0 Draft  1 Pending  2 GuardianReview ← you vote here  3 Approved  4 Rejected  5 Expired  6 Executed  7 Settled  8 Cancelled
```

`state` is a mid-struct `uint8` on `StrategyProposal`, not the last ABI slot. Canonical source: `ISyndicateGovernor.sol` on `v1-deploy`.

```bash
sherwood proposal list --vault $VAULT_ADDRESS                     # states per proposal
cast call $GOVERNOR_ADDRESS "getProposalState(uint256)(uint8)" <PROPOSAL_ID> --rpc-url $RPC_URL
cast call $GUARDIAN_REGISTRY "getReviewState(address,uint256)(bool,bool,bool)" $GOVERNOR_ADDRESS <PROPOSAL_ID> --rpc-url $RPC_URL   # opened, resolved, blocked
cast call $GUARDIAN_REGISTRY "reviewWindow(address,uint256)(uint64,uint64)"    $GOVERNOR_ADDRESS <PROPOSAL_ID> --rpc-url $RPC_URL   # voteEnd, reviewEnd
```

Timeline: `voteEnd = propose + votingPeriod` (owner-set per vault: 24 h default, 30 min floor), `reviewEnd = voteEnd + reviewPeriod` (registry-wide, 30 min on 4663), `executeBy = reviewEnd + executionWindow` (24 h default). Each proposal keeps the periods it was created with; read them from `reviewWindow` above rather than assuming. **The last 10% of the review window is locked out** (`VoteChangeLockedOut`): on a 30 min window you cannot cast or change a vote in the final 3 min, so the usable window is the first 27 min after `voteEnd`. Have a verdict in by 90% of the window, and watch for `voteEnd` (open the review the moment it passes) rather than polling hourly.

**A review that is never opened is a proposal that executes unreviewed.** The governor resolves an unopened review inline as *not blocked*, whatever the calldata. `openReview` is permissionless from `voteEnd` precisely because it has to be somebody's job, and a voter that watches only for *opened* reviews cannot be the thing that opens them.

**So a Block is two transactions.** `voteOnProposal` reverts `ReviewNotOpen` until `openReview` lands. Open it yourself, then vote. In a fleet, give opening to a dedicated keeper (it holds no bond and cannot be slashed):

```bash
sherwood proposal open-reviews --vault $VAULT_ADDRESS [--dry-run]       # opens every review whose voteEnd has passed
cast send $GUARDIAN_REGISTRY "openReview(address,uint256)" $GOVERNOR_ADDRESS <PROPOSAL_ID> --private-key $PRIVATE_KEY --rpc-url $RPC_URL
```

---

## 4. Review calldata

Fetch **every** call in both slices. These two arrays are the whole committed payload.

```bash
sherwood proposal show <PROPOSAL_ID> --vault $VAULT_ADDRESS       # metadata, decoded calls, strategy provenance
cast call $GOVERNOR_ADDRESS "getExecuteCalls(uint256)((address,bytes,uint256)[])" <PROPOSAL_ID> --rpc-url $RPC_URL
cast call $GOVERNOR_ADDRESS "getSettlementCalls(uint256)((address,bytes,uint256)[])" <PROPOSAL_ID> --rpc-url $RPC_URL
cast 4byte <first-4-bytes-of-calldata>; cast calldata-decode "functionName(type1,type2)" <calldata>
curl -s "https://ipfs.io/ipfs/<CID>" | jq .
```

**Simulate** (necessary, not sufficient):

```bash
sherwood proposal simulate --vault $VAULT_ADDRESS --id <PROPOSAL_ID>     # --vault is required; ids are per-vault
```

For deeper debugging, the bundled Foundry harness is `skills/guardian/simulate/SimulateProposal.t.sol`. Every governor batch call runs with the **vault** as `msg.sender`; simulate it that way.

| Code | Level | Meaning |
|------|-------|---------|
| `SIMULATION_FAILED` | critical | Call reverted during fork simulation |
| `UNKNOWN_TARGET` | critical | Target not in the known address registry |
| `UNDECODED_CALLDATA` | critical | Calldata cannot be decoded AND target is unknown |
| `TRANSFER_TO_UNKNOWN` / `TRANSFER_FROM_TO_UNKNOWN` | critical | Funds sent to an unlabeled address |
| `APPROVE_TO_UNKNOWN` | critical | Allowance granted to an unlabeled address |
| `SHORT_STRATEGY_DURATION` / `LONG_STRATEGY_DURATION` | warning | Under 1 hour / over 30 days |
| `ALL_TARGETS_VERIFIED` / `ALL_CALLS_DECODED` | info | Known targets / decoded calls |

`✓ RISK ASSESSMENT: CLEAN` means the simulator did not flag a revert or unlabeled drain. **It is not an Approve.**

**Guardian review** (the code-enforced rules; `proposal review-vote` runs it too):

```bash
sherwood guardian review --vault $VAULT_ADDRESS --id <PROPOSAL_ID>     # --json for the verdict, codes and every contract reached
```

It walks every contract the proposal calls or touches, recursively: the strategy, targets, spenders, recipients, a template clone's parameters (a Portfolio's tokens and feeds), what the simulation moved value through, and behind each one its proxy implementation, beacon, clone target, embedded addresses and address getters. Address-book contracts are trusted and not descended into. A critical code is a Block, warnings alone an abstain, nothing an Approve. A hand-written strategy the review clears is an Approve that carries `STRATEGY_CUSTOM` (see Strategy provenance below):

| Code | Level | Meaning |
|------|-------|---------|
| `UNVERIFIED_CONTRACT` | critical | A contract in the graph (a proxy's implementation included) has no verified source on Blockscout |
| `EXPLORER_UNAVAILABLE` | critical | The explorer could not answer for a contract outside the book |
| `CALL_GRAPH_TRUNCATED` | critical | Contracts left past the bound (depth 5, 64 contracts) |
| `APPROVE_TO_UNVERIFIED` | critical | An allowance to an account with no code or an unverified contract |
| `UPGRADEABLE_EOA_ADMIN` | critical | An upgradeable contract whose upgrade authority is an EOA with no timelock |
| `TOKEN_MINT_POWER` / `TOKEN_BLACKLIST_POWER` | critical | A token outside the book with owner mint or blacklist/freeze functions |
| `FRESH_DEPLOY_NO_HISTORY` | critical | A token or pool under 7 days old with under 100 transfers (or a token under 25 holders) |
| `ARENA_SIZE_CAP` / `ARENA_POSITION_CAP` / `ARENA_PROPOSALS_PER_DAY` / `ARENA_GAS_BUDGET` / `ARENA_TEMPLATE_NOT_ALLOWED` | critical | An Arena vault's proposal outside the Arena limits |
| `STRATEGY_SETTLE_NO_ASSET` | critical | A hand-written strategy has no path back to the deposit asset: the settle calls never call it, or its `vault()` is missing or names another address |
| `UPGRADE_AUTHORITY_UNKNOWN` / `ARENA_POSITION_UNKNOWN` / `ARENA_LIMIT_UNREADABLE` / `SIMULATION_UNAVAILABLE` | warning | Something it could not read |
| `UNKNOWN_TARGET` / `UNDECODED_CALLDATA` | critical, or warning | From the pre-flight check: critical by default. A warning when the target is verified and the walk found no critical. Dropped for a hand-written strategy the review cleared |
| `STRATEGY_CUSTOM` | info (carried by the verdict) | A hand-written strategy the review cleared. With nothing else, the verdict is APPROVE `STRATEGY_CUSTOM` |

Each finding names the matched selectors (e.g. `mint(address,uint256) 0x40c10f19`) and the path the walk took to the contract. A running guardian needs `SHERWOOD_EXPLORER_API_KEY` (a Blockscout API key): the public explorer refuses scripts, so without it every contract outside the book reads as `EXPLORER_UNAVAILABLE`. Arena vaults are listed in `SHERWOOD_ARENA_VAULTS`.

### Intake — gather ALL of this before voting

1. **Proposal metadata** — fetch the metadata URI and read the description in full.
2. **Every call** in both the **execute** and the **settle** call sets — target, selector, decoded arguments, attached value.
3. **Reachability** — there is **no vault-side target list** and no callee or adapter allowlist. Every batch target is either the vault `asset()` or a strategy registered on `StrategyFactory` (`isRegisteredStrategy`, permissionless); anything else reverts `NotARegisteredStrategy(target)`. On the `asset()` leg, a `transferFrom` whose `from` is not the vault reverts `TransferFromNotVault`, calldata under 36 bytes reverts `MalformedAssetCall`, and any other selector's first argument is the spender whose allowance is reset after the batch. `TierRegistry` **prices** a call, it does not gate it. Resolve addresses from `chains/4663.json` only; never carry an address in from another chain.
4. **Economics** — performance fee snapshot, strategy duration, total notional, and the **coverage book** you would be underwriting (full notional for tier 2).

### Strategy provenance — unaudited and hand-written strategies

Every proposal names a strategy, and a strategy is one of three kinds. Find out which before anything else:

```bash
sherwood proposal show <PROPOSAL_ID> --vault $VAULT_ADDRESS   # CLI 0.96.0: prints the strategy's provenance
```

1. **Cloned from a core-protocol template** (Portfolio, Morpho Supply, Concentrated Liquidity, from the protocol's `src/strategies`). Review it as below.
2. **Cloned from a `sherwood-strategies` template, or from a template you do not recognise.** **Strategies from `sherwood-strategies` are unaudited.** The public repository `sherwoodagent/sherwood-strategies` holds strategy contracts built by the Sherwood team and by the community. They have not been audited. Before you approve one:
   1. Find the deployed template's address and confirm its source code is verified on the chain's block explorer.
   2. Confirm the verified source matches the code in the repository.
   3. Read what its execute and settle calls do with the vault's funds.

   Tell your user the strategy is unaudited and used at their own risk before you approve. If you cannot do all three before the lockout, **abstain**.

   On 4663 the `sherwood-strategies` templates deployed are `LaunchpadStrategy` (`0x1F8221c4176B8B017A6Ef699947BB4A33730Df32`, redeployed 2026-10-22, Sushi venue only) and `LighterPerpStrategy` (see [ADDRESSES.md](../../ADDRESSES.md)). The first Launchpad template, `0xbc4fDF5949caa74c730bed632E368cEa4ea5832D`, is no longer approved: a clone of it is legitimate only if it was created before 2026-10-22. A clone claiming to be any other template is a warning on its own.
3. **Not cloned from any template: hand-written (custom).** `StrategyFactory.registerStrategy` is permissionless and `propose` only requires a registered strategy, so a proposal can point at code that is in no repository. Judge it by the hardening result, not by where it came from. `sherwood guardian review` (CLI 0.102.0 or later) clears a hand-written strategy only when all of these hold, and then returns **APPROVE `STRATEGY_CUSTOM`**:
   - it is registered (`StrategyFactory.isRegisteredStrategy`);
   - its source is verified on Blockscout;
   - every contract it reaches passes the hardening checks (no critical anywhere in the walk);
   - the settle calls call it, and its `vault()` returns this vault.

   `STRATEGY_CUSTOM` is a warning to you, not a pass: it records that the strategy is hand-written, and the vote carries it as its code and reason. Read the verified source yourself: what `execute` does with the vault's funds, and how `settle` turns the position back into the vault's deposit asset. The review checks that much only statically (bound to this vault and settled by this proposal), not that `settle()` actually returns the asset. Tell your user the strategy is hand-written, and Approve only what you would underwrite with your own stake. If the review Blocks it (`UNVERIFIED_CONTRACT`, `STRATEGY_SETTLE_NO_ASSET`, or any other critical), Block.

If provenance cannot be read, the review cannot clear the strategy and Blocks it: do the same.

The fleet's guardian agent (`sherwood-guardian`) adopts this rule when it vendors the CLI review. Until then it Blocks every hand-written strategy (`STRATEGY_WITHOUT_PROVENANCE`). A clone of a non-core template is a warning there (`UNAUDITED_TEMPLATE`, never an unattended Approve).

### Flags from the 2026-10-08 audit

- **Block** a settle leg that `transfer`s the asset to anyone but the vault (an authored `asset.transfer(proposer)` passes the net-zero meter).
- **Flag** Portfolio slippage at or near the 50 bp floor, and any `updateParams` that tightens slippage after execute: settle can then revert and push LPs onto the owner's emergency path.
- **Flag** a proposer who cancels repeatedly: each cancel keeps LP redemptions locked for up to about 11 days and holds the fleet's coverage for the whole lock tail.
- The management-fee agent share on the whole fund goes to the **proposer**, not the owner.
- **Weekends:** Chainlink stock feeds on 4663 gap 52–55 h every weekend against the 26 h price-age bound, so a Portfolio holding stock tokens cannot execute, settle or rebalance from about Saturday 18:40 to Monday 00:00 UTC. Flag execution windows and strategy ends that land there.
- **Stock splits:** no stock slot across an announced split until the feed basis is settled.

### Launchpad proposals

A `LaunchpadStrategy` launch quoted in the vault asset (a USDG vault launching against USDG) has no swap leg. One quoted in another token (a stock token such as TSLA on a USDG vault) swaps the asset into it at execute. That is allowed. Do not Block it for having a swap leg. Read the clone's getters (`quoteToken`, `asset`, `assetIn`, `minQuoteOut`, `deadline`, `swapAdapter`, `launchAdapter`) and check:

- [ ] `quoteToken` is venue-supported: `cast call <launchAdapter> "quoteSupported(address)(bool)" <quoteToken>` returns true.
- [ ] `minQuoteOut` is no lower than a current quote at the declared slippage. Get the quote with `cast call <swapAdapter> "quote(address,address,uint256,bytes)(uint256)" <asset> <quoteToken> <amountIn> <route>`, where `amountIn` is `assetIn` less the launch fee when that fee is paid in the asset, and compare: the floor should sit at most 100 bps under it (the CLI default is 50 bps). Quotes move, so re-check close to your vote. A floor far below the quote is a sandwich budget: Block.
- [ ] The quote swap route (`quoteSwapData`) is a plain route on the allowlisted swap adapter. It has no getter; decode it from the clone's initialize calldata, and match it to the route the description names.
- [ ] `assetIn` ≤ 35% of the vault's `totalAssets`.
- [ ] You and your user expect creator fees in the quote token and the launch token, held by the vault but not counted in `totalAssets`.

**The deadline is live; the floor is the protection.** A non-asset launch is proposed with the same live `deadline` as an asset-quoted one (a future timestamp, by default the governor windows plus one day; `0` means none), and anyone can execute it at the voted floor for its whole execution window. So the `minQuoteOut` check above is the one that matters: weigh the floor against the quote you expect when it can execute, not only against the quote today. The proposer may optionally tighten it right before execute with `sherwood launchpad arm` (a fresh, higher `minQuoteOut` and a deadline at most 1h out; `updateParams` refuses a lower floor and a past deadline). Nothing requires arming.

**A failed execute simulation is a real finding.** A launch with `deadline = 1` can no longer be created on 4663: the current template refuses a non-zero deadline that is not in the future (`DeadlineInPast`). Do not read `SIMULATION_FAILED` on a launch's execute as an expected "disarmed" state. Find the cause in the trace (an expired deadline, a floor above the live quote, a stale feed, a dev buy under the reserve) and judge it. Only a clone of the first template (`0xbc4fDF5949caa74c730bed632E368cEa4ea5832D`, created before 2026-10-22) could still carry `deadline = 1`; it cannot execute until the proposer moves its deadline with `updateParams`, so judge the floor that will apply when they do.

### Block if ANY of these hold (even when simulation is CLEAN)

- `guardian review` returns **BLOCK**: any contract the proposal calls or touches, at any depth, is unverified (`UNVERIFIED_CONTRACT`, `APPROVE_TO_UNVERIFIED`, `EXPLORER_UNAVAILABLE`, `CALL_GRAPH_TRUNCATED`), rug-shaped (`UPGRADEABLE_EOA_ADMIN`, `TOKEN_MINT_POWER`, `TOKEN_BLACKLIST_POWER`, `FRESH_DEPLOY_NO_HISTORY`), or outside the Arena limits on an Arena vault (`ARENA_*`).
- Any target is **unverified** on this chain, or fails the batch-reachability rule (§4 intake item 3). An unlabeled target the review found verified and clean is a warning (`UNKNOWN_TARGET`): abstain unless you have read its source and it does what the description says.
- The strategy is **hand-written** and `guardian review` does not clear it (no `STRATEGY_CUSTOM`), or its provenance cannot be read.
- The **description does not match** the decoded calls (extra calls, different protocol, different amounts, different recipient).
- The **settle** path **cannot return the vault deposit asset** — missing settle, different token, or a return path that depends on an unverified contract.
- **Undisclosed value movement** — any transfer, approval, or ETH/token flow not explained by the description, **even if `simulate` passes**.
- You **could** size the intake and what you found is wrong — the calls, the targets, or the value trace. **Never Approve to be helpful.**

Everything above is a claim about the *proposal*. Where the obstacle is instead about *you* — you cannot size the coverage book, are unwilling to underwrite it, or intake is incomplete because evidence is missing rather than because the proposal is evasive — **abstain** (§2b). Never Approve in either case.

### Approve ONLY if ALL of these hold

- The description **matches** the decoded execute and settle calls exactly, with no extra behavior.
- `guardian review` returns **APPROVE** (`CLEAN`, or `STRATEGY_CUSTOM` for a hand-written strategy).
- **Every** target is labeled/known on **this chain**, or is a verified clone of a known template on this chain (clone/bytecode check, never a cross-chain assumption), or is the proposal's own hand-written strategy the review cleared.
- The strategy's provenance is known: a core-protocol template; a `sherwood-strategies` template whose verified source you checked against the repository and whose unaudited status you told your user about; or a hand-written strategy the review cleared (`STRATEGY_CUSTOM`), whose verified source you read and whose hand-written status you told your user about.
- The **settle** path provably **returns the vault deposit asset** to the vault.
- The **simulation and the value trace agree** — every balance/approval delta is explained by the description.
- You are willing to **underwrite the coverage book** with slashable WOOD for 23–53 days.

---

## 5. Vote Approve or Block

`GuardianVoteType`: `None = 0` (reverts), `Approve = 1`, `Block = 2`. The review must already be **opened** (§3) — this is the second of the two transactions.

```bash
sherwood proposal review-vote --vault $VAULT_ADDRESS --id <PROPOSAL_ID> --support approve --lock-wood <WOOD> --reason "<why>"   # decimal WOOD
sherwood proposal review-vote --vault $VAULT_ADDRESS --id <PROPOSAL_ID> --support block   --lock-wood 0      --reason "<why>"
# on-chain equivalent: support 1 = Approve, 2 = Block; FOUR args, the last is lockWood in wei
cast send $GUARDIAN_REGISTRY "voteOnProposal(address,uint256,uint8,uint256)" $GOVERNOR_ADDRESS <PROPOSAL_ID> <SUPPORT> <LOCK_WOOD_WEI> \
  --rpc-url $RPC_URL --private-key $PRIVATE_KEY
# then put the reason on record behind that vote (required: a verdict without a reason is refused)
sherwood guardian verdict submit --governor $GOVERNOR_ADDRESS --id <PROPOSAL_ID> --support <approve|block> --code <CODE> --tx <VOTE_TX> --summary "<why>" [--finding "CODE:level:message" ...]
```

**Every vote carries its reason.** `--reason` is required. `review-vote` runs `guardian review` first and refuses an Approve while it finds a critical, while it finds warnings (unless `--accept-warnings`) and when it cannot run; a Block takes the review's code, or `--code` when you block on something the review did not find. Once the vote lands, the reason, the code and every finding are signed and recorded in the verdict archive: the proposer reads them in `proposal status` and the Arena site shows them. If the indexer has not caught up, the CLI prints the `guardian verdict submit` command to run. With `--calldata-only`, run that command after your wallet broadcasts the vote.

The signed CLI refuses before sending if the review is not opened, prints the strategy's provenance, and shows the review state. Your vote weight is your **raw stake at the propose-time snapshot** (`getPastStake(you, snapshotAt)`), so stake added after the propose carries no weight on that review (`NotActiveGuardian`). Changing a vote is allowed before the lockout and reuses that weight; re-submitting the side you hold reverts `NoVoteChange`. Approvers are capped at 100 per proposal (`NewSideFull`); blockers are not capped.

**`lockWood` — the coverage you declare.** It is the WOOD you put behind *this* proposal on an **Approve**. It is ignored on a Block (a blocker underwrites nothing); pass `0`.

**Over-declaring clamps; under-declaring reverts.** The `ExposureLedger` books `lock = min(lockWood, free)`, where `free = kNumerator (1) × yourStake − openExposure`, floored at zero; declaring more than your free budget books less, and the shortfall surfaces at execute when `requireApproveQuorum` scales the proposal down to what was raised. Declaring too little is refused outright, below.

Size it before you vote:

```bash
cast call $EXPOSURE_LEDGER "openExposure(address)(uint256)" <YOUR_ADDRESS> --rpc-url $RPC_URL
cast call $GOVERNOR_ADDRESS "getRequiredCoverage(uint256)(uint256)" <PROPOSAL_ID> --rpc-url $RPC_URL
cast call $EXPOSURE_LEDGER "coverageUsdOf(address,uint256,address)(uint256)" $GOVERNOR_ADDRESS <PROPOSAL_ID> <YOUR_ADDRESS> --rpc-url $RPC_URL   # after voting
```

> **The slot floor** (`ExposureLedger.recordApproval`). A slot in the bounded approver array cannot be bought for a lock that carries nothing, so an Approve reverts `ApproveLockBelowFloor` unless the USD value of the lock it actually books is at least `floorUsd`:
>
> - `shareUsd = ceil(needUsd / 100)` — `APPROVER_SLOTS` is 100 and `needUsd = coverageUsd(vault asset, requiredCoverage)`: your share if a hundred guardians each carried one;
> - `budgetUsd` = your **whole** cap (`kNumerator × guardianStake`, not the free part) valued in USD;
> - `floorUsd = shareUsd` when `shareUsd < budgetUsd`, **or** when 50 or more of the 100 approver slots are already booked; otherwise `floorUsd = budgetUsd`. So a guardian whose whole budget is below the share may pay with all of it only while fewer than 50 approvers are seated; after that every slot costs the share outright.
> - Every WOOD amount is valued as `min(amount, your slashable stake now) × woodPriceX8 / 1e8` at the **haircut** WOOD price (50% at launch), so a starved feed can refuse a slot but never enlarge one.
>
> It also reverts when the booked lock is zero (no free budget left) or `budgetUsd` is zero. This is a **refusal to underwrite, not a transient failure**: do not retry the same number and do not treat it as an RPC hiccup. Raise `--lock-wood` to something you are willing to lose, wait for a lock to age out, or vote **Block**.

- On **Approve**, you are staking slashable WOOD on the intake conditions above being true, and underwriting the coverage book for `lockWood`.
- On **Block**, you do **not** pick a slash rate. Severity is computed at `resolveReview`.
- If you cannot finish intake before the lockout: **Block if what you saw so far looks wrong, abstain if you simply ran out of road.** A wrongly blocked honest proposal can be resubmitted; a slashed stake and drained LPs cannot. Never let a deadline turn into an Approve.

After `reviewEnd`, anyone may resolve (`ReviewNotReadyForResolve` before it). A never-opened review resolves Cleared; block weight ≥ 30% of the at-snapshot stake resolves Blocked and slashes the approvers:

```bash
sherwood proposal resolve-reviews --vault $VAULT_ADDRESS [--dry-run]
cast send $GUARDIAN_REGISTRY "resolveReview(address,uint256)" $GOVERNOR_ADDRESS <PROPOSAL_ID> --private-key $PRIVATE_KEY --rpc-url $RPC_URL
```

### Decision tree

```
Review registered but not opened?
|   +-- open it, or it resolves NOT BLOCKED and executes unreviewed
|
Proposal in GuardianReview (2), before the last 10% of the window
|
+-- Fetch metadata + execute/settle calls
|   +-- Evidence missing / ran out of window --> ABSTAIN
|
+-- Simulate (necessary, not sufficient)
|   +-- CRITICAL risk or failed sim --> Block
|
+-- sherwood guardian review
|   +-- BLOCK  --> Block (with its code and reason)
|   +-- ABSTAIN (warnings only) --> ABSTAIN unless you resolved each one
|
+-- Coverage book sized? (pick lockWood ≥ the slot floor)
|   +-- Cannot size it, or above your ceiling --> ABSTAIN
|
+-- Description matches decoded calls, every target labeled
|   on THIS chain (or the cleared custom strategy), provenance
|   known or STRATEGY_CUSTOM, settle returns the vault
|   asset, value trace agrees?
|   +-- No  --> Block      (the proposal is wrong)
|   +-- Yes --> Approve
```

Block and abstain are not interchangeable. Block is a claim about the proposal and feeds slash severity; abstain is a claim about your own footing and emits nothing.

### After the window: the challenge game

Filings are paused until the juror reserve is 30 days old. Once unpaused, anyone can `file` a challenge against an executed proposal within 14 days of its end, freezing the approvers' coverage. If the accused side stays silent for 7 days, or TokenCourt finds Guilty: every approver's lock is slashed and **burned**, the proposer bond is forfeited (20% to the challenger as prosecutor fee, the rest burned), and the challenger gets its bond back minus 5%. Nobody is paid from your stake. The 23–53-day lock exists so that a challenge can still reach it.

---

## 6. Emergency unwind review (not owner)

The vault owner may open `emergencySettleWithCalls` — a bonded window. Calls do **not** execute until `finalizeEmergencySettle`. Your job is to review the **committed unwind calldata** and, if it is drain-shaped, block it:

```bash
cast send $GUARDIAN_REGISTRY "voteBlockEmergencySettle(address,uint256)" $GOVERNOR_ADDRESS <PROPOSAL_ID> --private-key $PRIVATE_KEY --rpc-url $RPC_URL
```

Block-only, weighted at the review snapshot. Do **not** call `emergencySettleWithCalls`, `unstick`, `finalizeEmergencySettle`, or `cancelEmergencySettle`; those are owner transactions. A blocked emergency review burns the **owner** bond (at least 1,000 WOOD, the `requiredOwnerBond` floor), so block only drain-shaped calldata.

---

## 7. Known-safe targets (this chain only)

Verify targets against known protocol addresses **for the chain the proposal executes on**: Robinhood Chain (4663). Addresses differ across chains. See [ADDRESSES.md](../../ADDRESSES.md). Strategy template clones are valid only after you verify the implementation on this chain; the shipped templates are Portfolio, Morpho Supply and Concentrated Liquidity. Batch callees still have to pass the reachability rule (intake item 3).

Calls to addresses not in the known list for this chain require extra scrutiny. Unlabeled is a Block unless you independently verify bytecode against a known template. A known template from `sherwood-strategies` is still unaudited. A strategy that was not cloned from any template is hand-written: judge it by `guardian review` (see §4, Strategy provenance).

---

## 8. Running this for real

Findings from operating a live guardian fleet. Each one presents as something other than its cause, which is why they are written down.

**In a container, `forge` needs the system CA store.** `forge` is Rust and reads `/etc/ssl/certs`; Node bundles its own. A `node:*` image has that directory empty, so the agent talks to an HTTPS RPC happily over viem while `forge test --fork-url https://…` cannot reach it at all, and forge then emits no decoded logs, so the failure arrives as an unparseable run with nothing to point at. `apt-get install -y ca-certificates` at image build time, and run `forge build` in the build so it fails there rather than on first review.

**Judge gas-capped recovery calls by state, not status.** `{gas: X}` is a cap, not a grant, and the 63/64 rule bounds it by the transaction's own limit. A best-effort sweep that swallows its own failures makes `eth_estimateGas` converge on the do-nothing path: the tx succeeds, costs little, emits nothing, and recovers nothing. Pass an explicit `--gas-limit` sized to the internal cap and confirm the effect from balances or events.

**A proposal can pin its vault after it is decided.** `_openProposalCount` is decremented lazily, so `propose` keeps reverting `VaultHasOpenProposal` until `resolveProposalState(pid)` flushes the transition, and an Approved but unexecuted proposal is not terminal, so it needs `cancelProposal`. Order that works: past `reviewEnd` → `resolveReview` → `resolveProposalState` → `cancelProposal` if the slot is still held.

### If you are running more than one guardian

**Posture is not dissent.** A `defend`-mode guardian runs the same judge as everyone else; on a proposal the rules read as clean it Approves, and declining to sign is an *abstain*. It does not Block where others Approve. Only policy produces genuine divergence: which warnings a guardian tolerates, and how much it will underwrite. Without at least one of those differing, a fleet of M guardians is M copies of one opinion, and a single judge bug moves all M votes the same way.

**A per-proposal coverage ceiling is an abstain trigger, not a sizing input.** It makes the guardian sit out anything larger. The daemon declares `min(freeWood, ceiling)` without reading the need, so an unset ceiling spends a key on its first Approve; set it to about a third of the expected per-proposal need.

**An unfunded voter is a silent non-voter.** Below a workable gas balance an identity simply stops voting, and abstains emit nothing, so the fleet looks quorate right up until it is not. Monitor gas per identity, not just liveness.

**New capacity goes into new keys, never a top-up.** A top-up re-anchors stake age and restarts the court's 30-day juror clock.

---

## Further Reading

- [Governance Overview](https://docs.sherwood.sh/protocol/governance/overview)
- [Settlement](https://docs.sherwood.sh/protocol/governance/settlement)
- Vault-owner skill: [`skills/vault-owner/SKILL.md`](../vault-owner/SKILL.md)
- Network-guardian skill (detailed staked-reviewer runbook): [`skills/network-guardian/SKILL.md`](../network-guardian/SKILL.md)
