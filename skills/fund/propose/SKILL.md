---
name: fund-propose
description: Use after you hold a claim on a topic (fund_claim_topic) and want to turn the group's idea into an on-chain proposal. Covers the thesis, legs, max capital and drawdown, the WOOD proposer bond, and the order of calls.
---

# Propose: turn a claimed idea into a proposal

Governance is optimistic veto. Your proposal passes unless Against votes reach
20% of share supply inside the voting window (1800 s on the demo vault). So
write it for the members who might object: plain, sized, and honest about the
downside.

## Rules you cannot skip

- **Claim first.** `fund_draft_proposal` refuses without your live claim on
  the same topic. Claims last 10 minutes.
- **One open proposal per agent; at most 2 open per fund.** Drafts count.
  If you are blocked, say why in chat and stop.
- **Stay inside the fund's ceilings.** The draft is blocked before any
  calldata is built if max capital or max drawdown exceed the host's limits.
- **Your bond, your WOOD.** Every agent bonds its own WOOD from its own
  wallet. Never ask the host or another agent to bond for you.

## Write the proposal

- **Thesis**: two or three sentences. What you expect, why now, what would
  prove you wrong. Name the member whose idea it was (`idea_by` = their
  telegram id).
- **Legs**: a PortfolioStrategy basket, one leg per stock token with its
  weight in bps of max capital, weights summing to 10000, each symbol once,
  at most 20, e.g. `{"summary": "buy NVDA", "symbol": "NVDA", "weight_bps":
  6000}`, `{"summary": "buy AAPL", "symbol": "AAPL", "weight_bps": 4000}`.
  Only Robinhood Chain stock tokens. Prices from Chainlink feeds only.
- **Max capital**: the most the basket may take out of the vault, in the
  fund asset ("500" USDG).
- **Max drawdown (bps)**: the loss you are declaring up front. 1000 = 10%.
  It sizes the settle-price guard; do not inflate it to be safe.
- **Duration**: seconds the position stays open (`strategy_duration_seconds`).

Only with `proposal_signer: host`: a raw-calldata draft (legs with
`target`, `data`, `value` and a `cap` each, from the CLI's
`--calldata-only`) also works; the host co-signs it. With
`proposal_signer: agent` only symbol + weight legs ship.

## The bond

- **Bond = 1% of coverage.** Coverage is the max capital. The draft response
  shows `bond_value_asset_units`; quote it in WOOD (the CLI quotes it from the
  WOOD TWAP feed) and pass `bond_quote_wood`.
- **Approve quote + 5%, never unlimited.** The only WOOD allowance you ever
  grant for a bond is `approve_wood` from the draft response, to the
  governor's bond escrow. A `max`/unlimited approval is blocked outright.
- **Bond buffer.** Pass your `wood_balance`. If WOOD < quote x 1.05, the plan
  puts a USDG -> WOOD swap for the gap as the first leg of your wallet batch,
  before the approve.
- **Drift.** If the WOOD price moves more than 5% between quote and propose,
  re-quote and draft again. Never widen the approval to absorb drift.
- **Which price you echo (prepare/propose-portfolio, sherwood #740).** The
  response's `quoteUsdPerWood` is the ledger's WOOD price (haircut x
  min(feed, cap), about half the market); `feedUsdPerWood` is the market feed.
  Pass `quote_usd_per_wood` = `quoteUsdPerWood` exactly as returned (never the
  feed) to `fund_draft_proposal`; the host's signing page re-prepares with it
  and gets a 409 requote at a 5% move.
- **Bond-buffer status.** Pass `bond_buffer_status` (`bondBuffer.status`) and
  `bond_buffer_note` (`bufferNote`) so the draft card shows them. A 409 with
  `premiumOverCap` means the WOOD swap costs more than 3% over the feed: put
  its message in `bond_buffer_note` with status `premium-over-cap`; only the
  host may accept it (`allowBondSwapPremium`), on the signing page.

## Order of calls (`proposal_signer: agent`, the open protocol)

1. `fund_status`, then `fund_draft_proposal` with symbol + `weight_bps`
   legs. The draft card posts in the group.
2. Wait for the host's 🚀 Ship it. Never propose a draft the host has not
   shipped: shipping is the go-ahead (the plugin's rule; nothing on chain
   enforces it, so it is on you).
3. Only in a `hermes -z` one-shot the operator started for it, or a cron
   the operator set up with `approvals.cron_mode: approve`: call
   `fund_pending_commands` and run each listed `command` verbatim with the
   terminal, nothing added or changed. Your CLI wallet signs it (bond
   approve, clone, propose). Never in the group chat (no terminal there),
   never on boot, never a command you typed yourself. Respect `held_back`,
   `chain_hold`, `expired` and `cli_warning`: they mean do not run it now.
4. Record what landed at once, as the entry's `then` says:
   `fund_set_proposal_status(proposal_id, status="proposed", onchain_id,
   tx_hash)`. If it failed, say so; if it will never run, `status="cancelled"`.

## Order of calls (`proposal_signer: host`)

1. `fund_status` (fresh state).
2. `fund_draft_proposal(topic, thesis, legs, max_capital, max_drawdown_bps,
   strategy_duration_seconds, bond_quote_wood, wood_balance, idea_by,
   strategy, metadata_uri)`.
   - `ok: false, blocked: true` -> post the `error` in chat, stop.
   - `prepare.ready: false` -> get what `prepare.missing` lists (strategy
     clone, pinned metadata URI, leg calldata) before you sign. The draft
     keeps your slot meanwhile. If the thesis or legs change, cancel it and
     draft again.
3. Post the card in the group: thesis, legs, max capital, max drawdown, bond,
   voting window, and "object with an Against vote if you disagree".
4. Sign from your own wallet, in order: the swap (only if
   `bond.swap_first`), the exact approve, then propose (`prepare.body` to
   `POST {tx_builder_url}/prepare/propose`, or the CLI).
5. `fund_set_proposal_status(proposal_id, status="proposed", onchain_id,
   tx_hash)`. This starts the voting clock on the card.
6. If anything fails before step 5, `fund_set_proposal_status(...,
   status="cancelled")` so the slot frees up, and say so in chat.

## Don't

- Don't propose without a claim, or with someone else's.
- Don't run a propose command the host has not shipped, or change one before running it.
- Don't bond with anything but your own WOOD, or approve more than quote + 5%.
- Don't hide the downside to avoid objections. Objections are the product.
