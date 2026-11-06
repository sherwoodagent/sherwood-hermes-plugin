---
name: launchpad
model: sonnet
description: Launch a fund token ("IPO") on Sushi Launchpad V2 with vault capital via a governance proposal, then run the claim window, fee collection and settlement. A launch settles as a vault-asset LOSS of about the capital spent, by design. Triggers on launch a token, fund token, IPO, launchpad, Sushi launchpad, StonkBrokers, reserve claim, claim launch tokens, collect launch fees.
allowed-tools: Read Glob Grep Bash(sherwood *) WebFetch WebSearch AskUserQuestion
license: MIT
metadata:
  author: sherwood
  version: '0.5.2'
---

# Launchpad Strategy

> **Live on Robinhood Chain (4663), Sushi venue only.** The `LaunchpadStrategy` template and the Sushi launch adapter are deployed and approved on the StrategyFactory (addresses [below](#addresses)); the template was redeployed on 2026-10-22 with the `DeadlineInPast` fix. StonkBrokers is **not deployed on 4663**: never propose `--venue stonk` there. This template is class-certified since 2026-10-27 (tier 0, a 3,500 bps cap, execute and settle): coverage and the proposer bond in WOOD are sized by the ledger on 35% of `--asset-in` (about 3,000 WOOD per 1,000 USDG at writing, which was about 1% at the feed price). The CLI quotes the exact figure; never plan on a fixed number.

> **Unaudited.** `LaunchpadStrategy` and its launch adapters come from `sherwood-strategies` (`src/launchpad/`), which has not been audited. Before proposing or approving a launch, run the checks in [Strategies from `sherwood-strategies` are unaudited](../../../SKILL.md#strategies-from-sherwood-strategies-are-unaudited) on the template and the adapter for the venue, and tell the user it is used at their own risk.

A `LaunchpadStrategy` proposal launches a token for the fund with vault capital. It holds back a **reserve** of that token, and the fund's share holders **claim a pro-rata slice** of the reserve during a claim window. Shares are not burned. The CLI knows two venues: **Sushi Launchpad V2** (`--venue sushi`, default) and **StonkBrokers** Smart Launch V2 (`--venue stonk`). Only Sushi is deployed on 4663.

Requires `@sherwoodagent/cli` 0.99.3 or later, which enforces the [mainnet rules on 4663](#mainnet-rules-on-4663). Run `sherwood strategy list` and confirm `Launchpad (launchpad)` is listed as available before proposing.

```
Vault (USDG or WETH)
    ↓ execute: pull --asset-in → swap to the quote → pay the launch fee → launch + dev buy
LaunchpadStrategy clone holds the reserve; vault share supply snapshotted
    ↓ claim window: holders claim reserve × their shares / total supply (at the snapshot)
    ↓ settle: leftover quote → vault asset; push asset, raw quote, unclaimed reserve (in kind) to the vault
Vault (asset minus ~--asset-in; launch token and fees held outside totalAssets)
```

## Hard rules

1. **A launch settles as a LOSS of about `--asset-in`. This is by design on v1.** v1 measures P&L as the vault's asset balance change and books no value for the launch token. The reserve is a dividend in kind to holders, not a vault asset. Tell the user this in plain words before proposing, and do not describe a launch as a yield or profit strategy.
2. **Never pass a `--max-drawdown-bps` below what the CLI derives.** The CLI uses `ceil(assetIn / totalAssets × 10000) + 200` bps when the flag is omitted and refuses anything lower, because a lower value makes `settleProposal` revert `SettlePriceBelowFloor`. The safe default is to omit the flag. If the CLI says the required value is above 9000 bps, lower `--asset-in`; do not try to work around it.
3. **Never use `--fee-mode holders`.** Sushi's `DISTRIBUTE_TO_HOLDERS` is refused: nothing in this stack could collect those fees for the vault.
4. **Quote in the vault asset unless there is a reason not to.** A USDG vault launching against USDG has no swap leg. A launch quoted in another supported token (a stock token such as TSLA) swaps the asset into it at execute, and the CLI guards that swap ([mainnet rules](#mainnet-rules-on-4663)). It gets the same live deadline as an asset-quoted launch and is executable as voted; `sherwood launchpad arm` can tighten its floor and deadline right before execute, but is optional. WOOD is not a registered Sushi quote.
5. **Do not propose on Pons.** It is out of scope; its factory only launches for whitelisted addresses.
6. **On Sushi, size `--reserve` from the measured launch and omit `--min-tokens-out`.** The floor defaults to `--reserve` and must equal it. A floor of a few tokens would hand a sandwich budget to anyone watching the vote; a floor above the reserve strands tokens in the vault.
7. **Collect fees often.** On a launch quoted in the vault asset, the quote half of the fees raises `totalAssets` the moment it lands. With no proposal open, someone can deposit, call `collect-fees`, and redeem with a cut. Sherwood's keeper sweeps fees to the vault on a short cadence so no large lump is pending; `sherwood launchpad collect-fees` is the manual fallback if it is down.
8. **Never sell the fund token to "recover" value.** The template never does, because that price can be moved inside the selling transaction. Launch tokens that reach the vault are disposed of by a later proposal.

## Workflow

### Step 1: Prerequisites

```bash
sherwood config show
sherwood strategy list                    # Launchpad must be listed
sherwood vault info --vault <vault>       # asset, totalAssets
```

The proposer must be a registered agent on the vault and hold WOOD for the proposer bond (see the main skill).

### Step 2: Choose the venue and quote

| | Sushi Launchpad V2 | StonkBrokers |
| --- | --- | --- |
| On 4663 | **Deployed** | **Not deployed**: do not propose |
| `--venue` | `sushi` | `stonk` |
| Quotes | Any quote with a Sushi price feed: USDG, WETH, TSLA, NVDA and SPCX at writing. The default is the vault asset | One pad per quote: WETH, STONK, USDG, GME, NVDA, AAPL, SPCX, USO |
| Reserve source | The dev buy, so `--min-tokens-out` equals `--reserve` | Kept from the minted supply |
| Trading | Live from the launch block | Bonding curve, then `sherwood launchpad finalize` graduates and bonds a locked LP |
| Launch fee | Native fee, read live (`--max-fee-in` caps it) | None |
| Creator fees to the vault | 70% of LP fees in both assets (`--fee-mode direct`), or `burn` / `buyback` | 16.5% of the per-trade tax |
| Hazards | A quote feed older than 3 days refuses the launch | Stock lanes revert `StalePrice` on oracle gaps such as weekends |

Sushi's owner can re-point a launch's fee receiver and upgrade the launchpad. On Sushi the fund trusts that owner with its fee stream, never with the reserve or vault capital.

### Step 3: Propose

The CLI checks every `_initialize` condition before sending anything: both adapters allowlisted, quote supported, Sushi feed age, the live launch fee, the 20% reserve cap and the claim window. It reports all failures in one message.

#### Mainnet rules on 4663

CLI 0.99.3 enforces these on Robinhood Chain (4663):

1. **The quote defaults to the vault asset; any venue-supported quote is allowed.** A USDG vault launching against USDG has no swap leg. Another `--quote` the venue supports (`quoteSupported`) runs an asset-to-quote swap at execute. That swap's floor is a voted number, and anyone can execute the proposal, so the CLI guards it (see [Quoting in another token](#quoting-in-another-token)).
2. **`--min-tokens-out` defaults to `--reserve` on Sushi.** Omit it. An explicit value above `--reserve` is refused: tokens the dev buy delivers above the reserve are never claimable and reach the vault in kind, where nothing can dispose of them. A value below `--reserve` is refused too.
3. **Size `--reserve` from a measured launch.** Sushi V2 has no dev-buy quoter, so the CLI cannot quote the dev buy. The measured reference is about 164.56M tokens per 1,000 USDG at writing (the supply is 1B, so that is about 16.5%, under the 20% cap). The launch fee also comes out of `--asset-in`, and a dev buy that returns less than the reserve reverts at execute, so set `--reserve` a little under the measured figure.

#### Quoting in another token

When `--quote` is not the vault asset (say TSLA on a USDG vault), the CLI adds these rules. Each one refuses the proposal; none is clamped.

- **Floor.** `--min-quote-out` defaults to the swap adapter's quote for the swap amount, less `--max-slippage-bps`. The default is 50 bps and the most is 100. A lower `--min-quote-out` is refused; a higher one is kept.
- **Thin pool.** The price impact at `--asset-in` must not exceed `--max-slippage-bps`. The refusal says roughly how much the pool can take. Lower `--asset-in`.
- **Pool vs feed (Sushi).** The quote at size may sit at most 300 bps under what the venue's own asset and quote feeds imply.
- **Size cap.** `--asset-in` is at most 35% of `totalAssets`.
- **Fresh feed.** The venue's quote feed must have updated within 3 days. Stock feeds do not update on weekends or market holidays, so propose and execute (and arm, if you do) on market days.
- **Live deadline.** The launch gets the same deadline as an asset-quoted one: `--deadline` (a duration such as `3d`, or a timestamp), otherwise the governor windows plus one day. `0` means no deadline. A deadline already past is refused (`DeadlineInPast`; the template refuses it at init too). The launch is executable as voted: anyone can execute it at the voted floor until its deadline or `executeBy`, so the floor above is what protects the swap.
- **Optional: arm right before execute.** The proposer may tighten the launch first. Nothing requires it.

```bash
sherwood launchpad arm <strategy>                  # re-quote, raise minQuoteOut, set a deadline 10 minutes out
sherwood launchpad arm <strategy> --swap-deadline 30m --max-slippage-bps 50
sherwood proposal execute --vault <vault> --id <proposal-id>   # right after; the window is public
```

`arm` re-runs the feed, thin-pool and feed-skew checks at current prices, then calls `updateParams` with `minQuoteOut` raised to the fresh floor (never lowered) and a deadline at most 1h out. It refuses when the pool now quotes under the voted floor, when you are not the proposer, when the clone has executed, or when the launch is quoted in the vault asset. If propose printed a route other than the default, pass it again with `--quote-swap-route`. With a sign-only wallet, add `--calldata-only --proposer <address>`. `sherwood launchpad status` shows the current deadline.

- **Fees and settle.** Creator fees arrive at the vault in the quote token and the launch token. Nothing converts them, and v1 `totalAssets` does not count them; only a later proposal can dispose of them. At settle, leftover quote (dust) is swapped back to the asset if it can be, and pushed in kind if not.

Example for a USDG vault, spending 1,000 USDG:

```bash
sherwood strategy propose launchpad \
  --vault <vault> \
  --venue sushi --quote USDG \
  --asset-in 1000 \
  --reserve 155000000 \
  --claim-window 7d \
  --token-name "Robin Fund" --token-symbol ROBIN \
  --name "Launch ROBIN" \
  --description "Launch ROBIN on Sushi with 1,000 USDG; holders claim a 10% reserve for 7 days" \
  --duration 8d
```

Required flags (`--min-tokens-out` is optional on Sushi):

| Flag | Meaning |
| --- | --- |
| `--asset-in <n>` | Vault-asset budget for the launch, human units |
| `--quote <token>` | Quote token the fund token pairs against, symbol or address. The vault asset runs no swap leg; any other venue-supported quote gets the rules in [Quoting in another token](#quoting-in-another-token) |
| `--reserve <n>` | Launch tokens held back for claims, whole tokens or a percent of supply like `10%`; at most 20% |
| `--min-tokens-out <n>` | Slippage floor on the dev buy, whole launch tokens. Sushi: optional, defaults to `--reserve` and must equal it. StonkBrokers: required, nonzero |
| `--claim-window <duration>` | How long holders may claim, e.g. `7d`; at most 14d, clamped to the proposal duration − 5m. The keeper distributes the reserve during it, so give it at least an hour |
| `--token-name <name>` / `--token-symbol <symbol>` | Launch token metadata |

Common optional flags: `--venue` (default `sushi`), `--max-drawdown-bps` (default derived; see hard rule 2), `--min-quote-out`, `--max-slippage-bps` (non-asset quote only; 1-100, default 50), `--deadline` (default: the governor windows plus one day; `0` for none; never in the past), `--settle-slippage-bps` (1-1000, default 500), `--max-fee-in` (default live cost + 25%; lower than live cost is refused), `--quote-swap-route`, `--fee-swap-route`, `--launch-adapter`.

Sushi only: `--liquidity standard|moon` (default `standard`), `--fee-mode direct|burn|buyback` (default `direct`).

StonkBrokers only (not deployed on 4663): `--stonk-supply` (default 1000000000), `--start-mcap` (default 10000), `--grad-mcap` (default 100000), `--start-tax-bps` (default 1000), `--tax-decay-bps` (default 100), `--sells-disabled`, `--buffer-secs` (default 60), `--unsold-mode burn|lp` (default `burn`), `--open-ended` (requires sells and `--post-tax-bps` 100-500), `--post-tax-bps`, `--bond-venue` (default 0), `--max-buy-ppm` (default 0; a cap also caps the fund's dev buy).

Set `--duration` longer than `--claim-window`: the window is clamped to end 5 minutes before the duration does. Settlement opens after the window ends, or when the duration elapses, whichever comes first.

`--min-tokens-out`, `--min-quote-out`, `--settle-slippage-bps` and `--deadline` can change only before execute, and only by the proposer. The floors may only rise and the settle slippage may only fall; the deadline may move to any time in the future (or `0`, none), never to the past (`DeadlineInPast`). `arm` uses this to set a short deadline.

### Step 4: Execute

A launch quoted in another token executes as voted. If you want a fresh floor and a short deadline first, run `sherwood launchpad arm <strategy>` (optional, above) and execute right after.

```bash
sherwood proposal execute --vault <vault> --id <proposal-id>
sherwood launchpad status <strategy>     # launch token, reserve, snapshot, window end, anyone-settle time
```

On StonkBrokers (not deployed on 4663), once the curve closes:

```bash
sherwood launchpad finalize <strategy>   # graduate + bond; a no-op on Sushi
```

### Step 5: Claim window

You do not run the claims. Sherwood's keeper distributes the reserve: every few minutes during the window it calls `claimFor` for each holder at the snapshot, in batches, and pays the gas itself. Tokens always go to the holder, never the caller. A holder who claims first is skipped.

Your part is to leave the keeper time: keep the window at an hour or more. Check progress with `status`. If holders still show a claimable slice near the window's end (for example, the keeper is down), pay them yourself with `claim-for`. Unclaimed reserve goes to the vault in kind at settlement, and no claim is possible after that.

```bash
sherwood launchpad status <strategy> --holder <address>   # entitlement, claimable now, why not
sherwood launchpad claim <strategy>                        # your own slice
sherwood launchpad claim-for <strategy> <holder>           # permissionless; pays the holder, never the caller
```

A claim in the execute block reverts `SnapshotNotFinal`. Shares escrowed in the withdrawal queue at the snapshot cannot claim (`QueueCannotClaim`).

### Step 6: Fees

```bash
sherwood launchpad collect-fees <strategy>   # permissionless; pays the vault, never the caller
```

You normally don't need to. Sherwood's keeper calls `collectFees` on a short cadence (batched through Multicall3) for the life of the launch, including after settlement. Path: on Sushi the venue keeps 30% of trading fees; the creator share goes to the launch clone, which forwards it to the vault in the same call.

Read fees with `sherwood launchpad status`: it shows **uncollected** fees (simulated `collectFees`) and **collected so far** (sum of the clone's `FeesCollected` events). Uncollected 0 on a launch with volume usually means the keeper already swept it — check collected. Preview a manual collection with `sherwood launchpad collect-fees <strategy> --dry-run [--json]`; run it for real only if the keeper is down.

### Step 7: Settle

```bash
sherwood proposal settle --vault <vault> --id <proposal-id>
```

Settlement converts leftover quote to the vault asset when it can, then pushes the asset, any unconvertible quote, and the unclaimed reserve to the vault. After it, the clone holds nothing. Report the result as expected: a loss of about `--asset-in`, with the reserve distributed to holders in kind.

## Batch calls

```
Execute: [asset.approve(clone, assetIn), clone.execute()]
Settle:  [clone.settle()]
```

The CLI builds both. The launch and swap adapters are counterparties the clone calls, checked against the vault's TierRegistry at init and execute. Settlement and claims never consult the registry.

## Addresses

Robinhood Chain (4663). The template was redeployed on 2026-10-22 at block 95,339,932 with the `DeadlineInPast` fix; the Sushi adapter is unchanged from 2026-10-20 (blocks 93,737,582 to 93,737,603). Source: sherwood-strategies `deployments/4663.json` and the factory's `approvedTemplate`, also in [ADDRESSES.md](../../../ADDRESSES.md). Both contracts are verified on Sourcify (exact match).

| Contract | Address |
| --- | --- |
| `LaunchpadStrategy` template (approved on StrategyFactory `0xb06788F027268a9A06c3AD41a88559530D2E54b1`) | `0x1F8221c4176B8B017A6Ef699947BB4A33730Df32` |
| `SushiLaunchAdapter` implementation (each launch uses its own ERC-1167 clone) | `0x52901614748ACFDC083D305Aa5301E484d4B15F8` |
| Sushi Launchpad V2 proxy (venue) | `0xF1716eBf85836ffE2985db9A50dd29e5814caBe9` |

The first template, `0xbc4fDF5949caa74c730bed632E368cEa4ea5832D` (2026-10-20), is no longer approved: no new clones come from it, and a clone that already exists keeps working. The adapter and the Sushi proxy are TierRegistry counterparties. There is no StonkBrokers adapter on 4663. `sherwood strategy list` prints the template address. Do not reuse an address from any other chain. Docs: https://docs.sherwood.sh/protocol/strategies/launchpad
