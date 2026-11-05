# Group-chat fund: host and join modes

Your Hermes agent pitches trades, your Telegram group jams with it, members
deposit and take part, and anyone can object. Bring your own Hermes and it
joins as a co-manager. The chat is the fund.

- Chain: Robinhood Chain mainnet (4663). Fund asset: USDG (6 decimals) or WETH.
- Governance: optimistic veto. Only Against votes count. A proposal passes
  unless Against reaches 20% of share supply inside the voting window (1800 s
  on the demo vault).
- Deposits are restricted: the host approves each member (`approveDepositor`).
- No new contracts. Strategies are PortfolioStrategy clones through the
  factory; calldata comes from the CLI's `--calldata-only` or the hosted
  prepare API. The plugin never signs.

## Host mode

1. Install the plugin and set the host agent in
   `~/.hermes/plugins/sherwood-monitor/config.yaml` (or the same keys under
   Hermes' plugin settings):

   ```yaml
   fund:
     agent_name: alpha           # this Hermes inside the fund
     agent_identity_id: "101"    # its ERC-8004 id
     agent_address: "0x..."      # its own wallet
     default_fund: alpha-fund
   ```

2. Add the bot to the Telegram group.
3. Create the vault. `fund_setup(fund="alpha-fund", ...)` records the fund and,
   while no vault is set, returns the `prepare/create-fund` body
   (`openDeposits: false`). Sign it, then call `fund_setup` again with `vault`,
   `governor`, `telegram_chat_id`, `host_telegram_id`, and the per-proposal
   ceilings `max_capital` / `max_drawdown_bps`.
4. Approve members and guest agents: `/fund-approve <telegram_id|address>`
   for depositors, `/fund-approve <agent>` for agents (then
   `/fund-approve <agent> <tx_hash>` once `registerAgent` lands). Both return
   the prepare body the host wallet signs.
5. Run the loop: pitch (skill `fund-pitch`), claim and propose
   (`fund-propose`), settle and post receipts (`fund-settle`).

## Join mode (bring your own Hermes)

1. The guest's owner installs the plugin and sets `fund.agent_name`,
   `fund.agent_identity_id` (its own ERC-8004 identity) and
   `fund.agent_address` (its own scoped wallet).
2. `/join-fund <fund>` records a join request in the shared fund state.
3. The host approves: `/fund-approve <agent>`, signs `registerAgent`, then
   confirms with the tx hash. Only a registered agent can claim or propose.
4. The guest proposes with its own WOOD bond, like the host.

The host removes an agent with one action (`fund_remove_agent`): its claims
are dropped and its unproposed drafts cancelled. Proposals already on-chain
keep running; anyone can veto them. The host agent cannot be removed.

## Shared state

Telegram bots never see each other's messages, so agents coordinate only
through fund state. One JSON file per fund:

    ~/.hermes/plugins/sherwood-monitor/funds/<fund>.json

(`fund_state_dir` or `$SHERWOOD_FUND_STATE_DIR` override the directory.) It
holds the fund, members, agents, live claims, proposals and settle receipts,
plus `schema_version` (readers refuse a newer schema) and `revision` (bumped on
every write). Every write takes an exclusive file lock, reads, applies the
change, and atomically replaces the file, so host and guest processes on the
same machine never lose an update. The file is mode 0600.

Guests on other machines need the hosted fund-state API:
`fund_state_backend: http` with `fund_state_url`. That backend is a stub today
(`HttpFundStateBackend`); its docstring is the contract the API lane fills.

## Coordination rules

Enforced in `sherwood_monitor/fund/rules.py` (pure functions), checked inside
every state update, and checked again by the `pre_tool_call` guard before
`fund_draft_proposal` builds anything:

- One open proposal per agent. At most two open per fund. Drafts count as
  open; `vetoed`, `settled` and `cancelled` do not.
- Claim before proposing: `fund_claim_topic` ("drafting X") reserves a topic
  for 10 minutes. Another agent cannot claim the same topic, an agent holds
  one claim at a time, and other agents' live claims count toward the fund's
  two slots. Drafting consumes the claim.
- Any registered agent may review another agent's proposal, never its own.
- Each agent bonds its own WOOD: bond = 1% of coverage (max capital); approve
  quote + 5%, never unlimited; if WOOD < quote x 1.05, swap the gap
  USDG -> WOOD first; re-quote when the price drifts more than 5%, never widen.
- Max capital and max drawdown must sit inside the fund's ceilings. Leg caps
  must sum to no more than max capital. Unlimited approvals are blocked.
- Members back or object in chat (recorded per telegram id); the binding veto
  is the on-chain Against vote.

## Surfaces

Tools (toolset `sherwood-fund`): `fund_status`, `fund_setup`,
`fund_claim_topic`, `fund_release_claim`, `fund_draft_proposal`,
`fund_set_proposal_status`, `fund_record_vote`, `fund_record_settle`,
`fund_approve_member`, `fund_approve_agent`, `fund_remove_agent`.

Slash commands: `/fund`, `/join-fund <fund>`, `/fund-approve`,
`/fund-agents`, `/leaderboard` (not live yet). Hermes' manifest has no field
for commands, so they are listed here and in the manifest description.

Skills: `fund-pitch`, `fund-propose`, `fund-settle`.

For other lanes: `sherwood_monitor.fund.service.FundService` is the one
place every action goes through; `cards.render_fund_card` gives the status
card text and its buttons; `txbuilder.TxBuilderClient` wraps the prepare API
routes `create-fund`, `approve-depositor`, `register-agent`, `deposit`,
`propose`, `vote`, `execute`, `settle`, `redeem`, `identity-mint`,
`governor-set`, and `PreparedAction.wallet_send_calls()` turns a response
into EIP-5792 batches, one per chain.
