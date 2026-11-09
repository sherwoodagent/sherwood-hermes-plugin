# Disclosure: Sherwood group-chat fund (sherwood-monitor v0.8.4)

This is the disclosure for the Nous Hermes hackathon entry and for the Hermes
plugin catalog (rule 13). It describes what the plugin does at v0.8.4, who
signs what, what it reads, and what does not work yet.

## What it is

`sherwood-monitor` is a Hermes plugin. It has two parts:

- **Monitor.** It streams on-chain events from your Sherwood funds into the
  agent's next turn, checks proposals against risk limits before the agent
  submits them, registers five crons, and writes settlements to memory. An
  optional XMTP sidecar adds group-chat messages, but only for funds whose
  XMTP group id is in their ENS record on Base; Robinhood Chain funds have
  none.
- **Group-chat fund.** It runs a Sherwood fund inside a Telegram group. The
  agent pitches trades, members back or object to them, the host approves
  members (or deposits are open to anyone) and guest Hermes agents, and each
  proposal gets a card in the chat that updates as votes and the chain
  change. A proposal is a stock-token basket or, under
  `proposal_signer: agent`, a Generic Call plan (a fixed list of calls on
  this fund's chain).
- **Fund scope.** Each turn in a fund's group (from its host or a member)
  and each operator turn is told that fund's chain, vault and asset and the
  templates the local `sherwood strategy list` printed, so the agent does
  not present another network's protocol, or a yield, as wired up. Other
  chats, DMs and strangers are told nothing about any fund.

Sherwood vaults are on Robinhood Chain mainnet (chain 4663). The fund asset is
USDG. There is no testnet deployment.

## On chain and off chain

On chain (Robinhood Chain 4663), using contracts that already exist. The
plugin adds no contracts:

- The vault is a Sherwood vault (ERC-4626) created through the factory,
  with restricted deposits (`openDeposits: false`, the default) or open
  deposits (`openDeposits: true`). The factory on 4663 opened on 2026-11-06
  (`ownerOnlyProposals` false, `depositsRestricted` false, `creationFee` 0),
  so an open vault takes deposits from anyone.
- Proposals are PortfolioStrategy clones made through the strategy factory.
  Each proposal names its legs (stock tokens and weights), a maximum capital
  and a maximum drawdown, which must sit inside the fund's ceilings. Under
  `proposal_signer: agent` a proposal can instead be a Generic Call clone:
  an unaudited template (tier 2 at full notional, never certified) that runs
  a fixed list of calls. The plugin checks the plan's shape the way the CLI
  0.104.0 compiles it and refuses calls to the vault or the governor; the
  CLI refuses the rest it reads from the chain. The plugin also checks
  approvals, which the CLI does not. It reads every call as compiled (raw
  `data` and `signature` + `args` alike), and every call inside a
  `multicall(bytes[])`, `multicall(uint256,bytes[])`,
  `multicall(bytes32,bytes[])`, `batch(bytes[],bool)` or Multicall3
  `aggregate`, `aggregate3`, `aggregate3Value`, `tryAggregate`,
  `blockAndAggregate` or `tryBlockAndAggregate` wrapper, up to three
  wrappers deep; a wrapper it cannot decode is refused. It refuses an
  operator approval that is switched on (`setApprovalForAll(…, true)`,
  ERC777 `authorizeOperator`, DAI `permit` with `allowed` true). It bounds
  `approve`, `increaseAllowance`, `increaseApproval`, `approveAndCall`,
  EIP-2612 `permit` and Permit2 `approve`. An approval of the vault asset
  may not be above the plan's pull: the amount of the vault asset the clone
  takes from the vault at execute (`--amount`, the draft's max capital). An
  approval of any other token is refused only at or above 2^255 (2^159 for
  Permit2), because the plan does not say how much of that token the clone
  will hold. Those bounds check the amounts written in the plan. A settle
  patch, which writes a balance into a call at settle time, may write into
  a call that approves only that approval's amount word, only from the
  approved token's own balance, and never scaled up (`mulNum` above
  `mulDen`). An operator approval and any call to one of the wrappers above
  take no patch, since a patch into a wrapper could rewrite an inner call at
  settle. A patched amount is therefore at most what the clone holds of that
  token at settle. The checks fail closed: a settle-leg approval of the
  asset above the pull is refused, and so is an approval inside a Multicall3
  `aggregate`, though the aggregator, not the clone, makes it. Approvals in
  other forms, or behind other wrappers, are not recognized. Guardians abstain on it (UNAUDITED_TEMPLATE), so execute stays
  blocked (InsufficientApproveCoverage) and the vault stays locked until a
  guardian operator allows it or the proposal is cancelled. The plugin does
  not lift that gate.
- Each proposer posts a WOOD bond of 1% of the proposal's coverage. The
  plugin refuses unlimited approvals. Approving the quote plus 5% and
  re-quoting on a drift over 5% are what the fund skills tell the agent to
  do; the plugin does not check either. A vetoed proposal's bond comes back
  at once; a settled one stays in escrow for the 14-day challenge window.
- After voting, the governor runs a 30-minute guardian review. Execution needs
  a staked guardian's Approve with WOOD coverage (about 35% of max capital).
- After a veto or a settle the governor enforces a 1-hour cooldown before the
  next proposal.

Off chain:

- Chat: pitches, the agent's replies, 🔥 counts, "ship it", 👍 Back it (social
  only, never a vote), and the cards.
- Fund state: one JSON file per fund under
  `~/.hermes/plugins/sherwood-monitor/funds/` (mode 0600) holding members,
  agents, claims, drafts, proposals and settle receipts. Agents coordinate
  through this file, not through chat. When the host ships a Generic Call
  draft under `proposal_signer: agent`, its checked plan is written next to
  it, `funds/<fund>/generic-call/p-N.json` (mode 0600), for the CLI's
  `--calls`.
- Signed links: the plugin signs personal links to `app.sherwood.sh` with an
  HMAC key (`SHERWOOD_TG_LINK_SECRET`). The signature proves the bot issued
  the link to that Telegram user. It does not authorize money.
- Monitoring: a `sherwood session check --stream --no-xmtp` subprocess per
  monitored fund, running for the life of the Hermes process on a
  plugin-owned event loop and stopped at Hermes shutdown (the CLI reads its own `~/.sherwood/config.json`), an
  optional XMTP sidecar (a local Node process, only with
  `SHERWOOD_PRIVATE_KEY`), and the crons. `hermes sherwood install-cron`
  runs `hermes cron list/create/edit` and writes four `.py` wrapper scripts to
  `$HERMES_HOME/scripts/`. The stream watchdog reads a status file the
  supervisor writes; it no longer runs `hermes sherwood status`.

## Who signs what

The plugin never signs or sends a transaction. It builds unsigned
transactions through the prepare API at `api.sherwood.sh` and posts links. A
person or the agent signs each one with their own wallet.

The plugin runs in one of two postures. With `proposal_signer: host` (the
default, the closed pre-GA posture) the vault owner is the human host's
browser wallet. With `proposal_signer: agent` (the open protocol) the vault
owner is the Hermes agent's own wallet: the one the `sherwood` CLI holds in
`~/.sherwood/config.json`. The plugin never reads that key. For a proposal
or a guest-agent registration it hands the agent the exact CLI command; the
agent runs it outside the plugin, in a `hermes -z` one-shot the operator
starts or a cron the operator configured in approve mode (the Telegram
toolset has no terminal), and the CLI's wallet signs. Deposits are
restricted or open per fund, independently of the signer.

| Signer | Signs | Where |
|---|---|---|
| Host (vault owner), `proposal_signer: host` | create the fund; approve each depositor (`approveDepositor`, restricted deposits only); register each guest agent (`registerAgent`); set the agent fee; co-sign proposals while the protocol requires owner-only proposals | `app.sherwood.sh/tg/sign`, from a link issued to the host's Telegram id. The page's contract: check the link and that the connected wallet owns the vault, show every address in full, and send the prepared transactions in order without editing them. |
| Host agent (vault owner), `proposal_signer: agent` | register each guest agent (`sherwood vault add --vault --wallet --agent-id`); propose its own drafts (`sherwood strategy propose portfolio ...`, or `sherwood strategy propose generic-call --calls <plan.json> ...` for a Generic Call draft, with its own WOOD bond). Creating the fund (`--open-deposits` for an open fund), the agent fee and governor parameters are CLI steps the operator runs by hand; the plugin builds no command for them | the `sherwood` CLI with the wallet it holds, run by the agent with its terminal tool outside the group chat. The plugin builds the command (from the draft the fund rules checked, every argument shell-quoted, free text in the attached `--flag=value` form), shows it on the draft card and in `fund_pending_commands`, and records the on-chain id and tx hash afterwards. |
| Members | join (link a wallet), deposit, withdraw, Object (an Against vote) | `app.sherwood.sh/tg/join`, `/tg/deposit`, `/tg/withdraw`, `/tg/vote`, from their own wallets |
| The agent (and guest agents) | its own WOOD bond approval and the USDG to WOOD swap that tops up the bond; settling its own proposals; under `proposal_signer: agent` a guest agent proposes its own shipped drafts with its own CLI wallet | its own wallet (`fund.agent_address`) |
| The plugin | nothing on chain; HMAC signatures on links, and XMTP messages from the sidecar's own derived key when `SHERWOOD_PRIVATE_KEY` is set | |

The agent never holds member funds. Deposits go into the vault, and a
proposal's strategy spends vault funds only up to the proposal's max capital.
The fund features hold no wallet credential: there are no Privy keys and no
host, member or agent keys in the plugin's configuration. The only key the
plugin can hold is the optional monitor-wallet key `SHERWOOD_PRIVATE_KEY`,
which the XMTP sidecar uses to derive its own separate messaging key. It is
never used to sign a transaction.

Gas: the plugin pays and sponsors no gas. If the web app sponsors gas for a
member's transaction, that is the app's choice and applies on mainnet only.

The prepare API is hosted by Sherwood at `api.sherwood.sh`. It returns
unsigned calldata; it holds no keys and no funds.

## What it reads

- **Prices: Chainlink only.** The fund skills tell the agent to price from
  Chainlink feeds and nothing else, and to say "no Chainlink feed, can't size
  this" rather than guess. The card's bond line asks the prepare API's
  Chainlink-backed `/sources/prices` for WOOD/USD; it has no WOOD entry
  today, so the bond shows in WOOD only.
- **Instruments: the Robinhood Chain stock-token list.** The skills restrict
  pitches and proposal legs to tokens on that list and the fund asset. A
  Generic Call plan is not a basket and does not add a token to it.
- **api.sherwood.sh:** the prepare routes (create-fund, approve-depositor(s),
  register-agent, deposit, propose, propose-portfolio, vote, execute, settle,
  redeem, set-agent-fee and others), the fund-card read
  `GET funds/<vault>/tg-card` (live cards refresh every 60 s; reads are
  throttled to at most one per 30 s per fund; `fund_pending_commands` reads
  it once before it hands out a propose command; the Telegram `/fund`
  command reads it once when the throttle allows, for the card's NAV and
  P&L, while the Hermes in-session `/fund` and the `fund_status` tool
  render from stored state), and `/sources/prices`.
- **app.sherwood.sh:** `GET /api/tg/link` to find the wallet a member linked
  (bearer `SHERWOOD_TG_API_TOKEN`, https on allowlisted hosts only, no
  redirects), and a readiness check on share images before posting them.
- **Telegram:** messages in the allowed group, through Hermes' Telegram
  adapter and allowlist. Only senders that allowlist admits count toward 🔥
  and "ship it".
- **Credentials:** only through Hermes' secret scope
  (`agent.secret_scope.get_secret`): `TELEGRAM_BOT_TOKEN`,
  `SHERWOOD_TG_LINK_SECRET`, `SHERWOOD_TG_API_TOKEN`, and the optional
  monitor wallet `SHERWOOD_PRIVATE_KEY` / `SHERWOOD_AGENT_ADDRESS`. The
  XMTP sidecar's key is derived from the private key on this machine and
  the key is not sent anywhere.
- **Local files:** only the plugin's own config and state under
  `~/.hermes/plugins/sherwood-monitor/` and `~/.hermes/xmtp/`. It does not
  read the Sherwood CLI's `~/.sherwood/config.json` or any other tool's
  credentials.
- **RPC:** one Robinhood Chain RPC for the gas watchdog's balance check
  (`gas_rpc_url`, default `https://rpc.mainnet.chain.robinhood.com`). With
  `SHERWOOD_PRIVATE_KEY` set, the XMTP sidecar also calls the Base RPC
  `https://mainnet.base.org` (override `SIDECAR_BASE_RPC`) to read each
  fund's XMTP group id from the Durin L2Registry, and talks to the XMTP
  network.
- **Child processes:** the plugin runs the `sherwood` CLI only for reads
  (`session check`, `vault info`, `--version`, which
  `fund_pending_commands` also runs under `proposal_signer: agent`, and
  `strategy list`, which prints the templates this CLI has on its network:
  for the per-turn fund-scope block, `fund_status` and a Generic Call
  draft's preview, at most once per 10 minutes, with a 3 s timeout, off the
  event loop); it never runs a signing command. The `sherwood` CLI, node and npm get the environment
  without `SHERWOOD_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN` and `SHERWOOD_TG_*`.
  Hermes itself keeps plugin secrets in its environment, so a session with
  the terminal tool, or a cron script, can read them: use a dedicated
  monitor key if you set one.

The plugin sends no telemetry.

## Governance

Optimistic veto. Only Against votes count. A proposal passes unless Against
reaches 20% of share supply before the voting window ends. The demo vault's
window is 1800 s (30 minutes). Silence means pass. Vote weight is a snapshot
of shares at the time of the proposal. ❌ Object on a card opens `/tg/vote`,
where the member's wallet casts the Against vote; 👍 Back it is recorded in
chat only.

Coordination rules the plugin enforces before it builds anything: one open
proposal per agent, at most two per fund (drafts count), claim a topic before
drafting (a claim lasts 10 minutes), no agent reviews its own proposal, and
max capital and drawdown inside the fund's ceilings.

## Deposits

Deposits are restricted by default. A member taps 🎟 Join, links a wallet on
`/tg/join`, and the host approves them with a button. The plugin checks that
the tapper is the host before it looks up the member's wallet, shows the
address in full, and hands the host a `/tg/sign` link for `approveDepositor`.
Only then can the member deposit.

On a fund with open deposits (`fund_setup open_deposits=true`, the vault
created with `openDeposits: true`) there is no approval step. A member taps
🎟 Join and gets one message with two personal links: `/tg/join` to link a
wallet and `/tg/deposit`. No Approve card goes to the host, and
`fund_approve_member` and `/fund-approve <member>` answer that there is
nothing to approve. Once the vault exists, the fund-card read's
`depositsOpen` (the vault's flag and the factory's `depositsRestricted`)
replaces the setting, so the chain decides which posture applies. The app
reads those rules failing closed (a rule it could not read comes back as
restricted, with no warning), so an open read applies at once and a
restricted read closes an open fund only after two reads in a row; each
change is logged as a warning.

## What is held for human approval

- With `proposal_signer: host`, every owner action on chain needs the human
  host's wallet signature on `/tg/sign`.
- With `proposal_signer: agent`, no human signs anything: the agent's CLI
  wallet does. The human gate is the host's Telegram tap: 🚀 Ship it for a
  proposal, the approval of a guest agent (a human-approved tool call) for a
  registration. That gate is the plugin's convention, not an on-chain
  control: the open factory accepts a propose from any registered agent,
  and nothing stops an agent with a terminal from running the CLI itself.
  The plugin holds what it can: it builds a propose command only for a
  shipped draft, holds a listed command back for 30 minutes, holds every
  propose while the vault has an open proposal, a newer unrecorded one from
  the agent's wallet, or a failed proposal read, expires a ship after 24
  hours, resets a guest's
  approval if it changes its wallet or identity, and tells the agent to run
  the commands only in an operator-started one-shot or an approve-mode cron,
  never in the group chat and never on boot. Creating the fund, the agent
  fee and governor parameters are the operator's own CLI steps.
- The host-authority tools (`fund_setup`, `fund_approve_member`,
  `fund_approve_agent`, `fund_remove_agent`, `fund_record_settle`,
  `fund_set_proposal_status`, and `fund_record_vote` for someone else's vote)
  refuse anyone but the fund's host, and the `pre_tool_call` guard asks a
  human to approve each call. An "Always" answer covers only that exact
  action. Hosts should approve in a private chat with the bot or from the CLI
  and never answer "Always", because any allowlisted group member can tap the
  approval buttons.
- Changing a fund's vault, governor, chat or host is the operator's job, from
  their own shell (`hermes sherwood fund-bind`). It refuses inside a chat
  turn.
- The deployment boundary in the README is required: no terminal, file or
  code tools in the fund group's toolset, `plugins.isolation: in_process`,
  and `busy_input_mode: interrupt` in forum topics.
- For this entry, a Sherwood maintainer (a person) does every merge, deploy,
  mainnet spend, public post and the hackathon submission.

## Known limitations

- **No Telegram Mini App.** Telegram's Bot Developer Terms (7.1 to 7.4) allow
  blockchain Mini Apps only on TON. Money steps are plain links that open the
  browser, so each costs one extra tap.
- **Bots never see each other's messages in a group.** Guest agents
  coordinate through the shared fund state, not chat. That state is a local
  file today; the hosted fund-state API for agents on other machines is a
  stub, so a guest Hermes must run on the same machine as the host's for now.
- **WETH vaults cannot propose today.** They can be created and take
  deposits, but proposals revert (`AssetNotUsdPegged`), so the plugin refuses
  claims and drafts on them. Use USDG.
- **Smart-contract wallets (ERC-1271) cannot link** to a Telegram account on
  `/tg/join` today.
- **Feeds go stale over weekends and market holidays.** Stock-token Chainlink
  feeds stop updating when the market is closed. The skills tell the agent to
  say so and settle on schedule rather than guess a price.
- **No XMTP for Robinhood Chain funds.** The sidecar finds a fund's XMTP
  group only through the `xmtpGroupId` text record on Base's L2Registry.
  Current Sherwood CLI releases write no ENS record and Robinhood Chain has
  no registry, so those funds get on-chain events only. The group chat in
  this entry is Telegram, which does not depend on XMTP.
- **The gas watchdog needs the monitor wallet** in the Hermes profile `.env`
  (`SHERWOOD_AGENT_ADDRESS`, or `SHERWOOD_PRIVATE_KEY`, which XMTP also
  needs). Without either it is off and everything else works. Upgrading
  from 0.6.0 means setting it (0.7.0 no longer reads the Sherwood CLI's
  config) and re-running `hermes sherwood install-cron`; the plugin says so
  once in chat.
- **It depends on hosted routes.** The prepare API, the fund-card read, the
  member-wallet lookup and the `/tg` pages are served by Sherwood. When a
  route is missing, the plugin names it and the card shows less.
- **Agent-signer commands need Sherwood CLI 0.103.0 or later.** They carry
  no `--chain` (Robinhood Chain mainnet is the only network from 0.93.0),
  0.93.0 shipped without the bond escrow addresses on the propose path, and
  0.103.0 is the release the flags were checked against. A Generic Call
  command needs 0.104.0 or later (the release that added `--calls`).
  `fund_pending_commands` reads `sherwood --version` and warns when it is
  older.
- **Generic Call is agent-signer only and offline-checked.** Under
  `proposal_signer: host` there is no prepare route for it, so the draft is
  refused. The plugin's checks make no chain read: the CLI also refuses the
  factory, registries, sWOOD and the ledger as targets, a target with no
  code, and a pull above the vault's tier-2 ceiling, at propose time. A
  passing preview is not `simulate_batch` and not execution acceptance.
- **A deny-mode cron must not run agent-signer commands.** Recording the
  on-chain id after a propose (`fund_set_proposal_status`) or the tx after a
  registration (`fund_approve_agent`) is a host-authority call, so Hermes'
  approval gate applies. A `hermes -z` one-shot runs without approval
  prompts; a cron follows `approvals.cron_mode`, whose default `deny` blocks
  the recording, so an unrecorded propose would be listed again (the
  30-minute hold and the chain check limit that; they do not replace the
  record). Run the commands only in an operator-started one-shot or a cron
  set to `approve`.
- **The open-deposits read needs a chain read.** The 60 s card refresh reads
  the fund card only while a proposal is on chain; since 0.8.1 the Telegram
  `/fund` command also reads it once when the 30 s throttle allows (the
  Hermes in-session `/fund` does not read). Until a read answers, the
  `open_deposits` setting decides how the Join button behaves; after that,
  an open fund closes only after two consecutive restricted reads.
- **Telegram limits.** 🔥 reactions reach the bot only when it is a group
  admin; ephemeral link messages are not guaranteed to reach an offline
  client (the bot falls back to a DM, then a start link). Pitches live 6
  hours in memory and are lost on restart.

## How it was built

The plugin was built with Claude: Opus 5.5 subagents wrote the code, tests
and pull requests, orchestrated by Fable 5.1. Every pull request had an
adversarial review before merge, and a person merged each one.
