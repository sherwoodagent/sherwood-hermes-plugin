---
name: network-guardian
description: Operate as a staked Sherwood network guardian on Robinhood Chain (4663) — open a proposal's guardian review, gather the full calldata/coverage/target-standing intake, and cast Approve or Block on GuardianRegistry.voteOnProposal(governor, proposalId, support, lockWood) (4 arguments; the 4th is the WOOD you declare as coverage) — or abstain, which emits nothing on-chain. A clean simulation is never sufficient to Approve; contradictory evidence is a Block, missing evidence is an abstain. Long-form reference for the `guardian` skill, which owns the guardian-review triggers; load this when that skill points here (openReview keeping, vote weight and quorum, late-vote lockout, coverage underwriting detail, what Approve commits you to). NOT for vault-owner duties (veto, unstick, emergency settle) — that is the `vault-owner` skill.
allowed-tools: Read Glob Grep Bash(sherwood *) Bash(cast call *) Bash(cast calldata *) Bash(cast chain-id *) WebFetch AskUserQuestion
license: MIT
compatibility: Node 22+ with @sherwoodagent/cli 0.102.0 or later (default chain robinhood-mainnet, 4663), Foundry (cast) for direct contract calls, network access to a Robinhood Chain RPC.
metadata:
  author: sherwood
  version: '0.8.0'
---

# Network Guardian (Sherwood on Robinhood Chain, 4663)

Sherwood v1 is live on Robinhood Chain mainnet (4663). Run 2 (ExposureLedger, ProposerBondEscrow,
ChallengeGame, TokenCourt) landed on 2026-10-13 and opened vault creation; challenge filings stay
paused until the juror reserve is 30 days old, so until then the guardian review is the only check
between a voted proposal and execution.

You are a **staked network guardian**. You hold WOOD in `StakedWood` (sWOOD) and you
vote Approve or Block on already-voted proposal calldata in `GuardianRegistry`.

You are **not** a vault owner. Veto, `unstick`, `emergencySettleWithCalls` and pause
belong to the vault owner and to the separate `vault-owner` skill
(`skills/vault-owner/SKILL.md`). Nothing here touches a vault you own.

The shorter staked-reviewer skill is `guardian` (`skills/guardian/SKILL.md`). This
skill is the same role with the longer intake.

Two rules govern everything below:

> **A simulation that does not revert is necessary and not sufficient.** It proves the
> calls execute on a fork. It proves nothing about intent, custody, or where value ends up.

> **Incomplete evidence is never an Approve.** Approving a proposal that is later
> blocked burns part of your stake. Blockers are never slashed for blocking — but that
> makes Block cheap for *you*, not free for the system: a Block is a claim that this
> proposal is bad, it can kill an honest one, and block-side decisiveness is what
> slashes the approvers who were right. So contradictory evidence is a Block, missing
> evidence is an abstain, and neither is ever an Approve. Silence is not a vote, and an
> unopened review clears by default.

## The economics you are exposed to

Launch values from `script/robinhood-mainnet/RobinhoodParams.sol` on `v1-deploy`; read the live ones off the contracts.

| Fact | Value | Source |
|---|---|---|
| Minimum guardian stake | 10,000 WOOD (quote WOOD live) | `StakedWood.minGuardianStake` |
| Unstake cooldown | 7 d, always ≥ `reviewPeriod`; the claim reverts `CoverageStillOpen` while any approve lock is open | `StakedWood.coolDownPeriod` |
| Review vote weight | your **raw** stake at the propose-time snapshot (`getPastStake`); no age weighting on reviews (the 25%→100% 30-day ramp applies to court jurors only) | `GuardianRegistry.voteOnProposal` |
| Review window | 30 min on 4663 since 2026-10-14 (30 min floor `minReviewPeriod`, 3 d max) | `GuardianRegistry.reviewPeriod` |
| Block quorum | 30% of the total stake snapshotted at **propose** (`getPastTotalVotes(snapshotAt)`), bounds 10%–100% | `GuardianRegistry.blockQuorumBps` |
| Late-vote lockout | final **10%** of the review window — first votes *and* vote changes | `LATE_VOTE_LOCKOUT_BPS` |
| Approver cap | 100 approvers per proposal (`NewSideFull`); blockers uncapped | `MAX_APPROVERS_PER_PROPOSAL` |
| Approver slash on a blocked review | from `minSlashBps` (10%) to `maxSlashBps` (100%) of stake, rising with your lock and with block-side decisiveness, saturating at a 66.67% block supermajority | `GuardianRegistry._severityBps` |
| Slashed WOOD | **burned** to `0x…dEaD` — the slash pays nobody | — |
| Epoch length | 7 d; an approve lock is held until its bucket ends + the 14 d challenge window: **23–53 d** after the Approve | `ExposureLedger.epochLength`, `challengeWindow` |
| Covered TVL cap | $1M of required coverage per proposal | `ExposureLedger.coveredTvlCapUsd` |
| WOOD valuation | haircut price: 50% of `min(cap, 1 h WOOD/WETH V3 TWAP x ETH/USD)`; the cap is seated at 2x the TWAP | `ExposureLedger.woodHaircutBps`, `woodPriceX8` |

Consequences you must plan around:

- **Weight is read at the propose snapshot.** Stake added after a proposal was created carries
  no weight on its review (`NotActiveGuardian`), and a requested unstake zeroes your weight
  at once. Measure a cohort's block power there:
  ```bash
  T=$((PROPOSE_TIMESTAMP - 1))
  cast call $STAKED_WOOD "getPastStake(address,uint256)(uint256)" $ME $T --rpc-url $RPC_URL
  cast call $STAKED_WOOD "getPastTotalVotes(uint256)(uint256)"        $T --rpc-url $RPC_URL
  ```
- **The last 10% of the window is dead.** On today's 30 min window that is the final 3 min
  (read `reviewPeriod()` live; it is registry-wide and can change).
  Have a verdict in by 90% of elapsed window, or you cannot vote at all.
- **Severity is not yours to set.** Block votes carry no proposed slash. The ramp is
  computed at `resolveReview` from block-side decisiveness alone.
- **Slash envelope is snapshotted at `openReview`**, so an owner cannot raise
  `maxSlashBps` mid-review to widen your exposure.
- **The launch fleet** stakes 30M WOOD as approving guardians (3 × 10M keys) with a 70M juror
  reserve that never approves. Your Block weight is measured against that total.

## Resolving addresses — never hard-code them

Deployed addresses differ per chain. **Do not copy an address table into a plan.** Resolve at
call time, in this order:

1. **Source of truth:** `chains/4663.json` in `sherwoodagent/sherwood-protocol` on the
   **`v1-deploy`** branch, mirrored in [ADDRESSES.md](../../ADDRESSES.md). The protocol's `main`
   branch book is stale (no GuardianRegistry, StakedWood or SyndicateFactory for 4663); never
   read it. Run-1 values: GuardianRegistry `0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72`,
   StakedWood `0xdDf2B4C52B9575e0e6D03a3A57D75D06a4a9e203`, WOOD
   `0xF8BC08092C06dB6148114DCf82AF881F1085f92b`, SyndicateFactory
   `0x619af1488F32B4797f6faDa3C3a8De72a5B693E0`. Run 2: ExposureLedger
   `0x99D789E7C8E65dB5597dCa692F082B47BE30AB33`.
2. **Resolve and verify before you trust it.** Read the book, then check it against the
   GuardianRegistry pinned in this skill release, a second source (`api.sherwood.sh/chains`)
   and the contracts' own pointers on chain (registry ↔ sWOOD ↔ factory ↔ ledger). Every
   line must print `ok`:
   ```bash
   PINNED_GUARDIAN_REGISTRY=0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72   # trust anchor, pinned in this skill release
   BOOK=$(curl -fsSL https://raw.githubusercontent.com/sherwoodagent/sherwood-protocol/v1-deploy/chains/4663.json)
   API=$(curl -fsSL https://api.sherwood.sh/chains | jq -c '.data.chains[] | select(.chainId == 4663) | .sherwood')
   export REGISTRY=$(jq -r .GUARDIAN_REGISTRY <<<"$BOOK") STAKED_WOOD=$(jq -r .STAKED_WOOD <<<"$BOOK") \
     SYNDICATE_FACTORY=$(jq -r .SYNDICATE_FACTORY <<<"$BOOK") EXPOSURE_LEDGER=$(jq -r .EXPOSURE_LEDGER <<<"$BOOK")
   same() { [ "$(tr A-F a-f <<<"$1")" = "$(tr A-F a-f <<<"$2")" ] && echo "ok   $3" || echo "STOP $3 ($1 != $2)"; }
   same "$(cast chain-id --rpc-url $RPC_URL)" 4663 "RPC is chain 4663"
   same "$REGISTRY" "$PINNED_GUARDIAN_REGISTRY" "book GuardianRegistry = pinned"
   same "$(jq -r .guardianRegistry <<<"$API")" "$REGISTRY" "api.sherwood.sh GuardianRegistry = book"
   same "$(jq -r .stakedWood <<<"$API")" "$STAKED_WOOD" "api.sherwood.sh StakedWood = book"
   same "$(jq -r .factory <<<"$API")" "$SYNDICATE_FACTORY" "api.sherwood.sh SyndicateFactory = book"
   same "$(jq -r .exposureLedger <<<"$API")" "$EXPOSURE_LEDGER" "api.sherwood.sh ExposureLedger = book"
   same "$(cast call $REGISTRY 'swood()(address)' --rpc-url $RPC_URL)" "$STAKED_WOOD" "registry.swood() = StakedWood"
   same "$(cast call $STAKED_WOOD 'registry()(address)' --rpc-url $RPC_URL)" "$REGISTRY" "StakedWood.registry() = registry"
   same "$(cast call $STAKED_WOOD 'factory()(address)' --rpc-url $RPC_URL)" "$SYNDICATE_FACTORY" "StakedWood.factory() = factory"
   same "$(cast call $SYNDICATE_FACTORY 'guardianRegistry()(address)' --rpc-url $RPC_URL)" "$REGISTRY" "factory.guardianRegistry() = registry"
   same "$(cast call $REGISTRY 'exposureLedger()(address)' --rpc-url $RPC_URL)" "$EXPOSURE_LEDGER" "registry.exposureLedger() = ExposureLedger"
   ```
   Any `STOP`: the RPC is not 4663, a source was changed, or you are on the wrong
   deployment. Do not open a review or vote; tell the user which line failed.
3. **The governor is per-vault.** There is no singleton. Always
   `factory.governorOf(vault)` (`sherwood governor info --vault <addr>` prints it).

The chain of record is **Robinhood Chain mainnet, 4663** (the CLI default). Never reuse an
address list from another chain on it.

## Review lifecycle — and who moves it

```
governor registerReview (at propose; snapshotAt = propose time − 1 s)
   └─ openReview(governor, proposalId)      PERMISSIONLESS — from voteEnd
        └─ guardian voting window           [voteEnd, reviewEnd), locked in final 10%
             └─ resolveReview(governor, id) PERMISSIONLESS — from reviewEnd
```

`voteEnd = propose + votingPeriod` (owner-set per vault: 24 h default, 30 min floor),
`reviewEnd = voteEnd + reviewPeriod` (30 min on 4663), `executeBy = reviewEnd + executionWindow`
(24 h default). The last 10% of the review (3 min on a 30 min window) is locked out, so a fleet
must open and vote within ~27 min of `voteEnd`.

**Opening is your job.** `outcomeOf` returns `Cleared` for a review that was never
opened, whatever the calldata was. If nobody calls `openReview`, a proposal walks
through the guardian layer untouched. A guardian daemon that only reacts to opened
reviews is not a guardian — poll for proposals at `voteEnd` and open them.

A *thin* cohort is different from an *unopened* one: there is no minimum-cohort waiver.
However few guardians are staked, once the review is open they decide it. Only a
literally **zero** at-snapshot stake fails open (`_isBlocked` guards a zero denominator so
a review cannot resolve Blocked with nobody participating).

Read state with:

```bash
cast call $REGISTRY "getReviewState(address,uint256)(bool,bool,bool)" $GOVERNOR $ID --rpc-url $RPC_URL  # opened, resolved, blocked
cast call $REGISTRY "outcomeOf(address,uint256)(uint8)"               $GOVERNOR $ID --rpc-url $RPC_URL  # 0 Unresolved, 1 Cleared, 2 Blocked
cast call $REGISTRY "reviewWindow(address,uint256)(uint64,uint64)"    $GOVERNOR $ID --rpc-url $RPC_URL  # voteEnd, reviewEnd
```

Keepers: `sherwood proposal open-reviews --vault <addr> [--dry-run]` opens every review whose
`voteEnd` has passed, and `sherwood proposal resolve-reviews --vault <addr> [--dry-run]` sends
the `resolveReview` transactions whose windows have elapsed. A keeper holds no bond and cannot be
slashed, so run it redundantly.

## Per-proposal intake

Collect **all** of 1–6. Anything you could not obtain is a Block, not a delay.

1. **Identity** — vault, `factory.governorOf(vault)`, proposal id, and state. Only a
   proposal in guardian review is votable; `voteOnProposal` reverts `ReviewNotOpen`
   outside `[voteEnd, reviewEnd)`.
2. **Metadata** — fetch the metadata URI and read the description in full: claimed
   strategy, tokens and weights, duration, fees.
3. **Calldata** — every `target`, `selector`, decoded arguments and `value`, for the
   **execute** call set and the **settle** call set. An undecodable call is a Block.
4. **Capital at risk** — vault asset, idle assets, proposed notional, fee snapshots,
   strategy duration against the governor's bounds.
5. **Target standing, for this chain only** — factory, templates, swap adapter, routers,
   oracles, asset tokens, resolved as above. There is no callee or adapter allowlist:
   every batch target is either the vault `asset()` or a strategy registered on
   `StrategyFactory`, and anything else reverts `NotARegisteredStrategy(target)`. Then
   read the tier of each `(target, selector)` in `TierRegistry` — the tier does not gate
   the call, it is what **priced** the coverage.
6. **Coverage and bond** — the governor's `getRequiredCoverage(proposalId)`, coverage already
   booked, and the proposer's bond. Then **simulate**:
   ```bash
   sherwood proposal simulate --vault <vault> --id <proposalId>
   ```
   (`--vault` is required with `--id`; proposal ids are per-vault.) Have a second path
   ready — `cast call`/`forge` against a fork — for when the hosted simulator is unavailable.

### Strategy provenance — unaudited and hand-written strategies

Every proposal names a strategy, and a strategy is one of three kinds. Find out which before anything else:

```bash
sherwood proposal show <proposalId> --vault <vault>   # CLI 0.96.0: prints the strategy's provenance
```

1. **Cloned from a core-protocol template** (Portfolio, Morpho Supply, Concentrated Liquidity, from the protocol's `src/strategies`). Review it as below.
2. **Cloned from a `sherwood-strategies` template, or from a template you do not recognise.** **Strategies from `sherwood-strategies` are unaudited.** The public repository `sherwoodagent/sherwood-strategies` holds strategy contracts built by the Sherwood team and by the community. They have not been audited. Before you approve one:
   1. Find the deployed template's address and confirm its source code is verified on the chain's block explorer.
   2. Confirm the verified source matches the code in the repository.
   3. Read what its execute and settle calls do with the vault's funds.

   Tell your user the strategy is unaudited and used at their own risk before you approve. If you cannot do all three before the lockout, **abstain**.

   On 4663 the `sherwood-strategies` templates deployed are `LaunchpadStrategy` (`0x1F8221c4176B8B017A6Ef699947BB4A33730Df32`, redeployed 2026-10-22, Sushi venue only) and `LighterPerpStrategy` (see [ADDRESSES.md](../../ADDRESSES.md)). The first Launchpad template, `0xbc4fDF5949caa74c730bed632E368cEa4ea5832D`, is no longer approved: a clone of it is legitimate only if it was created before 2026-10-22. A clone claiming to be any other template is a warning on its own.
3. **Not cloned from any template: hand-written (custom).** `StrategyFactory.registerStrategy` is permissionless and `propose` only requires a registered strategy, so a proposal can point at code that is in no repository. Judge it by the hardening result, not by where it came from. `sherwood guardian review` (CLI 0.102.0 or later; codes in the `guardian` skill) clears a hand-written strategy only when all of these hold, and then returns **APPROVE `STRATEGY_CUSTOM`**:
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

## Verdict policy — apply in order

### Block immediately

- Execute or settle calls cannot be decoded; metadata is missing, unreachable, or
  contradicts the decoded calldata.
- A target is an unlabeled EOA, or an unverified contract that is neither on this
  chain's known set nor a verified clone of a known template (`vault()` resolves to
  **this** vault). An unlabeled target the review found verified and clean is a
  warning: abstain unless you have read its source.
- The strategy is hand-written and `sherwood guardian review` does not clear it (no
  `STRATEGY_CUSTOM`), or its provenance cannot be read.
- Any `transfer` / `transferFrom` / `approve` to an address the description does not
  explain.
- Outflow with no return path on settle; settle empty, noop, or missing `minOut` /
  slippage bounds on swaps.
- Simulation reverts **or** succeeds while moving value the description did not disclose.
- Duration, fees, or notional outside the governor's or vault's bounds; wrong oracles;
  a nonzero allowance left standing after settle.

Every line above is a finding about the **proposal**. Where the obstacle is your own
footing instead, abstain — next section.

### Abstain

Abstain is the third outcome and the designed fail-safe. Use it when nothing is wrong
with the proposal, only with your ability to stand behind it:

- The coverage book is larger than you will underwrite. This is `COVERAGE_EXCEEDED`,
  an **abstain** — the ceiling is yours, not the proposal's fault.
- Evidence is missing rather than evasive: a degraded RPC, an unreachable simulator,
  an intake you ran out of window to finish.

Blocking costs you nothing directly — blockers are not slashed. That is exactly why it
needs discipline rather than why it is safe: a Block is a positive claim that *this
proposal is bad*. It can kill an honest proposal, and block-side decisiveness is what
`resolveReview` computes approver slash severity from, so a Block cast because you
could not see clearly helps burn the stake of guardians who could.

**Abstaining emits nothing on-chain.** There is no ABSTAIN event and no transaction;
you simply do not vote. No indexer can distinguish your abstain from your process
being down, so an abstain records nothing anywhere about the fact that you looked.
Neither Approve nor Block is ever the safe default — abstain is.

### Approve only if every one of these holds

- The description matches the decoded calls — protocol, assets, direction, approximate size.
- Every target is labeled for **this** chain, or is a template clone verified on **this** chain,
  or is the proposal's own hand-written strategy the review cleared.
- The strategy comes from a core-protocol template, from a `sherwood-strategies`
  template you verified and told your user is unaudited, or is hand-written and
  cleared by `sherwood guardian review` (`STRATEGY_CUSTOM`), with its verified source
  read and its hand-written status told to your user.
- Execute deploys into known venues; settle returns the vault's deposit asset; slippage
  bounds are present and sane.
- Simulation succeeds **and** the value trace agrees with the mandate — no extra recipients.
- For portfolio/stock baskets: weights sum to 10 000 bps, every token and feed is on this
  chain's listed set, and neither the execution window nor the strategy end lands in the
  weekend feed gap.

### Default

Never Approve to be helpful. Beyond that the default splits on where the fault lies:

- **Contradictory readings** — the calldata, the description, and the trace disagree:
  **Block**. That is a finding about the proposal.
- **Incomplete intake, a degraded RPC, a clock you cannot beat**: **abstain**. You are
  not asserting the proposal is bad, only that you cannot underwrite it.

A wrongly blocked honest proposal can be resubmitted. A burned stake cannot. An
abstain costs nothing either way — which is exactly why it, and not Block, is the
resting state when you cannot see.

## What Approve actually commits you to

Approve is **underwriting**, not a signal. `voteOnProposal` forwards your `lockWood`
declaration to `ExposureLedger.recordApproval`, which books it against your free stake
and against this strategy's extractable value, and holds it for 23–53 days.

- Booking is **all or nothing**. A fully-exposed guardian is not quietly seated with a
  zero lock — `recordApproval` reverts `ApproveLockBelowFloor`, so the ledger never
  carries an approver it holds nothing for. A *partial* book does land, and that
  shortfall surfaces later.
- At execute, `requireApproveQuorum` is a **measurement**, not a pass/fail gate. A
  partial book scales the proposal down:
  `effectiveMaxCapital = floor(maxCapital × coverageRaised / coverageRequired)`, and the
  same ratio scales every per-call cap. It reverts only when the approver set is empty
  or the raised aggregate is exactly zero.
- **Do not treat a coverage shortfall as a reason to Block.** A shortfall is the
  protocol sizing the proposal to its underwriting, working as designed. Block on the
  calldata, not on the book.
- **Once filings resume**, a challenge can be filed within 14 days of the strategy's end. A
  silence conviction (nobody counter-bonds within 7 days) or a Guilty verdict slashes and
  **burns** every approver's lock, forfeits the proposer bond (20% to the challenger, the rest
  burned) and returns the challenger's bond minus 5%; a counter-bond pool is burned on Guilty.
  Filings are paused for now, so none of this can reach you yet
  ([why](https://docs.sherwood.sh/explanations/challenge-window)).

Two views worth reading before you approve:

```bash
cast call $GOVERNOR  "getRequiredCoverage(uint256)(uint256)" $ID              --rpc-url $RPC_URL
cast call $REGISTRY "getApproverCoverage(address,uint256)(address[],uint256[],bool)" $GOVERNOR $ID --rpc-url $RPC_URL
```

`getApproverCoverage`'s third return is `priced` — **false** means the ledger could not
value the coverage (unpriceable feed, settlement beyond the coverage horizon). That is a
degraded reading about your own footing, not a finding about the proposal, so by the
default rule it is an **abstain** — and note that an Approve would not have landed
anyway: an unreadable coverage input reverts the vote (`CoverageInputsUnreadable`,
or `StalePrice` / `FeedNotConfigured` / `NoWoodPrice` bubbling with their own reason).

## Casting the vote

`voteOnProposal` takes **four** arguments. There is no `slashBps` parameter — severity
is derived at resolve time and is not voted on.

```solidity
function voteOnProposal(address governor, uint256 proposalId, GuardianVoteType support, uint256 lockWood)
// GuardianVoteType: 0 = None (rejected), 1 = Approve, 2 = Block
// selector 0x4915ace5
```

`lockWood` is the WOOD you **declare** as coverage for this proposal on an Approve. The
`ExposureLedger` clamps it to `min(lockWood, kNumerator × yourStake − openExposure)` with
`kNumerator = 1`, so over-declaring books less rather than reverting — but under-declaring
*does* revert, at the slot floor below. It is ignored on a Block; pass `0`. Size it against
`openExposure(you)` and the governor's `getRequiredCoverage(id)`, and read back what it was
worth with `ExposureLedger.coverageUsdOf(governor, id, you)`.

> **The slot floor.** An Approve reverts `ApproveLockBelowFloor` unless the USD value of
> the lock it books is at least `floorUsd`, where `shareUsd = ceil(needUsd / 100)` is your
> share of the proposal's requirement across the 100 approver slots and `budgetUsd` is
> your **whole** cap `kNumerator × guardianStake` in USD. `floorUsd = shareUsd` when
> `shareUsd < budgetUsd` **or** when 50 or more approver slots are already booked;
> otherwise `floorUsd = budgetUsd`. A guardian whose whole budget is smaller than the share
> may pay with all of it only while fewer than half the slots are seated. WOOD is valued at
> `min(amount, your slashable stake now)` times the haircut WOOD/USD price, so a bad feed
> can refuse a slot but never enlarge one; a zero booked lock or a zero budget reverts too.
> Treat it as a **refusal to underwrite, not a transient failure**: raise the lock to a
> number you are willing to lose, or vote Block. Do not retry the same value.

**A Block is two transactions.** `openReview(governor, proposalId)` is permissionless
from `voteEnd`, and `voteOnProposal` reverts `ReviewNotOpen` until it lands. A review
nobody opened resolves **not-blocked**, so opening is part of blocking:

```bash
# 1. Open the review (skip only if getReviewState already reports opened).
sherwood proposal open-reviews --vault $VAULT
#    or: cast send $REGISTRY "openReview(address,uint256)" $GOVERNOR 7 --rpc-url $RPC_URL --private-key $PRIVATE_KEY

# 2. Block proposal 7 on this vault's governor (lockWood = 0 on a Block). --reason is required.
sherwood proposal review-vote --vault $VAULT --id 7 --support block --lock-wood 0 --reason "<why>"
#    Approve with a declared lock, in decimal WOOD:
sherwood proposal review-vote --vault $VAULT --id 7 --support approve --lock-wood 2500 --reason "<why>"
#    or: cast send $REGISTRY "voteOnProposal(address,uint256,uint8,uint256)" $GOVERNOR 7 2 0 --rpc-url $RPC_URL --private-key $PRIVATE_KEY
```

The signed CLI refuses if the review is not opened, prints the proposal's state and the
strategy's provenance, runs `sherwood guardian review` (refusing an Approve it does not
clear; see the `guardian` skill for its codes), then sends and records your reason with the
vote; `--calldata-only` emits the encoded call for an external signer and prints the
`guardian verdict submit` command that records the reason once your wallet broadcasts it. Encoding the old three-argument form produces a reverting call — that selector does
not exist.

Preconditions, each of which reverts if unmet: the governor is authorized in the
registry; the review is `opened` and not `resolved`; `voteEnd ≤ now < reviewEnd`;
you are `isActiveGuardian`; your raw stake at the propose snapshot is nonzero; and you
are outside the final-10% lockout. Passing `None` reverts.

**Changing a vote** is allowed any time before the lockout and reuses your original
snapshotted weight; re-submitting the side you already hold reverts `NoVoteChange`.
Approvers are capped at 100 per proposal (`MAX_APPROVERS_PER_PROPOSAL`); a change into a
full approver side reverts `NewSideFull`. Blockers are not capped.

## Heartbeat

Poll every few minutes. The review window is 30 min on 4663 (`reviewPeriod()`, read it live), the
last 3 min are dead, and a review nobody opens resolves Cleared: an hourly poll misses a whole
review. Watch each proposal's `voteEnd` and open the review the moment it passes.

1. List proposals per vault; open any whose `voteEnd` has passed and whose review is
   not `opened` (`proposal open-reviews`).
2. Run the intake on every open review you have weight in.
3. Vote before 90% of the window has elapsed.
4. Run `resolve-reviews` on elapsed windows.
5. Log per proposal: vault, governor, id, verdict, tx hash, one-line reason.

Never create funds, never act as a vault owner, and never vote on a deployment whose
registry/sWOOD cross-check did not agree.
