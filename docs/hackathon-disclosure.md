# Disclosure: Sherwood group-chat fund (sherwood-monitor v0.7.0)

This is the disclosure for the Nous Hermes hackathon entry and for the Hermes
plugin catalog (rule 13). It describes what the plugin does at v0.7.0, who
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
  members and guest Hermes agents, and each proposal gets a card in the chat
  that updates as votes and the chain change.

Sherwood vaults are on Robinhood Chain mainnet (chain 4663). The fund asset is
USDG. There is no testnet deployment.

## On chain and off chain

On chain (Robinhood Chain 4663), using contracts that already exist. The
plugin adds no contracts:

- The vault is a Sherwood vault (ERC-4626) created through the factory with
  restricted deposits (`openDeposits: false`).
- Proposals are PortfolioStrategy clones made through the strategy factory.
  Each proposal names its legs (stock tokens and weights), a maximum capital
  and a maximum drawdown, which must sit inside the fund's ceilings.
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
  through this file, not through chat.
- Signed links: the plugin signs personal links to `app.sherwood.sh` with an
  HMAC key (`SHERWOOD_TG_LINK_SECRET`). The signature proves the bot issued
  the link to that Telegram user. It does not authorize money.
- Monitoring: a `sherwood session check --stream --no-xmtp` subprocess per
  monitored fund (the CLI reads its own `~/.sherwood/config.json`), an
  optional XMTP sidecar (a local Node process, only with
  `SHERWOOD_PRIVATE_KEY`), and the crons. `hermes sherwood install-cron`
  runs `hermes cron list/create/edit` and writes four wrapper scripts to
  `$HERMES_HOME/scripts/`. The stream watchdog reads a status file the
  supervisor writes; it no longer runs `hermes sherwood status`.

## Who signs what

The plugin never signs or sends a transaction. It builds unsigned
transactions through the prepare API at `api.sherwood.sh` and posts links. A
person or the agent signs each one with their own wallet.

| Signer | Signs | Where |
|---|---|---|
| Host (vault owner) | create the fund; approve each depositor (`approveDepositor`); register each guest agent (`registerAgent`); set the agent fee; co-sign proposals while the protocol requires owner-only proposals (`proposal_signer: host`) | `app.sherwood.sh/tg/sign`, from a link issued to the host's Telegram id. The page's contract: check the link and that the connected wallet owns the vault, show every address in full, and send the prepared transactions in order without editing them. |
| Members | join (link a wallet), deposit, withdraw, Object (an Against vote) | `app.sherwood.sh/tg/join`, `/tg/deposit`, `/tg/withdraw`, `/tg/vote`, from their own wallets |
| The agent | its own WOOD bond approval and the USDG to WOOD swap that tops up the bond; settling its own proposals; proposing directly once the protocol allows non-owner proposals (`proposal_signer: agent`) | its own scoped wallet (`fund.agent_address`) |
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
  pitches and proposal legs to tokens on that list and the fund asset.
- **api.sherwood.sh:** the prepare routes (create-fund, approve-depositor(s),
  register-agent, deposit, propose, propose-portfolio, vote, execute, settle,
  redeem, set-agent-fee and others), the fund-card read
  `GET funds/<vault>/tg-card` (live cards refresh every 60 s; reads are
  throttled to at most one per 30 s per fund), and `/sources/prices`.
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
- **Child processes:** the `sherwood` CLI, node and npm get the environment
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

## Restricted deposits

Deposits are closed by default. A member taps 🎟 Join, links a wallet on
`/tg/join`, and the host approves them with a button. The plugin checks that
the tapper is the host before it looks up the member's wallet, shows the
address in full, and hands the host a `/tg/sign` link for `approveDepositor`.
Only then can the member deposit.

## What is held for human approval

- Every owner action on chain needs the host's wallet signature on
  `/tg/sign`.
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
- **Telegram limits.** 🔥 reactions reach the bot only when it is a group
  admin; ephemeral link messages are not guaranteed to reach an offline
  client (the bot falls back to a DM, then a start link). Pitches live 6
  hours in memory and are lost on restart.

## How it was built

The plugin was built with Claude: Opus 5.5 subagents wrote the code, tests
and pull requests, orchestrated by Fable 5.1. Every pull request had an
adversarial review before merge, and a person merged each one.
