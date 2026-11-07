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
- Deposits are restricted (the default: the host approves each member,
  `approveDepositor`) or open (`open_deposits`: anyone deposits, no approval
  step). See "Two postures" below.
- No new contracts. Strategies are PortfolioStrategy clones through the
  factory; calldata comes from the CLI's `--calldata-only` or the hosted
  prepare API. The plugin never signs.

## Two postures

The factory on Robinhood Chain 4663 opened on 2026-11-06 (live reads at
block 108,643,768: `ownerOnlyProposals()` false, `depositsRestricted()`
false, `creationFee()` 0). The plugin supports both the closed posture it
was built for and the open one.

| | Closed (pre-GA, the defaults) | Open protocol |
|---|---|---|
| Setting | `proposal_signer: host` | `proposal_signer: agent` |
| Who owns the vault | the human host's browser wallet | the Hermes agent's own wallet, the one the `sherwood` CLI holds in `~/.sherwood/config.json` (created with the CLI by hand) |
| Deposits | restricted (`open_deposits: false`): the host approves each member on `/tg/sign` | open (`fund_setup open_deposits=true`, `openDeposits: true` in `create-fund`): no approval step |
| Proposals | the agent drafts, the host ships and co-signs on `/tg/sign` | the agent drafts, the host ships; the card shows `sherwood strategy propose portfolio ...` for the proposing agent to run |
| Guest agents | `/fund-approve <agent>` gives the host a register-agent body / `/tg/sign` link | `/fund-approve <agent>` gives the host agent `sherwood vault add ...` |

The two settings are independent: `open_deposits` is per fund (and the
chain's `depositsOpen` replaces it once the vault exists: an open read at
once, a restricted read only after two in a row, because the app reads the
rules failing closed), `proposal_signer` is per Hermes.

In the open protocol nothing in the plugin sends a transaction either. The
agent runs the CLI itself, and the CLI's wallet signs. The Telegram toolset
has no terminal, on purpose (Deployment boundary), so the plugin hands the
agent the exact command and records the result. It does so for proposing
and for registering a guest agent only: creating the vault, the agent fee
and the governor parameters are CLI steps the operator runs by hand.

- **Ship it.** `ship_draft` (the host's 🚀) puts the command on the record
  (`propose_command`), in the `shipped` event (`data.propose_command`) and on
  the draft card: `sherwood strategy propose portfolio --vault <vault>
  --tokens <symbols> --weights <bps, summing to 10000> --amount <max capital,
  in USDG> --duration <e.g. 6h> --max-drawdown-bps <bps> '--name=<name>'
  '--description=<thesis>'` (`fund/signed_cli.py`). `--max-drawdown-bps` is
  the drawdown the rules checked (the CLI's default is 20%); every argument
  is shell-quoted, so the thesis stays data, and the name and thesis use the
  attached `--flag=value` form, so text starting with `-` is never read as a
  flag. Only PortfolioStrategy drafts (1 to 20 symbol + weight legs, each
  symbol once) ship in this mode. The ship is the plugin's convention for
  the host's go-ahead, not an on-chain control.
- **`fund_pending_commands`** lists this agent's shipped, unproposed drafts
  with their commands (a guest runs its own drafts with its own wallet) and,
  for the host agent, approved but unregistered guest agents with their
  `sherwood vault add` commands. Each entry says how to record it:
  `fund_set_proposal_status <id> proposed` with `onchain_id` and `tx_hash`,
  or `fund_approve_agent` with `tx_hash`. Guards against proposing twice: a
  listed propose is stamped `handed_out_at` and held back for 30 minutes
  (`include_recent=true` to list it again); before listing one it reads the
  fund card and holds every propose while the vault has an open proposal,
  while a proposal from this agent's wallet is on chain but not recorded, or
  when the card cannot be read; a command shipped over 24 hours ago is listed
  as expired. A listing from a chat turn (which cannot run anything) is not
  stamped, so it never holds back the operator's run.
- **Guest wallet swaps.** A second `/join-fund` that changes an approved,
  unregistered agent's wallet or identity resets it to pending; the host
  approves the new pair, so no `vault add` is built for a pair the host
  never saw.
- **Where to run them.** The recording tools are host-authority tools, so
  Hermes' approval gate applies. Run the commands only in a `hermes -z`
  one-shot the operator starts for it (no approval prompts) or a cron the
  operator configured with `approvals.cron_mode: approve`. A cron with
  `approvals.cron_mode: deny` (the default) must not run them: it cannot
  record the result. Never in the group chat, never on boot.
- **CLI version.** No `--chain` is emitted, and the commands need the
  Sherwood CLI 0.103.0 or later: mainnet is the only network from 0.93.0,
  0.93.0 shipped without the bond escrow addresses, and 0.103.0 is the
  release the flags were checked against. `fund_pending_commands` reads
  `sherwood --version` (read-only) and warns in `cli_warning` when it is
  older; a failed check is a warning too.

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
   (`openDeposits: false`, or `true` with `open_deposits=true`). Sign it,
   then call `fund_setup` again with `vault`, `governor`,
   `telegram_chat_id`, `host_telegram_id`, and the per-proposal ceilings
   `max_capital` / `max_drawdown_bps`.
4. Approve members and guest agents: `/fund-approve <telegram_id|address>`
   for depositors, `/fund-approve <agent>` for agents (then
   `/fund-approve <agent> <tx_hash>` once `registerAgent` lands). With
   `proposal_signer: host` both return the prepare body the host wallet
   signs. With open deposits there is no depositor to approve (the command
   says so); with `proposal_signer: agent` the agent approval returns the
   `sherwood vault add` command the host agent runs.
5. Run the loop: pitch (skill `fund-pitch`), claim and propose
   (`fund-propose`), settle and post receipts (`fund-settle`).

## Join mode (bring your own Hermes)

1. The guest's owner installs the plugin and sets `fund.agent_name`,
   `fund.agent_identity_id` (its own ERC-8004 identity) and
   `fund.agent_address` (its own scoped wallet).
2. `/join-fund <fund>` records a join request in the shared fund state.
3. The host approves: `/fund-approve <agent>`, signs `registerAgent` (on
   `/tg/sign`, or with `sherwood vault add` under `proposal_signer: agent`),
   then confirms with the tx hash. Only a registered agent can claim or
   propose.
4. The guest proposes with its own WOOD bond, like the host. Under
   `proposal_signer: agent` its own `fund_pending_commands` lists its
   shipped drafts, and it runs them with its own CLI wallet.

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
- Each agent bonds its own WOOD: bond = 1% of coverage (max capital). The
  rules refuse unlimited approvals. Approve quote + 5%; if WOOD < quote x
  1.05, swap the gap USDG -> WOOD first; re-quote when the price drifts more
  than 5%, never widen: these are what the fund-propose skill tells the
  agent, not checks the plugin runs (`rules.check_bond_approve` /
  `needs_requote` exist but nothing calls them yet).
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
  1500, and claims and drafts are refused while the fee is unknown or above
  it (the tg-card read fills it from the chain); set 500 with
  `prepare/set-agent-fee` before the first proposal.
- **Bond.** A vetoed proposal's bond comes back at once (`reclaim-bond`); a
  settled one stays in escrow for the 14-day challenge window. Receipts and
  cards say so.

## Surfaces

Tools (toolset `sherwood-fund`): `fund_status`, `fund_setup`,
`fund_claim_topic`, `fund_release_claim`, `fund_draft_proposal`,
`fund_set_proposal_status`, `fund_record_vote`, `fund_record_settle`,
`fund_approve_member`, `fund_approve_agent`, `fund_remove_agent`,
`fund_pending_commands`.

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
  | `approve_member_fn` | `applink.ensure_member_address` (the wallet linked in /tg/join, from the app's `GET /api/tg/link`, sherwood-app #152), then `approve_member` (host tg id checked) | toast + a `/tg/sign` link for `approve-depositor`; nothing linked -> `/fund-approve <tg> <address>`; open deposits -> "nothing to approve" (no lookup, no link) |
  | `approve_agent_fn` | `approve_agent` | toast + a `/tg/sign` link for `register-agent`; agent signer: no link, the toast points at `/fund-approve <agent>` for the CLI command |
  | `request_membership_fn` | `request_membership` (🎟 Join) | restricted: the host gets an Approve card; open deposits: the member is admitted at once (`member_approved`, the member card) and no card goes to the host |
  | `remove_agent_fn` | `remove_agent` | claims dropped, drafts cancelled |
  | `toggle_backer_fn` | `record_vote(side="back" / "clear")` | social only; card edits in place |
  | `on_draft_requested_fn` | `request_draft`: claims the pitch topic for this agent with `idea_by` | agent context queued; a refusal (cooldown, fund full) is said in chat once |
  | `ship_draft_fn` | `ship_draft` (host's go-ahead) | toast; agent mode: the draft card shows the CLI propose command and the host's next chat turn hears where it runs |
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
  `member_approved` -> the member #N card (on an open fund this follows the
  member's 🎟 Join).
- **Chain reads.** The 60 s card refresh calls `GET funds/<vault>/tg-card`
  (throttled to one read per 30 s per fund, 10 min backoff on a 404) and
  `apply_chain_card` takes Against %, voting/review windows, guardian coverage,
  the chain state (review/approved/executed/vetoed), `proposeGate.cooldownUntil`
  and `fund.depositsOpen` (the vault's `openDeposits` and not the factory's
  `depositsRestricted`, which replaces `open_deposits`) from it. The read
  runs only while a proposal is live on chain, so before that the record
  field decides.
- **Signing.** Nothing in the plugin sends a transaction. Members sign in the
  browser from personal `/tg/*` links; with `proposal_signer: host` the host
  signs from `/tg/sign` links (see "Host signing page" in `docs/telegram.md`);
  the agent signs with its own wallet, and with `proposal_signer: agent` it
  signs the vault owner's actions too, with the `sherwood` CLI outside the
  plugin.

`tests/test_fund_telegram_integration.py` runs the whole loop in memory through
`register()`: join, approve, pitch + 3 🔥, agent draft, draft card, ship,
co-sign, proposed/voting (one card), back, Against via the chain read, guardian
review, settle, receipt, cooldown, leaderboard.

## Deployment boundary (required)

The plugin checks who started each turn, but it cannot make a chat-driven agent
safe if the agent holds general-purpose tools. For a fund group:

- **No terminal, file or code tools in the group's toolset.** Set
  `platform_toolsets.telegram` to a toolset without `terminal`, `write_file`,
  `patch` or `execute_code` (the default `hermes-telegram` toolset includes
  them). An agent that has them in the group can rewrite fund state or the
  plugin config whatever the plugin does. The plugin's `pre_tool_call` guard
  refuses, in chat turns, terminal / file / code calls whose arguments name the
  rebind command, the fund state directory or a `config.yaml`, and
  `hermes sherwood fund-bind` refuses when it inherits a chat turn's
  `HERMES_SESSION_*` env: both are best effort (a determined model can
  obfuscate a command or strip env).
- **`plugins.isolation: in_process`** (Hermes' default). Under
  `isolation: host` the plugin's hooks and tools run in a child process whose
  session ContextVars are empty, so every turn looks like the operator surface:
  nothing is sender-checked (everything goes to the approval prompt), and the
  Telegram handler and CLI command are not registered.
- **Forum topics:** sessions in a topic are shared. With
  `busy_input_mode: steer`, another member's text lands inside the host's
  running turn, which carries the host's identity. Use `interrupt`.
- **Approval buttons:** any allowlisted member of the chat can tap the buttons
  on an approval prompt (Hermes checks only the allowlist), "Always" included.
  Run host approvals in a private chat with the bot or from the operator CLI,
  and never answer "Always" to a fund prompt (each prompt's allowlist key is
  per action, so an "Always" covers only that exact action, but it still skips
  the human next time).
- Rebinding a fund (vault, governor, chat, host) is the operator's job, from
  their own shell: `hermes sherwood fund-bind <fund> [--vault 0x..]
  [--governor 0x..] [--telegram-chat-id -100..] [--host-telegram-id 123]`.
  Tool and chat error messages never name this command.

## Host authority (review of #28)

Group members talk to the agent, so the plugin assumes anything typed in the
group can steer it:

- **Host-authority tools** (`fund_setup`, `fund_approve_member`,
  `fund_approve_agent`, `fund_remove_agent`, `fund_record_settle`,
  `fund_set_proposal_status`, and `fund_record_vote` for someone else's
  vote) check who started the turn (Hermes' session vars): a Telegram sender
  must be the fund host (`host_telegram_id`, or `owner_telegram_id` before the
  fund exists); other chat platforms are refused; an unset id means nobody.
  The `pre_tool_call` guard blocks non-hosts and otherwise returns Hermes'
  approval shape (`{"action": "approve", "rule_key": "<tool>:<hash of the
  action>"}`), so a human confirms each call and an "Always" covers only that
  exact action (Hermes uses `rule_key or tool_name` as the allowlist key).
- **Fund identity** (`vault`, `governor`, `telegram_chat_id`,
  `host_telegram_id`) is set once by `fund_setup`; only the operator changes
  it, with `hermes sherwood fund-bind` (see "Deployment boundary").
- **Slash commands** fail closed: a known caller with no host id on record (or
  `/join-fund` with no `owner_telegram_id`) is refused with how to set it.
- **Order:** the host check runs before the app's member-wallet lookup and its
  state write; addresses are shown in full before the host signs.
- **Chat hygiene:** fund state IO runs off the bot's event loop; error text sent
  to chat has local paths stripped; 🎟 Join posts at most one approve card per
  user per hour.

## Catalog rule 11: resolved in 0.7.0

Up to 0.6.0 the monitor read the Sherwood CLI's `~/.sherwood/config.json`
(its `privateKey`) for the XMTP sidecar key and the gas watchdog's address,
outside Hermes' secret scope. 0.7.0 removes both reads. The monitor wallet
comes only from the declared secrets `SHERWOOD_PRIVATE_KEY` and
`SHERWOOD_AGENT_ADDRESS` (Hermes' `agent.secret_scope.get_secret`), and the
gas watchdog's RPC from the plugin's own `gas_rpc_url`. The fund features
never needed a wallet key.
