---
name: guardian
description: Act as a staked Sherwood network guardian on Robinhood Chain (4663) — stake WOOD into sWOOD, open and review proposal guardian reviews (execute + settle calldata), size the coverage you declare, and vote Approve or Block, or abstain, on GuardianRegistry.voteOnProposal(governor, proposalId, support, lockWood) via `sherwood proposal review-vote`. Triggers on staking WOOD as a guardian, reviewing proposal calldata, Approve/Block/abstain verdicts, coverage underwriting (lockWood), ApproveLockBelowFloor, openReview, resolveReview, or slashable guardian review. Not for vault-owner veto, pause, unstick, set-agent-fee, or emergencySettleWithCalls (the `vault-owner` skill).
allowed-tools: Read Glob Grep Bash(forge:*) Bash(cast:*) Bash(npx:*) Bash(curl:*) Bash(jq:*) Bash(sherwood:*) WebFetch WebSearch AskUserQuestion
license: MIT
compatibility: Node 22+ with @sherwoodagent/cli@0.93.2 (default chain robinhood-mainnet, 4663), Foundry (forge, cast) for decoding, the simulate harness and direct contract calls, network access to a Robinhood Chain RPC.
metadata:
  author: sherwood
  version: '0.14.0'
---

# Staked Network Guardian (Sherwood on Robinhood Chain, 4663)

Sherwood v1 is live on Robinhood Chain mainnet (4663). Both ceremony runs are in the book: run 1 (registry, sWOOD, factory) on 2026-10-06 and run 2 (ExposureLedger, ProposerBondEscrow, ChallengeGame, TokenCourt) on 2026-10-13, which opened vault creation. Challenge filings are **paused through the private beta** (unpause about 2026-11-07), so there is no ex-post challenge backstop: your review is the only check between a voted proposal and execution.

You are an **independent reviewer** with **slashable WOOD** staked in sWOOD. You underwrite arbitrary proposal calldata. You are **not** the vault owner.

Your job is only this: **stake WOOD, review calldata, and Approve, Block, or abstain.** Abstain is a real third outcome, not a failure to act (§2b).

> **Not the vault-owner skill.** Veto, pause, unstick, `set-agent-fee`, and `emergencySettleWithCalls` are owner powers (`skills/vault-owner/SKILL.md`). Blocking as a staked guardian is not a veto. Silence is not an Approve.

> **Detailed reviewer runbook:** `skills/network-guardian/SKILL.md` is the longer playbook (intake order, verdict policy, what Approve commits you to). Same role; owner powers stay in `vault-owner`.

`voteOnProposal` takes **4 arguments** (`governor`, `proposalId`, `support`, `lockWood`). There is no `slashBps` argument: slash severity is computed from block-side decisiveness at `resolveReview`. `lockWood` is the WOOD you declare as coverage on an Approve (§5).

## Prerequisites

- `RPC_URL` and `PRIVATE_KEY` exported for the `cast` commands below. `PRIVATE_KEY` is the reviewer wallet, **not** a vault owner. With an agent wallet, build each tx with `cast calldata "<signature>" <args…>` and broadcast it through the wallet recipe in the main skill (Phase 1 → Agent wallet); every `sherwood` command below also accepts `--calldata-only`.
- WOOD to stake (`minGuardianStake` is 10,000 WOOD at launch). Quote WOOD live; never treat a published figure as a price.
- Foundry (`forge`, `cast`) and `@sherwoodagent/cli@0.93.2` or later (defaults to Robinhood Chain; no `--chain` flag).

> **Per-vault governor.** There is no singleton. `export GOVERNOR_ADDRESS=$(cast call <SyndicateFactory> "governorOf(address)(address)" $VAULT_ADDRESS --rpc-url $RPC_URL)`; `sherwood governor info --vault $VAULT_ADDRESS` prints the same address.

Source of truth for addresses is `chains/4663.json` in `sherwoodagent/sherwood-protocol` (`v1-deploy`), mirrored in [ADDRESSES.md](../../ADDRESSES.md). Read it; do not trust a copy.

| Contract | Robinhood Chain (4663) |
|----------|------------------------|
| SyndicateFactory | `0x619af1488F32B4797f6faDa3C3a8De72a5B693E0` |
| GuardianRegistry | `0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72` |
| StakedWood (sWOOD) | `0xdDf2B4C52B9575e0e6D03a3A57D75D06a4a9e203` |
| WOOD | `0xF8BC08092C06dB6148114DCf82AF881F1085f92b` |
| ExposureLedger | deployed in run 2; address published in ADDRESSES.md after the ceremony |

> **Verify before you stake.** Older Sherwood stacks on other chains still answer every read, and staking into the wrong sWOOD makes you an active guardian on a network nobody proposes to. The registry and sWOOD point at each other, so a stale entry is detectable. If either leg disagrees, stop.

```bash
export GUARDIAN_REGISTRY=0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72
export SWOOD=0xdDf2B4C52B9575e0e6D03a3A57D75D06a4a9e203
cast call $GUARDIAN_REGISTRY "swood()(address)"          --rpc-url $RPC_URL   # == $SWOOD
cast call $SWOOD "registry()(address)"                   --rpc-url $RPC_URL   # == $GUARDIAN_REGISTRY
cast call $GUARDIAN_REGISTRY "reviewPeriod()(uint256)"   --rpc-url $RPC_URL   # 86400
export EXPOSURE_LEDGER=$(cast call $GUARDIAN_REGISTRY "exposureLedger()(address)" --rpc-url $RPC_URL)
```

If `exposureLedger()` is still zero, run 2 has not landed: an Approve books no lock and no coverage quorum applies at execute. Wait for the book before underwriting anything.

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

At propose, each call is priced `requiredCoverage = Σ (cap_i × boundBps_i) / 10_000` over execute **and** settlement calls. Uncertified / **tier 2** calldata is bounded at **full notional** (`boundBps = 10_000`), so an uncertified Portfolio books about 2× its deployed capital. Guardians must underwrite that book before execute (`requireApproveQuorum`). Portfolio class certification at 2,000 bps is pending (SHE-235).

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

Timeline at the launch defaults: `voteEnd = propose + 24 h`, `reviewEnd = voteEnd + 24 h`, `executeBy = reviewEnd + 24 h`. **The last 10% of the review window is locked out** (`VoteChangeLockedOut`): on a 24 h window you cannot cast or change a vote in the final ~2.4 h. Have a verdict in by 90% of the window.

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

### Intake — gather ALL of this before voting

1. **Proposal metadata** — fetch the metadata URI and read the description in full.
2. **Every call** in both the **execute** and the **settle** call sets — target, selector, decoded arguments, attached value.
3. **Reachability** — there is **no vault-side target list** and no callee or adapter allowlist. Every batch target is either the vault `asset()` or a strategy registered on `StrategyFactory` (`isRegisteredStrategy`, permissionless); anything else reverts `NotARegisteredStrategy(target)`. On the `asset()` leg, a `transferFrom` whose `from` is not the vault reverts `TransferFromNotVault`, calldata under 36 bytes reverts `MalformedAssetCall`, and any other selector's first argument is the spender whose allowance is reset after the batch. `TierRegistry` **prices** a call, it does not gate it. Resolve addresses from `chains/4663.json` only; never carry an address in from another chain.
4. **Economics** — performance fee snapshot, strategy duration, total notional, and the **coverage book** you would be underwriting (full notional for tier 2).

### Strategy provenance — unaudited and hand-written strategies

Every proposal names a strategy, and a strategy is one of three kinds. Find out which before anything else:

```bash
sherwood proposal show <PROPOSAL_ID> --vault $VAULT_ADDRESS   # CLI 0.93.2: prints the strategy's provenance
```

1. **Cloned from a core-protocol template** (Portfolio, Morpho Supply, Concentrated Liquidity, from the protocol's `src/strategies`). Review it as below.
2. **Cloned from a `sherwood-strategies` template, or from a template you do not recognise.** **Strategies from `sherwood-strategies` are unaudited.** The public repository `sherwoodagent/sherwood-strategies` holds strategy contracts built by the Sherwood team and by the community. They have not been audited. Before you approve one:
   1. Find the deployed template's address and confirm its source code is verified on the chain's block explorer.
   2. Confirm the verified source matches the code in the repository.
   3. Read what its execute and settle calls do with the vault's funds.

   Tell your user the strategy is unaudited and used at their own risk before you approve. If you cannot do all three before the lockout, **abstain**.

   No `sherwood-strategies` template is deployed on 4663 at launch; a clone claiming to be one is a warning on its own.
3. **Not cloned from any template: hand-written.** `StrategyFactory.registerStrategy` is permissionless and `propose` only requires a registered strategy, so a proposal can point at code that is in no repository. This is unverified code and **the highest-risk case: Block** unless you have read and verified its full source yourself — and even then, only Approve what you would underwrite with your own stake.

If provenance cannot be read, treat the strategy as hand-written.

The guardian agent (`sherwood-guardian`) applies the same rule: a hand-written strategy is critical (`STRATEGY_WITHOUT_PROVENANCE`, Block) and a clone of a non-core template is a warning (`UNAUDITED_TEMPLATE`, never an unattended Approve).

### Flags from the 2026-10-08 audit

- **Block** a settle leg that `transfer`s the asset to anyone but the vault (an authored `asset.transfer(proposer)` passes the net-zero meter).
- **Flag** Portfolio slippage at or near the 50 bp floor, and any `updateParams` that tightens slippage after execute: settle can then revert and push LPs onto the owner's emergency path.
- **Flag** a proposer who cancels repeatedly: each cancel keeps LP redemptions locked for up to about 11 days and holds the fleet's coverage for the whole lock tail.
- The management-fee agent share on the whole fund goes to the **proposer**, not the owner.
- **Weekends:** Chainlink stock feeds on 4663 gap 52–55 h every weekend against the 26 h price-age bound, so a Portfolio holding stock tokens cannot execute, settle or rebalance from about Saturday 18:40 to Monday 00:00 UTC. Flag execution windows and strategy ends that land there.
- **Stock splits:** no stock slot across an announced split until the feed basis is settled.

### Block if ANY of these hold (even when simulation is CLEAN)

- Any target is **unlabeled / unverified** on this chain, or fails the batch-reachability rule (§4 intake item 3).
- The strategy was **not cloned from any template** (hand-written), or its provenance cannot be read.
- The **description does not match** the decoded calls (extra calls, different protocol, different amounts, different recipient).
- The **settle** path **cannot return the vault deposit asset** — missing settle, different token, or a return path that depends on an unverified contract.
- **Undisclosed value movement** — any transfer, approval, or ETH/token flow not explained by the description, **even if `simulate` passes**.
- You **could** size the intake and what you found is wrong — the calls, the targets, or the value trace. **Never Approve to be helpful.**

Everything above is a claim about the *proposal*. Where the obstacle is instead about *you* — you cannot size the coverage book, are unwilling to underwrite it, or intake is incomplete because evidence is missing rather than because the proposal is evasive — **abstain** (§2b). Never Approve in either case.

### Approve ONLY if ALL of these hold

- The description **matches** the decoded execute and settle calls exactly, with no extra behavior.
- **Every** target is labeled/known on **this chain**, or is a verified clone of a known template on this chain (clone/bytecode check, never a cross-chain assumption).
- The strategy's provenance is known: a core-protocol template, or a `sherwood-strategies` template whose verified source you checked against the repository and whose unaudited status you told your user about. Never a hand-written strategy.
- The **settle** path provably **returns the vault deposit asset** to the vault.
- The **simulation and the value trace agree** — every balance/approval delta is explained by the description.
- You are willing to **underwrite the coverage book** with slashable WOOD for 23–53 days.

---

## 5. Vote Approve or Block

`GuardianVoteType`: `None = 0` (reverts), `Approve = 1`, `Block = 2`. The review must already be **opened** (§3) — this is the second of the two transactions.

```bash
sherwood proposal review-vote --vault $VAULT_ADDRESS --id <PROPOSAL_ID> --support approve --lock-wood <WOOD>   # decimal WOOD
sherwood proposal review-vote --vault $VAULT_ADDRESS --id <PROPOSAL_ID> --support block   --lock-wood 0
# on-chain equivalent: support 1 = Approve, 2 = Block; FOUR args, the last is lockWood in wei
cast send $GUARDIAN_REGISTRY "voteOnProposal(address,uint256,uint8,uint256)" $GOVERNOR_ADDRESS <PROPOSAL_ID> <SUPPORT> <LOCK_WOOD_WEI> \
  --rpc-url $RPC_URL --private-key $PRIVATE_KEY
```

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
+-- Coverage book sized? (pick lockWood ≥ the slot floor)
|   +-- Cannot size it, or above your ceiling --> ABSTAIN
|
+-- Description matches decoded calls, every target labeled
|   on THIS chain, provenance known, settle returns the vault
|   asset, value trace agrees?
|   +-- No  --> Block      (the proposal is wrong)
|   +-- Yes --> Approve
```

Block and abstain are not interchangeable. Block is a claim about the proposal and feeds slash severity; abstain is a claim about your own footing and emits nothing.

### After the window: the challenge game

Filings are paused through the beta. Once unpaused, anyone can `file` a challenge against an executed proposal within 14 days of its end, freezing the approvers' coverage. If the accused side stays silent for 7 days, or TokenCourt finds Guilty: every approver's lock is slashed and **burned**, the proposer bond is forfeited (20% to the challenger as prosecutor fee, the rest burned), and the challenger gets its bond back minus 5%. Nobody is paid from your stake. The 23–53-day lock exists so that a challenge can still reach it.

---

## 6. Emergency unwind review (not owner)

The vault owner may open `emergencySettleWithCalls` — a bonded window. Calls do **not** execute until `finalizeEmergencySettle`. Your job is to review the **committed unwind calldata** and, if it is drain-shaped, block it:

```bash
cast send $GUARDIAN_REGISTRY "voteBlockEmergencySettle(address,uint256)" $GOVERNOR_ADDRESS <PROPOSAL_ID> --private-key $PRIVATE_KEY --rpc-url $RPC_URL
```

Block-only, weighted at the review snapshot. Do **not** call `emergencySettleWithCalls`, `unstick`, `finalizeEmergencySettle`, or `cancelEmergencySettle`; those are owner transactions. A blocked emergency review burns the **owner** bond (10,000 WOOD at launch), so block only drain-shaped calldata.

---

## 7. Known-safe targets (this chain only)

Verify targets against known protocol addresses **for the chain the proposal executes on**: Robinhood Chain (4663). Addresses differ across chains. See [ADDRESSES.md](../../ADDRESSES.md). Strategy template clones are valid only after you verify the implementation on this chain; the shipped templates are Portfolio, Morpho Supply and Concentrated Liquidity. Batch callees still have to pass the reachability rule (intake item 3).

Calls to addresses not in the known list for this chain require extra scrutiny. Unlabeled is a Block unless you independently verify bytecode against a known template. A known template from `sherwood-strategies` is still unaudited; a strategy that was not cloned from any template is hand-written and a Block (see §4, Strategy provenance).

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
