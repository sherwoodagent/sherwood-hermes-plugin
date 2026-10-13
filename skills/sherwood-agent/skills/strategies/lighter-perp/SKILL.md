---
name: lighter-perp
model: sonnet
description: Agent-traded perpetuals on Lighter (zkLighter) through a LighterPerpStrategy clone that owns the Lighter account — agent key, guardrails, and the three-step exit (initiate-return, queue-withdraw, settle). NOT deployed on Robinhood Chain (4663) yet, where Sherwood is live; open audit findings G-1/G-2 gate it. Triggers on Lighter, zkLighter, perps on Robinhood, lighter keygen, initiate-return, queue-withdraw, prepare-settle, residual margin.
allowed-tools: Read Glob Grep Bash(sherwood:*) Bash(npm:*) Bash(npx:*) Bash(pip:*) WebFetch WebSearch AskUserQuestion
license: MIT
metadata:
  author: sherwood
  version: '0.2.0'
---

# Lighter Perp Strategy

> **Not deployed on Robinhood Chain (4663), where Sherwood is live.** `LighterPerpStrategy` comes from `sherwood-strategies`, which has no deployment book for 4663 yet, and the audit's G-1/G-2 findings (SHE-365) must be closed before the template is approved there. Lighter itself runs on 4663, so this is where the template will deploy. Until then `sherwood strategy propose lighter-perp` cannot run and `sherwood strategy list` shows it under "Not available". Tell the user this plainly if they ask for Lighter perps.

> **Unaudited.** `LighterPerpStrategy` comes from `sherwood-strategies` (`src/lighter/`), which has not been audited. Before proposing or approving it, run the checks in [Strategies from `sherwood-strategies` are unaudited](../../../SKILL.md#strategies-from-sherwood-strategies-are-unaudited) and tell the user it is used at their own risk. As an uncertified tier-2 template, each Lighter proposal books 2× its capital in guardian coverage.

A `LighterPerpStrategy` clone owns its own Lighter perp account, funded with USDG from the vault. The agent trades that account off-chain through Lighter's API with a **trade-only L2 key** the contract registers. The proposer and the vault owner keep an on-chain kill switch: cancel orders, close positions, rotate the key, and drain the account. Requires `@sherwoodagent/cli@0.93.2` or later.

```
Vault (USDG)
    ↓ execute: pull USDG (scaled by approve coverage) → deposit into a strategy-owned Lighter account
Clone owns the account; agent key registered (register-key)
    ↓ agent trades via the Lighter API; guardrails available on-chain
    ↓ exit 1: initiate-return   (cancel all + close every configured market; queues nothing)
    ↓ exit 2: queue-withdraw --all  (after the closes fill; async, minutes to days)
    ↓ prepare-settle            (drain complete → rotate agent key to a burn key)
    ↓ exit 3: settle            (claim matured USDG → push all of it to the vault)
Vault (USDG + PnL)
```

## Hard rules

1. **Do not propose until the template is deployed and the CLI enables it.** Check `sherwood strategy list` first. If `Lighter Perp (lighter-perp)` is under "Not available", stop.
2. **The vault asset must be USDG.** Init reverts `AssetMismatch` otherwise.
3. **The agent key is a hot credential that can lose 100% of the deployed capital.** It cannot withdraw, but it can trade the account into the ground or against an attacker's account. The contract cannot cap size, leverage or trade rate. Size each proposal as if all of it can be lost.
4. **The exit is three steps, in order.** `initiate-return` → wait for the closes to fill → `queue-withdraw --all` → wait for maturity → `prepare-settle` → settle. Never skip a step and never reorder them.
5. **Always use `queue-withdraw --all`.** It reads the true L2 balance from the Lighter API and refuses while any position is open. Never queue a guessed `--ticks` amount: under-queueing stamps the settlement price low and moves value from exiting holders to those who stay. Do not use `--force`.
6. **If `queue-withdraw --all` refuses because a position is open, close it first.** For a market outside the configured list, `sherwood lighter guardrail close-market <strategy> <market>`. Closing works only before settlement.
7. **Rotate the agent key before settle.** Run `sherwood lighter prepare-settle <strategy>`. After settlement nobody can rotate the key, cancel orders or close a market, so a live key could keep trading residual margin.
8. **Never put `rescueTo(USDG)` before `settle()` in one batch.** Settle then reverts `WithdrawalInFlight`, the waiver reverts `NoShortfall`, and only the vault owner's emergency settle gets out. A proposal's settle calls are `[settle()]`. `rescueTo` belongs after settle only.
9. **Recover residual margin after settle promptly.** v1 has no deposit lock for value a settled clone still holds. While it is out, anyone who deposits takes a share of it when it lands. Queue the true balance before settling so nothing is left; if something is left, propose the recovery right away.
10. **Do not acknowledge a shortfall for a slow withdrawal.** `acknowledge-shortfall --yes` is only for a real venue under-fill, such as a partial fill or a liquidation. A slow withdrawal still matures; wait.

## Workflow

### Step 1: Agent key

```bash
pip install lighter-sdk        # keygen needs Lighter's Python SDK (LIGHTER_PYTHON selects the interpreter)
sherwood lighter keygen        # saves the L2 key to ~/.sherwood/config.json (0600)
sherwood lighter markets       # market ids for --markets
```

`keygen` options: `--key-index <n>` (slot 2..254, default 2), `--force` (replace a saved key; the old private key is gone unless backed up), `--print-private-key`, `--json`.

### Step 2: Propose

```bash
sherwood strategy propose lighter-perp \
  --vault <vault> \
  --deposit 1000 \
  --markets 0,1 \
  --name "Perps book" \
  --description "Agent-traded perps on Lighter, markets 0 and 1, 7 days" \
  --duration 7d
```

| Flag | Meaning |
| --- | --- |
| `--deposit <usdg>` | USDG deposit ceiling, human units (alias `--amount`). At least 1 USDG. Deployed scaled by approve coverage |
| `--markets <ids>` | Comma-separated market ids that `initiate-return` auto-closes, max 16, no duplicates |
| `--pubkey <hex>` | 40-byte L2 agent pubkey; default the key `keygen` saved |
| `--key-index <n>` | API key slot 2..254; default the saved key's slot, else 2 |

The CLI preflights: USDG vault, zkLighter allowlisted on the vault's TierRegistry, markets exist. `--markets` is **not** a trading whitelist: the key can trade any market, and only listed markets are auto-closed.

Declare `--max-drawdown-bps` for a leveraged venue. `settleProposal` refuses a price per share below that floor (capped at 90%), even after a shortfall waiver.

Coverage: the template is an uncertified tier-2 call, so required coverage is the full notional of both legs. An under-covered proposal deploys less rather than reverting. After execute, read the deployed amount with `status`, not the declared one.

### Step 3: Execute and register the key

```bash
sherwood proposal execute --vault <vault> --id <proposal-id>
sherwood lighter register-key <strategy>
sherwood lighter status <strategy>          # lifecycle, deployed amount, positions, margin
```

### Step 4: Guardrails (while Executed)

The proposer or the vault owner:

```bash
sherwood lighter guardrail cancel-all <strategy>
sherwood lighter guardrail close-market <strategy> <market> [--side long|short] [--price <ticks>]
sherwood lighter guardrail rotate-key <strategy> --pubkey <hex>      # or --burn
```

`close-market` works on any market, including ones outside `--markets`. `--price` is a protective bound; the default is the widest bound.

### Step 5: Exit

```bash
# Exit step 1: cancel all + both-side market close on every configured market. Queues nothing.
sherwood lighter initiate-return <strategy>

# Wait for the closes to fill, then check
sherwood lighter status <strategy>

# Exit step 2: queue the true L2 balance. Refuses with any open position.
sherwood lighter queue-withdraw <strategy> --all

# Wait for maturity (minutes to days), then verify and burn the key
sherwood lighter prepare-settle <strategy> --dry-run
sherwood lighter prepare-settle <strategy>

# Exit step 3
sherwood proposal settle --vault <vault> --id <proposal-id>
```

`initiate-return` closes at the widest price bound so it always fills; it has no slippage protection. For a large position, close it first with `guardrail close-market --price <ticks>` and use `initiate-return` as the backstop.

Anyone can call `initiate-return` once the strategy duration has elapsed. `queue-withdraw` is never permissionless.

`prepare-settle` options: `--pubkey <hex>` (rotate to this key instead of the derived burn key), `--assume-drained` (proceed when the venue's L2 state cannot be read), `--dry-run`. Only use `--assume-drained` when the user confirms the account is empty by other means.

### Step 6: Shortfall (only after a real under-fill)

```bash
sherwood lighter acknowledge-shortfall <strategy> --yes
```

Works only after `initiate-return`, after something was queued, and while a shortfall is observable now. See hard rule 10.

### Step 7: Late arrivals after settle

```bash
sherwood lighter queue-withdraw <strategy> --all   # works in the Settled state
sherwood lighter recover <strategy>                 # recoverResiduals(): claims matured USDG onto the clone
```

`recover` does not push to the vault. It prints the batch that does, `[clone.recoverResiduals(), clone.rescueTo(USDG)]`, for a later proposal's settle calls or an owner emergency batch. That batch runs after this clone's `settle()`, never before it (hard rule 8). Propose it promptly (hard rule 9). A tranche recovered inside a later proposal is booked as that proposal's profit.

## Batch calls

```
Execute: [USDG.approve(clone, depositAmount), clone.execute()]
Settle:  [clone.settle()]
Post-settle recovery (later batch): [clone.recoverResiduals(), clone.rescueTo(USDG)]
```

## Vault liquidity

While the proposal is open, instant deposits and redemptions are closed. Queued redemptions are paid only after settlement, which waits for Lighter to mature the withdrawal. Tell depositors the exit is slow by construction.

## Addresses

| Contract | Robinhood Chain (4663) |
| --- | --- |
| USDG (vault asset) | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` (`chains/4663.json`) |
| zkLighter proxy | Lighter's own contract; take it from the sherwood-strategies spec below and verify on the block explorer before use |
| LighterPerpStrategy template | Not deployed; see [ADDRESSES.md](../../../ADDRESSES.md) once it ships |

Contract spec: sherwood-strategies `docs/lighter/LighterPerpStrategy.md`. Docs: https://docs.sherwood.sh/protocol/strategies/lighter-perp
