---
name: launchpad
model: sonnet
description: Launch a fund token ("IPO") on Sushi Launchpad V2 or StonkBrokers with vault capital via a governance proposal, then run the claim window, fee collection and settlement. A launch settles as a vault-asset LOSS of about the capital spent, by design. Triggers on launch a token, fund token, IPO, launchpad, Sushi launchpad, StonkBrokers, reserve claim, claim launch tokens, collect launch fees.
allowed-tools: Read Glob Grep Bash(sherwood:*) Bash(npm:*) Bash(npx:*) WebFetch WebSearch AskUserQuestion
license: MIT
metadata:
  author: sherwood
  version: '0.3.0'
---

# Launchpad Strategy

> **Not deployed on Robinhood Chain (4663), where Sherwood is live.** `LaunchpadStrategy` and its launch adapters come from `sherwood-strategies`, which has no deployment book for 4663 yet. `sherwood strategy list` shows `Launchpad (launchpad)` under "Not available" until it ships; do not propose it before then. The workflow below is kept for that day.

> **Unaudited.** `LaunchpadStrategy` and its launch adapters come from `sherwood-strategies` (`src/launchpad/`), which has not been audited. Before proposing or approving a launch, run the checks in [Strategies from `sherwood-strategies` are unaudited](../../../SKILL.md#strategies-from-sherwood-strategies-are-unaudited) on the template and the adapter for the venue, and tell the user it is used at their own risk.

A `LaunchpadStrategy` proposal launches a token for the fund with vault capital. It holds back a **reserve** of that token, and the fund's share holders **claim a pro-rata slice** of the reserve during a claim window. Shares are not burned. Two venues: **Sushi Launchpad V2** (`--venue sushi`, default) and **StonkBrokers** Smart Launch V2 (`--venue stonk`).

Requires `@sherwoodagent/cli@0.93.2` or later. Run `sherwood strategy list` and confirm `Launchpad (launchpad)` is listed as available on 4663 before proposing.

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
4. **Do not use WOOD as the quote.** It is not supported on either venue yet. Use USDG or WETH.
5. **Do not propose on Pons.** It is out of scope; its factory only launches for whitelisted addresses.
6. **Size `--min-tokens-out` from a real quote**, not a token placeholder. A floor of a few tokens hands a sandwich budget to anyone watching the vote. On Sushi it must be at least `--reserve`.
7. **Collect fees often when the quote is the vault asset.** Those fees raise `totalAssets` the moment they land. With no proposal open, someone can deposit, call `collect-fees`, and redeem with a cut. Run `sherwood launchpad collect-fees` on a schedule so no large lump is ever pending.
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
| `--venue` | `sushi` | `stonk` |
| Quotes | Any quote with a Sushi price feed: USDG, WETH today | One pad per quote: WETH, STONK, USDG, GME, NVDA, AAPL, SPCX, USO |
| Reserve source | The dev buy, so `--min-tokens-out >= --reserve` | Kept from the minted supply |
| Trading | Live from the launch block | Bonding curve, then `sherwood launchpad finalize` graduates and bonds a locked LP |
| Launch fee | Native fee, read live (`--max-fee-in` caps it) | None |
| Creator fees to the vault | 70% of LP fees in both assets (`--fee-mode direct`), or `burn` / `buyback` | 16.5% of the per-trade tax |
| Hazards | A quote feed older than 3 days refuses the launch | Stock lanes revert `StalePrice` on oracle gaps such as weekends |

Sushi's owner can re-point a launch's fee receiver and upgrade the launchpad. On Sushi the fund trusts that owner with its fee stream, never with the reserve or vault capital.

### Step 3: Propose

The CLI checks every `_initialize` condition before sending anything: both adapters allowlisted, quote supported, Sushi feed age, the live launch fee, the 20% reserve cap and the claim window. It reports all failures in one message.

```bash
sherwood strategy propose launchpad \
  --vault <vault> \
  --venue sushi --quote USDG \
  --asset-in 1000 \
  --reserve 10% \
  --min-tokens-out 120000000 \
  --claim-window 7d \
  --token-name "Robin Fund" --token-symbol ROBIN \
  --name "Launch ROBIN" \
  --description "Launch ROBIN on Sushi with 1,000 USDG; holders claim a 10% reserve for 7 days" \
  --duration 8d
```

Required flags:

| Flag | Meaning |
| --- | --- |
| `--asset-in <n>` | Vault-asset budget for the launch, human units |
| `--quote <token>` | Quote token the fund token pairs against, symbol or address |
| `--reserve <n>` | Launch tokens held back for claims, whole tokens or a percent of supply like `10%`; at most 20% |
| `--min-tokens-out <n>` | Slippage floor on the dev buy, whole launch tokens; `>= --reserve` on Sushi |
| `--claim-window <duration>` | How long holders may claim, e.g. `7d`; at most 14d, clamped to the proposal duration − 5m. The keeper distributes the reserve during it, so give it at least an hour |
| `--token-name <name>` / `--token-symbol <symbol>` | Launch token metadata |

Common optional flags: `--venue` (default `sushi`), `--max-drawdown-bps` (default derived; see hard rule 2), `--min-quote-out`, `--deadline`, `--settle-slippage-bps` (1-1000, default 500), `--max-fee-in` (default live cost + 25%; lower than live cost is refused), `--quote-swap-route`, `--fee-swap-route`, `--launch-adapter`.

Sushi only: `--liquidity standard|moon` (default `standard`), `--fee-mode direct|burn|buyback` (default `direct`).

StonkBrokers only: `--stonk-supply` (default 1000000000), `--start-mcap` (default 10000), `--grad-mcap` (default 100000), `--start-tax-bps` (default 1000), `--tax-decay-bps` (default 100), `--sells-disabled`, `--buffer-secs` (default 60), `--unsold-mode burn|lp` (default `burn`), `--open-ended` (requires sells and `--post-tax-bps` 100-500), `--post-tax-bps`, `--bond-venue` (default 0), `--max-buy-ppm` (default 0; a cap also caps the fund's dev buy).

Set `--duration` longer than `--claim-window`: the window is clamped to end 5 minutes before the duration does. Settlement opens after the window ends, or when the duration elapses, whichever comes first.

`--min-tokens-out`, `--min-quote-out`, `--settle-slippage-bps` and `--deadline` can change only before execute, and only in the tightening direction.

### Step 4: Execute

```bash
sherwood proposal execute --vault <vault> --id <proposal-id>
sherwood launchpad status <strategy>     # launch token, reserve, snapshot, window end, anyone-settle time
```

On StonkBrokers, once the curve closes:

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

Run it regularly for the life of the launch, not just once (hard rule 7). Fees keep accruing after settlement and still go to the vault.

### Step 7: Settle

```bash
sherwood proposal settle --vault <vault> --id <proposal-id>
```

Settlement converts leftover quote to the vault asset, then pushes the asset, any unconvertible quote, and the unclaimed reserve to the vault. After it, the clone holds nothing. Report the result as expected: a loss of about `--asset-in`, with the reserve distributed to holders in kind.

## Batch calls

```
Execute: [asset.approve(clone, assetIn), clone.execute()]
Settle:  [clone.settle()]
```

The CLI builds both. The launch and swap adapters are counterparties the clone calls, checked against the vault's TierRegistry at init and execute. Settlement and claims never consult the registry.

## Addresses

Not deployed on Robinhood Chain (4663). Once the template and both adapters ship there, their addresses appear in sherwood-strategies `deployments/4663.json` and in [ADDRESSES.md](../../../ADDRESSES.md); `sherwood strategy list` prints the template address. Do not reuse an address from any other chain. Docs: https://docs.sherwood.sh/protocol/strategies/launchpad
