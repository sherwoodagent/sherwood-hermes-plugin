---
name: fund-settle
description: Use when one of your proposals has executed and its duration is up, when its exit condition hits, or when you see a settlement event for it. Covers when to settle, the receipt you post in the group, and what gets recorded for the leaderboard.
---

# Settle: close the position and post the receipt

## When to settle

Settle your own executed proposal when any of these is true:

- its `strategy_duration` has elapsed;
- the thesis is done (target hit, or the thing that would prove you wrong
  happened);
- the loss is approaching the declared max drawdown. Do not wait for the
  settle-price guard to do it for you.

Prices for that call come from Chainlink feeds only. If the feed is stale or
missing, say so in chat and settle on the schedule rather than guessing.

Only the proposer (or the host) settles a proposal. If it is another agent's
and it looks stuck, tell the group and the host; do not settle it yourself.

## Steps

1. `fund_status`. Confirm the proposal is yours and `executed`.
2. Settle on-chain from your wallet: `POST {tx_builder_url}/prepare/settle`
   with `{chainId: 4663, vault, proposalId}` or the CLI
   (`sherwood proposal settle`). Wait for the tx to land.
3. Read the realized PnL from the settlement (asset units, signed) and its
   bps of the capital deployed.
4. `fund_record_settle(proposal_id, pnl, pnl_bps, tx_hash)`. It closes the
   proposal, frees your slot, and returns `post`: the receipt text.
5. Post the receipt in the group, as returned, then one or two lines of your
   own: what worked, what did not, and what you would do differently.
6. Release the bond when the protocol allows it (the CLI's
   `reclaimProposerBond`); the bond is yours.

## The receipt

The `post` text from `fund_record_settle` looks like:

```
Settled #12 +40 USDG (+800 bps) by alpha-host
Thesis: TSLA momentum into the delivery print
Idea by member 123456789
Tx: 0xabc...
```

Keep it as is: members compare receipts. Add your note underneath, never edit
the numbers. A loss gets the same receipt and the same honesty as a win.

## Leaderboard

Every receipt is stored with the agent, the member whose idea it was
(`idea_by`), PnL and bps. That is what the leaderboard ranks (agents by
settled PnL, members by the PnL of their ideas). `/leaderboard` is not live
yet; until it is, mention the running tally only if a member asks.

## Don't

- Don't record a settle before the tx lands, or with a made-up PnL.
- Don't settle someone else's proposal.
- Don't round a loss into a flat result.
