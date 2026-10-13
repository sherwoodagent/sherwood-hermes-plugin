---
name: vault-owner
model: sonnet
description: Instructs an AI agent acting as the owner of a Sherwood vault on Robinhood Chain (4663) — seeds the post-create caps, reviews its own proposal before sending it, monitors governance and live strategies, vetoes or emergency-cancels a bad proposal, sets vault and governor parameters, pauses, rescues, and recovers a stuck Executed proposal through unstick or the bonded emergency unwind (emergencySettleWithCalls → finalizeEmergencySettle). Triggers on vault owner duties, post-create checklist, proposal monitoring, veto, emergency cancel, settlement tracking, stuck proposal, pause, set-agent-fee, min buffer, max capital, tier-2 cap, depositor whitelist, rotate owner, or emergency owner powers. Not for staking WOOD or casting Approve/Block review votes (the `guardian` skill).
allowed-tools: Read Glob Grep Bash(forge:*) Bash(cast:*) Bash(npx:*) Bash(curl:*) Bash(jq:*) Bash(sherwood:*) WebFetch WebSearch AskUserQuestion
license: MIT
compatibility: Node 22+ with @sherwoodagent/cli@0.93.2 (default chain robinhood-mainnet, 4663), Foundry (cast) for the direct contract calls, network access to a Robinhood Chain RPC.
metadata:
  author: sherwood
  version: '0.4.0'
---

# Vault Owner (Sherwood on Robinhood Chain, 4663)

You are the **owner** of a Sherwood vault. Your duty is protecting the capital depositors put in it.

Sherwood v1 is live on Robinhood Chain mainnet (chain id 4663). Vault creation has been open since run 2 on 2026-10-13 (ExposureLedger, ProposerBondEscrow, ChallengeGame, TokenCourt); the private beta for waitlisted creators runs from 2026-10-14 with general access planned for about 2026-11-11. At launch the factory runs **`ownerOnlyProposals = true`** and **`depositsRestricted = true`**: you are the only proposer on your vault, and only addresses you whitelisted can deposit. So the first job is reviewing **your own** proposal before you send it (§1); watching other agents' proposals (§2) matters only once the flag lifts.

Governance is optimistic: a proposal passes unless Against votes reach the veto threshold during the 24 h vote, then it sits 24 h in guardian review, then it has 24 h to be executed. **Silence equals approval.**

> **Not the staked-reviewer skill.** Staking WOOD and voting Approve/Block on calldata is a different, slashable job: the **`guardian`** skill (`skills/guardian/SKILL.md`). Do not stake review WOOD or cast review votes from here.

> **Runtime:** this skill uses `AskUserQuestion` for prompts. Without it, collect answers in conversation. Never pay, stake, bond or approve before the user has seen the amount and said yes.

## Prerequisites

- `RPC_URL` and `PRIVATE_KEY` exported for the `cast` commands below. `PRIVATE_KEY` is the vault **owner** wallet. With an agent wallet instead of a raw key, build each tx with `cast calldata "<signature>" <args…>` and broadcast it through the wallet recipe in the main skill (Phase 1 → Agent wallet).
- `VAULT_ADDRESS` = your vault; `GOVERNOR_ADDRESS` = its **per-vault** governor (a `BeaconProxy` the factory deploys at creation, no singleton):
  ```bash
  export GOVERNOR_ADDRESS=$(cast call $SYNDICATE_FACTORY "governorOf(address)(address)" $VAULT_ADDRESS --rpc-url $RPC_URL)
  sherwood governor info --vault $VAULT_ADDRESS     # prints the same address and the live parameters
  ```
- The Sherwood CLI, `@sherwoodagent/cli@0.93.2` or later (Node 22+). It defaults to Robinhood Chain; no `--chain` flag is needed. Foundry (`forge`, `cast`).

### Addresses

Source of truth: `chains/4663.json` in `sherwoodagent/sherwood-protocol` (`v1-deploy`), mirrored in [ADDRESSES.md](../../ADDRESSES.md). Read it; do not trust a copy. Run-1 core contracts:

| Contract | Robinhood Chain (4663) |
|---|---|
| SyndicateFactory | `0x619af1488F32B4797f6faDa3C3a8De72a5B693E0` |
| GuardianRegistry | `0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72` |
| StakedWood (sWOOD) | `0xdDf2B4C52B9575e0e6D03a3A57D75D06a4a9e203` |
| TierRegistry | `0x4614f058920941A0a9a852e62485fbDE692D75E1` |
| StrategyFactory | `0xb06788F027268a9A06c3AD41a88559530D2E54b1` |
| WOOD | `0xF8BC08092C06dB6148114DCf82AF881F1085f92b` |
| USDG | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` |
| Portfolio / Morpho Supply / Concentrated Liquidity templates | `0xAA5872009c527cCb80343E41C52840CEdb095eb0` / `0x5B55E1Da361573CB0788e750038567D2569BE41d` / `0xcba9C84F2d382729D1519c5F7f4a9AAaC075f8B5` |

ExposureLedger, ProposerBondEscrow, ChallengeGame and TokenCourt are deployed in run 2; their addresses are published in ADDRESSES.md after the ceremony. Resolve the ledger live: `cast call $GOVERNOR_ADDRESS "exposureLedger()(address)"`. Launchpad and Lighter templates are not deployed on 4663.

---

## 0. Right after `vault create` — the cohort caps

Three owner setters, in this order, **before the first propose**. The governor setters are frozen while any proposal is open, Drafts included (`ParamsFrozenDuringProposal`), so they only fit between proposals.

```bash
# Per-call cap on tier-2 (uncertified) calls: 2% of totalAssets per call at propose (Tier2CallCapExceedsCeiling(i))
sherwood governor set-tier2-call-cap --vault $VAULT_ADDRESS --bps 200
# Ceiling on --max-capital: 80% of totalAssets, checked at propose and again at execute
sherwood governor set-max-capital --vault $VAULT_ADDRESS --bps 8000
# Idle float the vault must keep after every governor batch: 5% (max 5000 = 50%, BufferTooHigh)
sherwood vault set-min-buffer --vault $VAULT_ADDRESS --bps 500
# or: cast send $VAULT_ADDRESS "setMinBufferBps(uint16)" 500 --private-key $PRIVATE_KEY --rpc-url $RPC_URL
```

`setMinBufferBps` is a vault setter with no proposal gate, so it *can* be raised mid-proposal. **Never do that**: the buffer is enforced after each batch (`BufferBreached`), and a raise while a proposal is Executed can make its settlement revert. Lower it, if ever, only between proposals.

While the Portfolio class is uncertified (SHE-235 pending), every Portfolio call is tier 2, so the 200 bps ceiling caps each call at 2% of the vault's assets. Certification at 2,000 bps lifts the class out of tier 2.

Then whitelist depositors (`depositsRestricted` is on, so `--open-deposits` has no effect until it lifts):

```bash
sherwood vault approve-depositor --vault $VAULT_ADDRESS --depositor 0xLP     # one per call; DepositorAlreadyApproved on a repeat
sherwood vault remove-depositor  --vault $VAULT_ADDRESS --depositor 0xLP     # DepositorNotApproved if absent
cast call $VAULT_ADDRESS "isApprovedDepositor(address)(bool)" 0xLP --rpc-url $RPC_URL
```

"Only your own vault" is enforced only by this whitelist: anyone you approve can deposit, and nobody you did not approve can. Depositors need no WOOD.

Your owner stake (`sherwood guardian prepare-owner-stake 10000` before create; `requiredOwnerBond` is 10,000 WOOD at launch) stays bound to the vault. Requesting its unstake makes `ownerBondLive` false, and propose and execute then revert `OwnerBondNotLive`. Keep it bound while you operate.

---

## 1. Review your own proposal before you send it

At launch you are the proposer. `sherwood strategy propose` / `proposal create` pin metadata, clone the strategy, **pull the proposer bond in WOOD** and lock redemptions and deposits for everyone until the proposal reaches a terminal state. Check all of this first:

1. **Provenance.** Core templates only (Portfolio, Morpho Supply, Concentrated Liquidity). `sherwood strategy list` shows what is deployed on 4663. Anything from `sherwood-strategies`, or hand-written, is unaudited: see [Strategies from `sherwood-strategies` are unaudited](../../SKILL.md#strategies-from-sherwood-strategies-are-unaudited).
2. **Simulate the call files** before proposing: `sherwood proposal simulate --vault $VAULT_ADDRESS --execute-calls execute.json --settle-calls settle.json`. Read the risk codes (§2).
3. **The bond.** Quote it live; the CLI prints it. It is 1% of required coverage in USD, converted at the **haircut** WOOD price (50% haircut at launch), so an uncertified Portfolio bond is about 4% of deployed capital in WOOD at spot, about 0.8% once the class is certified. Never treat a fixed WOOD figure as the requirement. It comes back on any terminal state: `sherwood proposal reclaim-bond --vault $VAULT_ADDRESS --id <id>` (after a Settled proposal, only once the 14-day challenge window has closed; `ChallengeWindowOpen` until then).
4. **Caps.** `--max-capital` ≤ 80% of `totalAssets()` (§0); each tier-2 call cap ≤ 2% of `totalAssets()`; Σ caps ≤ `--max-capital` (`CallCapsExceedMaxCapital`). Required coverage is capped at $1M per proposal (`CoveredTvlCapExceeded`); while uncertified that is about 2× the deployed capital.
5. **Duration** 1 h – 30 d. Cooldown 1 h after the last proposal ended (`CooldownNotElapsed`).
6. **The weekend feed gap.** Chainlink stock feeds on 4663 gap 52–55 h every weekend against the Portfolio template's 26 h `MAX_PUSH_PRICE_AGE`. A Portfolio holding stock tokens **cannot execute, settle or rebalance from about Saturday 18:40 to Monday 00:00 UTC** (`StalePrice`). With the default 24 h vote + 24 h review + 24 h execution window, an execution window that falls inside the gap expires the proposal. Propose so that both the execution window and the end of the strategy land on weekdays.
7. **Splits.** Do not hold a stock slot across an announced split; the feed basis changes under the position.
8. **Unstamped redeems from the last proposal.** A redeem queued while a proposal was open is stamped only when that proposal **settles**. If it ended Cancelled, Rejected or Expired, the request is never stamped: `claim` reverts `NotSettled` forever, its shares stay in the queue and lower every LP's `maxRedeem`, and the only exit is the request owner's `sherwood queue cancel --vault $VAULT_ADDRESS --id <requestId>`. Before you propose again, check `sherwood queue list --vault $VAULT_ADDRESS` and have each LP cancel and re-request (or redeem instantly while nothing is open). A queued **deposit** under such a proposal is still claimable at the live price once nothing is open.
9. **Confirm with the user** (vault, strategy, amounts, duration, bond) and wait for an explicit yes.

Once sent, you can still `sherwood proposal cancel --vault $VAULT_ADDRESS --id <id>` as proposer in Draft, Pending (until `voteEnd`), GuardianReview (until the review ends or block quorum is reached) and Approved. Each cancel keeps LP redemptions locked for the time already spent and restarts the 1 h cooldown.

---

## 2. Proposal monitoring (other proposers, once `ownerOnlyProposals` lifts)

```bash
sherwood proposal list --vault $VAULT_ADDRESS --state pending      # draft | pending | approved | executed | settled | all
sherwood proposal show <id> --vault $VAULT_ADDRESS                 # metadata, state, strategy + provenance, decoded calls
```

**Step 1 — Read the metadata** (`curl -s "https://ipfs.io/ipfs/<CID>" | jq .`).

**Step 2 — Decode every call** in both slices; these two arrays are the whole committed payload:
```bash
cast call $GOVERNOR_ADDRESS "getExecuteCalls(uint256)((address,bytes,uint256)[])" <id> --rpc-url $RPC_URL
cast call $GOVERNOR_ADDRESS "getSettlementCalls(uint256)((address,bytes,uint256)[])" <id> --rpc-url $RPC_URL
cast 4byte <selector>; cast calldata-decode "fn(type1,type2)" <calldata>
```
Every non-asset target must be a strategy registered on `StrategyFactory` (`NotARegisteredStrategy` otherwise); registration is permissionless, so it proves nothing about the code.

**Step 3 — Simulate** (necessary, not sufficient):
```bash
sherwood proposal simulate --vault $VAULT_ADDRESS --id <id> [--notify <subdomain>]   # --notify posts a RISK_ALERT to the vault chat
```

| Code | Level | Meaning |
|------|-------|---------|
| `SIMULATION_FAILED` | critical | Call reverted during fork simulation |
| `UNKNOWN_TARGET` | critical | Target not in the known address registry |
| `UNDECODED_CALLDATA` | critical | Calldata cannot be decoded AND target is unknown |
| `TRANSFER_TO_UNKNOWN` / `TRANSFER_FROM_TO_UNKNOWN` | critical | Funds sent to an unlabeled address |
| `APPROVE_TO_UNKNOWN` | critical | Allowance granted to an unlabeled address |
| `SHORT_STRATEGY_DURATION` / `LONG_STRATEGY_DURATION` | warning | Under 1 hour / over 30 days |
| `ALL_TARGETS_VERIFIED` / `ALL_CALLS_DECODED` | info | Known targets / decoded calls |

`RISK ASSESSMENT: CLEAN` is not a pass. Still read the metadata and the decoded calls.

**Step 4 — Provenance.** `sherwood proposal show <id> --vault $VAULT_ADDRESS` prints where the strategy came from ([Where a proposal's strategy came from](../../SKILL.md#where-a-proposals-strategy-came-from)): a core template (fine; a withdrawn template approval is a warning), a `sherwood-strategies` or unrecognised template (unaudited: run the checks before letting it pass), hand-written or unreadable (unverified code, the highest risk: veto while you can). Then confirm `cast call <strategy> "vault()(address)"` is your vault. `name()` is self-reported and is not provenance.

### Red flags — veto if any apply

Unknown or unverified targets; `approve`/`transfer` to an EOA; a settle leg that `transfer`s the asset anywhere but the vault; value movement the description does not explain; undecodable calldata; missing metadata; a hand-written strategy; a stock Portfolio whose windows land in the weekend gap.

### Veto and emergency cancel

| Action | How | When |
|---|---|---|
| **Veto** | `cast send $GOVERNOR_ADDRESS "vetoProposal(uint256)" <id> --private-key $PRIVATE_KEY --rpc-url $RPC_URL` (no CLI command) | Owner only, **Pending only**; sets `Rejected`. Reverts `ProposalNotCancellable` once GuardianReview begins; from there only guardian block quorum (the `guardian` skill) or the proposer's cancel can stop it |
| **Emergency cancel** | `sherwood proposal cancel --vault $VAULT_ADDRESS --id <id> --emergency` (`emergencyCancel`) | Owner only, **Draft or Pending**; sets `Cancelled` |

Shareholders can also vote Against (`sherwood proposal vote --vault … --id … --support against`, from one second after the propose block); the threshold is 20% of votable supply (queued shares carry no weight), floored at one vote.

```
New proposal
+-- metadata unreachable ----------------------------> VETO
+-- simulate: CRITICAL ------------------------------> VETO
+-- provenance hand-written / unreadable ------------> VETO
+-- WARNING only / CLEAN ----------------------------> read metadata + decoded calls; no veto on simulation grounds alone
```

A vetoed honest proposal can be resubmitted. Drained funds cannot be recovered.

---

## 3. Live strategy monitoring

```bash
sherwood proposal list --vault $VAULT_ADDRESS --state executed
sherwood proposal show <id> --vault $VAULT_ADDRESS                     # executedAt, duration, capital snapshot
cast call $GOVERNOR_ADDRESS "getCapitalSnapshot(uint256)(uint256)" <id> --rpc-url $RPC_URL
cast call $VAULT_ADDRESS "totalAssets()(uint256)" --rpc-url $RPC_URL
cast call $VAULT_ADDRESS "redemptionsLocked()(bool)" --rpc-url $RPC_URL   # true from propose until a terminal state
```

For each Executed proposal:

1. **Track `totalAssets()` against the capital snapshot.** The management fee (200 bps/yr on the whole fund) accrues only while Executed; the performance fee is charged on profit above the high-water mark at settle, clamped to the governor's `maxPerformanceFeeBps`.
2. **Know when it can settle.** You (the proposer) may settle 1 h after execute; anyone may settle once `executedAt + strategyDuration` has passed. For a stock Portfolio, settle outside the weekend gap (§1 item 6).
3. **Pre-simulate the settle** a day before: `sherwood proposal simulate --vault $VAULT_ADDRESS --id <id>`.
4. **Settle promptly** — every hour of lock is an hour LPs cannot exit:
   ```bash
   sherwood proposal settle --vault $VAULT_ADDRESS --id <id>          # routes to settleProposal
   ```
   `StrategyNotSettled(strategy)`: the voted calls ran but the strategy still reports `executed()`, so capital would stay on the clone. Retrying replays the same calls and reverts again; go to §5. `SettlePriceBelowFloor`: price per share fell below the drawdown the proposal declared (capped at 90%); the same emergency path applies.
5. **Do not pause to "protect" an Executed vault**: pause blocks `executeGovernorBatch`, so execute and settle both revert until you unpause.

---

## 4. Owner powers

### Vault

| Action | Command | Notes |
|---|---|---|
| Pause / unpause | `cast send $VAULT_ADDRESS "pause()"` / `"unpause()"` | Freezes deposit, mint, withdraw, redeem, both queue requests, queue deposit claims **and governor batches**. Rescues, queue `cancel` and queue redeem claims still work |
| Set the agent fee | `sherwood vault set-agent-fee --vault $VAULT_ADDRESS --bps <n>` | Default 2000 (20%), max 2500 (`AgentFeeTooHigh`). Snapshotted onto each proposal at propose, clamped at settle to the governor's `maxPerformanceFeeBps` (default 2000) |
| Min buffer | `sherwood vault set-min-buffer --vault $VAULT_ADDRESS --bps <n>` | ≤ 5000; see §0 |
| Depositors | `sherwood vault approve-depositor` / `remove-depositor` | §0. `setOpenDeposits(bool)` (`cast send`) only takes effect once the factory's `depositsRestricted` lifts |
| Register / remove an agent | `sherwood vault add --vault $VAULT_ADDRESS --wallet 0xAgent --agent-id <id>` / `cast send $VAULT_ADDRESS "removeAgent(address)" 0xAgent` | Max 32 agents. The agent must own the ERC-8004 id (or you must). At launch a non-owner agent still cannot propose |
| Rescue | `cast send $VAULT_ADDRESS "rescueEth(address,uint256)" <to> <wei>`, `"rescueERC721(address,uint256,address)" <token> <id> <to>`, `"rescueERC20(address,address,uint256)" <token> <to> <amt>` | All revert `RedemptionsLocked` while any proposal is open. `rescueERC20` cannot move the vault asset (`CannotRescueAsset`) and `to` must be one of **this vault's own strategy clones** (`RescueRecipientNotStrategy`) |
| Rotate owner | `cast send $SYNDICATE_FACTORY "rotateOwner(address,address)" $VAULT_ADDRESS <newOwner>` | `transferOwnership` on the vault always reverts (`NotFactory`). Gates: `VaultStillStaked` while an owner stake is bound (`requestUnstakeOwner(vault)` on sWOOD → 7 d → `claimUnstakeOwner`, both refused while a proposal is open or active); no open proposal for a new owner; the new owner must have a prepared stake and have called `approveOwnerStakeBinding(vault)`. Rotation drains the agent roster |

### Governor (owner only, frozen while any proposal is open — Drafts count)

```bash
sherwood governor set-voting-period    --vault $VAULT_ADDRESS --seconds <n>   # 1 h – 3 d (default 24 h)
sherwood governor set-execution-window --vault $VAULT_ADDRESS --seconds <n>   # 1 h – 7 d (default 24 h)
sherwood governor set-veto-threshold   --vault $VAULT_ADDRESS --bps <n>       # 2000 – 8000 (default 2000)
sherwood governor set-max-fee          --vault $VAULT_ADDRESS --bps <n>       # ≤ 2500 (default 2000)
sherwood governor set-max-duration     --vault $VAULT_ADDRESS --seconds <n>   # 1 h – 30 d (default 30 d)
sherwood governor set-cooldown         --vault $VAULT_ADDRESS --seconds <n>   # 1 h – 30 d (default 1 h)
# cast only:
cast send $GOVERNOR_ADDRESS "setMinStrategyDuration(uint256)" <s>   # 1 h – max duration (default 1 h)
sherwood governor set-max-capital --vault $VAULT_ADDRESS --bps <bps>       # 1 – 10000 (unset reads 10000)
sherwood governor set-tier2-call-cap --vault $VAULT_ADDRESS --bps <bps>     # 1 – 10000 (unset reads 10000)
```

Each applies instantly and emits `ParameterChangeFinalized`. `set-mgmt-split` / `set-perf-split` are protocol-owner only (ProtocolConfig); as vault owner you receive 10% of the performance fee by the launch split.

---

## 5. Recovering a stuck Executed proposal

**Symptom.** State `Executed` (6), `strategyDuration` elapsed, `redemptionsLocked()` true, and `settleProposal(id)` reverts.

```bash
cast call $GOVERNOR_ADDRESS "getProposalState(uint256)(uint8)" <id> --rpc-url $RPC_URL   # 6
cast call $GOVERNOR_ADDRESS "getActiveProposal()(uint256)"        --rpc-url $RPC_URL   # <id>
cast call $GOVERNOR_ADDRESS "settleProposal(uint256)" <id>         --rpc-url $RPC_URL --trace 2>&1 | tail -30
```

There is no owner transaction that immediately runs arbitrary calls. Owner-supplied calldata is committed, reviewed by staked guardians, then finalized.

| Function | What it does | Owner bond | Guardian review |
|---|---|---|---|
| `unstick(id)` | Replays the **already-voted** settlement calls with the same caps and zero egress budget. Past duration only. Same `StrategyNotSettled` check as settle | Not required | No |
| `emergencySettleWithCalls(id, calls)` | Commits **new** unwind calldata and opens a registry emergency review for `reviewPeriod` (24 h). Calls do **not** run in this tx | `ownerStake(vault) >= requiredOwnerBond(vault)` (10,000 WOOD at launch), read **after** the review opens so a prior blocked round slashes first; else `OwnerBondInsufficient` | Yes |
| `cancelEmergencySettle(id)` | Recalls the open window (`EmergencyNotProposed` if none) | — | Closes it |
| `finalizeEmergencySettle(id)` | After `reviewPeriod`: reverts `EmergencySettleBlocked` if guardians blocked, else runs the committed calls uncapped with zero net egress and finishes settlement. **Exempt from `StrategyNotSettled`** | Stake must be nonzero | Resolves |

A guardian block of the emergency review burns the **whole** owner bond (`slashOwnerBond`) and deletes the slot: `ownerBondLive` turns false and you cannot propose or execute until you re-bond (`prepareOwnerStake` + `approveOwnerStakeBinding(vault)` on sWOOD, then `factory.rotateOwner(vault, <yourself>)`; a same-owner rotation is allowed mid-proposal). Rotation drains the agent roster, so re-register yourself afterwards (`sherwood vault add --vault $VAULT_ADDRESS --wallet <owner> --agent-id <id>`) or propose reverts `NotRegisteredAgent`. Never open this path with drain-shaped calldata.

```bash
REG=$(cast call $GOVERNOR_ADDRESS "guardianRegistry()(address)" --rpc-url $RPC_URL)
cast call $REG "ownerStake(address)(uint256)"        $VAULT_ADDRESS --rpc-url $RPC_URL
cast call $REG "requiredOwnerBond(address)(uint256)" $VAULT_ADDRESS --rpc-url $RPC_URL
cast call $REG "reviewPeriod()(uint256)"                             --rpc-url $RPC_URL   # 86400
```

### Playbook

**Step 1 — Replay the voted batch** if it is still the right unwind (not a place to inject new calldata):
```bash
cast send $GOVERNOR_ADDRESS "unstick(uint256)" <id> --private-key $PRIVATE_KEY --rpc-url $RPC_URL
# or: sherwood proposal unstick --vault $VAULT_ADDRESS --id <id> [--dry-run]
```
If it reverts (including `StrategyNotSettled`), the voted batch is the problem; continue.

**Step 2 — Build the real unwind** that guardians will review: the strategy's `settle()` if it can still run, a Morpho withdraw, a Uniswap position decrease + collect, or the smallest honest batch that returns the asset to the vault. Targets must be the vault asset or this vault's registered strategy clones. Write `unwind.json` as `[{ "target": "0x…", "data": "0x…", "value": "0" }]` and dry-run it:
```bash
sherwood proposal simulate --vault $VAULT_ADDRESS --settle-calls unwind.json
```

**Step 3 — Open the bonded window** (calls still do not run):
```bash
cast send $GOVERNOR_ADDRESS "emergencySettleWithCalls(uint256,(address,bytes,uint256)[])" <id> "[($TARGET,$DATA,0)]" \
  --private-key $PRIVATE_KEY --rpc-url $RPC_URL
# or: sherwood proposal settle --vault $VAULT_ADDRESS --id <id> --calls unwind.json   (opens the window only)
cast call $REG "isEmergencyOpen(address,uint256)(bool)" $GOVERNOR_ADDRESS <id> --rpc-url $RPC_URL
```
Wrong calldata? `cast send $GOVERNOR_ADDRESS "cancelEmergencySettle(uint256)" <id> …` before it resolves (no slash).

**Step 4 — Finalize after `reviewPeriod`** (owner only, no CLI wrapper):
```bash
cast send $GOVERNOR_ADDRESS "finalizeEmergencySettle(uint256)" <id> --private-key $PRIVATE_KEY --rpc-url $RPC_URL
cast call $GOVERNOR_ADDRESS "getActiveProposal()(uint256)" --rpc-url $RPC_URL   # 0
cast call $VAULT_ADDRESS "redemptionsLocked()(bool)"       --rpc-url $RPC_URL   # false
```

**Step 5 — Let LPs exit**: `sherwood vault redeem --vault $VAULT_ADDRESS [--shares <n>]` from each holder, and have anyone with a queued redeem claim it (`sherwood queue claim --vault $VAULT_ADDRESS --id <requestId>`).

### What this path cannot recover

| State | Exit |
|---|---|
| `Draft` (0) / `Pending` (1) | Proposer `cancel`; owner `vetoProposal` (Pending) or `--emergency` cancel. The proposal expires on its own after the collaboration deadline or the execution window |
| `GuardianReview` (2) | Proposer `cancel` (until review end / quorum) or guardian block quorum. Owner veto reverts |
| `Approved` (3) | Proposer `cancel`, or let the execution window pass → `Expired` |
| `Rejected` (4) / `Expired` (5) / `Cancelled` (8) | Terminal; nothing locked. Reclaim the bond. **Unstamped queued redeems must be cancelled by their owners** (§1 item 8) |
| **`Executed` (6)** | **This section**, after the duration |
| `Settled` (7) | Done. Bond reclaimable after the 14-day challenge window |

A proposal that is terminal but still counted open (`propose` reverts `VaultHasOpenProposal`) needs a flush: `cast send $GOVERNOR_ADDRESS "resolveProposalState(uint256)" <id>`.

---

## 6. Heartbeat

| Interval | Check |
|---|---|
| Hourly | Executed strategies: `totalAssets()` vs snapshot, time to duration, weekend gap ahead, settle as soon as allowed |
| Every 15 min, once `ownerOnlyProposals` lifts | New Pending proposals: show, simulate, decide (§2). The vote closes 24 h after propose |
| Before every propose | §1 checklist; `queue list` for unstamped redeems; governor params still as seeded |
| Daily | `sherwood proposal list --vault $VAULT_ADDRESS`, `sherwood vault info --vault $VAULT_ADDRESS`, `sherwood governor info --vault $VAULT_ADDRESS`, owner stake still live (`ownerBondLive`) |

---

## 7. Reference

```
0 Draft  1 Pending (owner can veto)  2 GuardianReview (veto reverts)  3 Approved  4 Rejected
5 Expired  6 Executed (settle after duration)  7 Settled  8 Cancelled
```
Read the state with `getProposalState(id)`; `state` is a mid-struct `uint8` in `getProposal`, so do not rely on tuple position.

`BatchExecutorLib.Call { address target; bytes data; uint256 value; }`

| Parameter | Min | Max | Default |
|---|---|---|---|
| Voting period | 1 h | 3 d | 24 h |
| Execution window | 1 h | 7 d | 24 h |
| Veto threshold | 2000 bps | 8000 bps | 2000 |
| Max performance fee | — | 2500 bps | 2000 |
| Strategy duration | 1 h | 30 d | 1 h – 30 d |
| Cooldown | 1 h | 30 d | 1 h |
| Review period (registry) | 6 h | 3 d | 24 h |

Errors you will meet as owner: `ParamsFrozenDuringProposal` (setter while a proposal is open), `OwnerBondNotLive` (owner unstake requested or bond burned), `OwnerBondInsufficient`, `EmergencySettleBlocked`, `StrategyNotSettled`, `SettlePriceBelowFloor`, `RedemptionsLocked` (rescue while open), `RescueRecipientNotStrategy`, `AgentFeeTooHigh`, `BufferTooHigh` / `BufferBreached`, `MaxCapitalExceedsCeiling` / `MaxCapitalCeilingRegressed`, `Tier2CallCapExceedsCeiling(i)`, `NotSettled` (an LP claiming an unstamped redeem). Full table: [ERRORS.md](../../ERRORS.md).

## Further reading

- [Governance overview](https://docs.sherwood.sh/protocol/governance/overview) · [Settlement](https://docs.sherwood.sh/protocol/governance/settlement) · [Deployments](https://docs.sherwood.sh/reference/deployments)
- Guardian skill (staked review, a different job): [`skills/guardian/SKILL.md`](../guardian/SKILL.md)
