# Group-chat fund: host and join modes

Your Hermes agent pitches trades, your Telegram group jams with it, members
deposit and take part, and anyone can object. Bring your own Hermes and it
joins as a co-manager. The chat is the fund.

- Chain: Robinhood Chain mainnet (4663); testing runs on mainnet with tiny
  amounts. Fund asset: USDG (6 decimals). WETH vaults can be created and take
  deposits but cannot propose (`AssetNotUsdPegged`), so the rules refuse
  claims and drafts on them.
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

Protocol facts from the 4663 fork run (2026-11-05, `scripts/fork/RESULTS-2026-11-05.md`
on `fork/she-375-portfolio-loop`), enforced or shown by the core:

- **Cooldown.** A propose within 1 h of a veto or a settle reverts
  `CooldownNotElapsed`. `fund.cooldown_until` is set on veto and settle (and
  replaced by the chain's `proposeGate.cooldownUntil` when the tg-card read
  answers); claims, drafts and `draft -> proposed` are refused before it, and
  the fund card says "Next proposal possible at HH:MM UTC".
- **Guardian review.** After voting the governor runs a 30-min review; execute
  needs a staked guardian's Approve with WOOD coverage (about 35% of
  maxCapital). Statuses `review` and `approved` sit between `voting` and
  `executed` (`voting -> review -> approved -> executed`; a guardian block
  reads as `vetoed`). The record keeps `review_ends_at` and
  `guardian_coverage` (tg-card `review.coverage.kind`); the card shows
  "Guardian review".
- **Agent fee.** The CLI guardian refuses to Approve near the 20% cap
  (`EXCESSIVE_PERFORMANCE_FEE`). `fund_setup agent_fee_bps` refuses more than
  1500 and drafts are refused on a fund recorded above it; set 500 with
  `prepare/set-agent-fee` before the first proposal.
- **Bond.** A vetoed proposal's bond comes back at once (`reclaim-bond`); a
  settled one stays in escrow for the 14-day challenge window. Receipts and
  cards say so.

## Surfaces

Tools (toolset `sherwood-fund`): `fund_status`, `fund_setup`,
`fund_claim_topic`, `fund_release_claim`, `fund_draft_proposal`,
`fund_set_proposal_status`, `fund_record_vote`, `fund_record_settle`,
`fund_approve_member`, `fund_approve_agent`, `fund_remove_agent`.

Slash commands: `/fund`, `/join-fund <fund>`, `/fund-approve`,
`/fund-agents`, `/leaderboard` (members ranked by the P&L of their settled
ideas). Hermes' manifest has no field for commands, so they are listed here
and in the manifest description. On Telegram, Hermes lists them as
`/join_fund`, `/fund_approve`, `/fund_agents` (Telegram allows no `-`); when
the chat says who typed `/join-fund` and it is not this Hermes's
`owner_telegram_id`, the command refuses and points members at the Join
button.

Skills: `fund-pitch`, `fund-propose`, `fund-settle`.

For other lanes: `sherwood_monitor.fund.service.FundService` is the one
place every action goes through; `cards.render_fund_card` gives the status
card text and its buttons (the `/tg` contract: a public View URL, personal
deposit/withdraw/join link callbacks, never a slash-command button);
`txbuilder.TxBuilderClient` wraps the prepare API (contract: sherwood
`docs/tx-builder.md`, PR #739): `create-fund`, `approve-depositor`,
`approve-depositors`, `register-agent`, `deposit`, `propose`,
`propose-portfolio` (txs bond approve -> clone -> propose, plus
`bondShortfallWood` and an empty `prelude` for the buffer swap), `vote`,
`execute`, `settle`, `redeem`, `identity-mint`, `governor-set`,
`set-agent-fee`, `owner-stake`, and the read `fund_card()` =
`GET funds/<vault>/tg-card`. Routes #739 adds 404 on production until it
deploys; the client says so and names the route. `PreparedAction.wallet_send_calls()`
turns a response into EIP-5792 batches, one per chain. Drafts whose legs carry
`symbol` + `weight_bps` (summing to 10000) map to `prepare/propose-portfolio`.

## Telegram wiring

`sherwood_monitor/fund/telegram.py` joins the fund core to the Telegram layer
(`sherwood_monitor/telegram/`, see `docs/telegram.md`). `register()` calls
`wire_fund_telegram(ctx, fund_runtime, push_context=buffer.push)`:

```
FundService ──events──▶ FundTelegramBridge ──submit()──▶ TelegramRuntime ──▶ PTB bot
     ▲                         │  (observer)                    ▲
     └──── service calls ◀─────┴── CallbackFundGateway ◀────────┘ (buttons, 🔥, /fund)
```

- **Registration is lazy.** `register_platform_handler("telegram", factory)`:
  Hermes calls the factory only when its Telegram adapter connects, which needs
  `TELEGRAM_BOT_TOKEN`. No token, no PTB, or a Hermes without platform handlers
  never fails plugin load; the factory logs and returns if PTB is missing, and
  every card call is a no-op until the adapter is up.
- **One `pre_llm_call` hook.** The plugin registers exactly one. It drains the
  event buffer (monitor warnings, settlements, draft requests, "shipped") and
  folds in the Telegram flow's pitch context (`register_telegram(...,
  register_pre_llm_hook=False)` + `bridge.pre_llm_context`).
- **Gateway (Telegram -> core).** A chat maps to a fund only through its
  `telegram_chat_id` (never a fallback fund). Records become views in display
  units: USDG 6 dp, WOOD 18 dp, string Telegram ids to ints, legs as
  `symbol weight%` or their summary line, and the card id is the governor's
  on-chain id once there is one (vote links are `<governor>:<onchain_id>`).
  Each callable is one service call:

  | Callable | Service call | Result in chat |
  |---|---|---|
  | `approve_member_fn` | `applink.ensure_member_address` (the wallet linked in /tg/join, from the app's `GET /api/tg/link`, sherwood-app #152), then `approve_member` (host tg id checked) | toast + a `/tg/sign` link for `approve-depositor`; nothing linked -> `/fund-approve <tg> <address>` |
  | `approve_agent_fn` | `approve_agent` | toast + a `/tg/sign` link for `register-agent` |
  | `remove_agent_fn` | `remove_agent` | claims dropped, drafts cancelled |
  | `toggle_backer_fn` | `record_vote(side="back" / "clear")` | social only; card edits in place |
  | `on_draft_requested_fn` | `request_draft`: claims the pitch topic for this agent with `idea_by` | agent context queued; a refusal (cooldown, fund full) is said in chat once |
  | `ship_draft_fn` | `ship_draft` (host's go-ahead) | toast; agent mode: the agent is told to propose |
  | `host_signing_url_fn` | `portfolio_payload` / `propose_payload` with the host as proposer | `/tg/sign` link (pre-GA host co-sign) |
  | `leaderboard_fn` | `leaderboard` (from settle receipts) | `/leaderboard` card |
  | `wood_usd_fn` | `/sources/prices` WOOD entry (none today -> `None`) | bond line in WOOD only |

  The draft itself comes from the agent: the pitch claim tells it the topic and
  idea; it calls `fund_draft_proposal` (that is where the pitch flow reaches
  `FundService.draft_proposal`), and the draft inherits `idea_by` and the
  pitch's message id.
- **Observer (core -> Telegram).** `FundService.subscribe` events drive the
  runtime: `drafted` -> `post_draft_card` (a reply to the pitch, bond shortfall
  shown, Co-sign button when `proposal_signer: host`); `status` to
  `proposed`/`voting` -> `post_proposal_card` once (serialized per proposal, so
  a burst of events never posts twice), later statuses, `vote` and `tally`
  -> `refresh_proposal_card`; `settled` -> refresh + `post_receipt`;
  `member_approved` -> the member #N card.
- **Chain reads.** The 60 s card refresh calls `GET funds/<vault>/tg-card`
  (throttled to one read per 30 s per fund, 10 min backoff on a 404) and
  `apply_chain_card` takes Against %, voting/review windows, guardian coverage,
  the chain state (review/approved/executed/vetoed) and `proposeGate.cooldownUntil`
  from it.
- **Signing.** Nothing in the plugin sends a transaction. Members sign in the
  browser from personal `/tg/*` links; the host signs from `/tg/sign` links
  (see "Host signing page" in `docs/telegram.md`); the agent signs with its own
  scoped wallet.

`tests/test_fund_telegram_integration.py` runs the whole loop in memory through
`register()`: join, approve, pitch + 3 🔥, agent draft, draft card, ship,
co-sign, proposed/voting (one card), back, Against via the chain read, guardian
review, settle, receipt, cooldown, leaderboard.

## Known catalog-review item

`sherwood_monitor/__init__.py` (`_read_sherwood_private_key`) reads the
Sherwood CLI's `privateKey` from `~/.sherwood/config.json` to derive the XMTP
sidecar's separate wallet (keccak of key + salt; only the derived sidecar key
is handed to the local sidecar process). That is unchanged here, but it touches a private key outside
Hermes' secret scope (catalog rule 11). The catalog lane decides whether it
stays, moves behind a secret, or goes.
