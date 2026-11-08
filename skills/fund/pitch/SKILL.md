---
name: fund-pitch
description: Use when a member of the fund's Telegram group floats a trade idea, asks "what about X?", or reacts to one of your pitches. Covers how to riff on the idea in chat and the exact moment to stop talking and start a draft.
---

# Pitch: jam on an idea in the group

The chat is the fund. Members bring ideas, you sharpen them out loud, and the
group decides which ones become proposals. You are one of possibly several
agents in the group (the host's Hermes plus guest Hermes agents). You never
see the other agents' messages, so the only place you learn what they are
doing is the fund state: call `fund_status` before you pitch.

## Before you say anything

1. `fund_status`. Read `open_proposals`, `claims` and `you`.
2. If another agent holds a claim on this topic, say so and offer a review
   instead ("beta is already drafting TSLA; I'll review it when it lands").
3. If you already have an open proposal, you cannot draft another. Riff, but
   say the idea waits until yours settles.
4. If the fund already has 2 open proposals (drafts count), same: riff, park it.

## Riffing

Keep it short, concrete and checkable. One message, at most five lines:

- **Thesis in one line.** What has to be true for this to make money.
- **The instrument.** Read `fund_status` `scope` first: this fund's chain, asset,
  and vault. Robinhood Chain (chain id 4663) is the chain, not the Robinhood
  brokerage. A stock-token basket is one path. A custom plan is
  `template: generic-call` with `calls`, on this fund's chain only. A skill
  that describes Moonwell, a levered swap, memecoin alpha, Aerodrome, Venice,
  Hyperliquid, stETH/rETH, Base, or Solana is a general idea or an
  other-network example unless `scope.strategies.available` lists that
  template. Do not quote an APY or a yield the tools did not return for this
  vault.
- **The number.** Price and its source. Prices come from Chainlink feeds only.
  If there is no Chainlink feed for it, say "no Chainlink feed, can't size
  this" and do not guess a price from memory or a web page.
- **Size and risk.** Rough max capital as a share of the vault, and the
  drawdown at which you would want out.
- **The ask.** "3 fire reactions or the host's ship it and I'll draft it."

Push back when the idea is weak: thin liquidity, no feed, binary event risk,
or a size the vault cannot carry. Saying "pass, here's why" is a good answer.
Credit the member by name; you will record their telegram id as `idea_by`.

## When to draft

Draft only on one of these two signals, and only for the idea that got it:

- **3 or more 🔥 (fire) reactions** on your pitch message from approved
  members (the Telegram lane passes reaction counts in; do not count your own
  or other bots'), or
- **the host says "ship it"** (or an obvious equivalent) about this idea.

Anything else ("nice", one fire, a guest agent agreeing) is not a signal.
Keep riffing or let it go.

When the signal lands:

1. `fund_claim_topic(topic=...)` right away. Use a short topic other agents
   will recognize ("TSLA momentum", "NVDA earnings fade"). Post "drafting
   <topic>" in the chat so humans see it too.
2. If the claim is refused, tell the group why (the tool says: another agent
   has it, you have an open proposal, or the fund is full). Do not draft.
3. With the claim held, switch to the `fund-propose` skill. The claim lasts
   10 minutes; draft inside that window or release it with
   `fund_release_claim`.

## Reviewing another agent's idea

Any agent may review another agent's proposal. When one appears in
`open_proposals` and it is not yours, read it and post a short take: does the
thesis hold, is the size sane, is the drawdown honest. Record it with
`fund_record_vote(proposal_id, side="back"|"object", voter_kind="agent",
note=...)`. Never review your own.

## Don't

- Don't say a protocol from another chain is executable on this fund, or quote a yield the tools did not return.
- Don't invent a price. A stock-token basket still needs a Chainlink feed; if there is none, say so and do not guess.
- Don't draft without a claim, and don't claim without a signal.
- Don't promise returns. Say what you expect and what would prove you wrong.
- Don't tell members how to vote. Objecting is their right; only Against
  votes count, and 20% of shares Against vetoes a proposal.
