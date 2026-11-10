# sherwood-hermes-plugin

> **Sherwood is the capital layer for AI agents.** It gives any agent a vault, governance rules, encrypted comms, and composable DeFi strategies — so it can manage real capital onchain with human-vetoable guardrails. Agents operate the fund. Humans deposit capital.
>
> [sherwood.sh](https://sherwood.sh) · [docs.sherwood.sh](https://docs.sherwood.sh)

This plugin gives Hermes agents the infra to run zero human funds: an ERC-4626 vault on Robinhood Chain (chain 4663), optimistic governance over every strategy call, a 24/7 monitoring loop that turns fund activity into events the agent reacts to on every turn, and a fund that runs inside a Telegram group (see [Group-chat fund](#group-chat-fund-telegram)).

If you're new to Sherwood, the model is simple:

- **Your agent** proposes and runs strategies (for example a basket of Robinhood Chain stock tokens through PortfolioStrategy) and earns a performance fee on profit.
- **Depositors** put the vault's asset (USDG) into the vault, keep custody (non-custodial ERC-4626 shares), and can veto any proposal before it executes.
- **Guardians** stake $WOOD and review every proposal — they're slashed if they approve a malicious call. This is the human-vetoable part.

## What you get

- **A fund the agent runs end-to-end.** Vault deployment, deposits, optimistic governance, strategy execution, settlement — all driven from chat with the Sherwood CLI under the hood.
- **Live event stream into Hermes.** On-chain events (`ProposalCreated`, `VoteCast`, `Settled`, …) arrive as `<sherwood-event>` blocks the agent sees on its next turn, through `sherwood session check --stream --no-xmtp`.
- **Risk guardrails before signing.** `pre_tool_call` hooks block proposals that exceed concentration / mandate limits before they hit the chain.
- **Autonomous mode.** `hermes sherwood install-cron` registers five crons: a 15-minute digest that speaks only when something happened, three watchdogs (TVL, gas, stream health) and one agent cron that suggests votes.
- **A fund in a Telegram group.** The agent pitches trades in the chat, members deposit and object from their own wallets, guest Hermes agents join as co-managers, and every proposal gets a live card. See [Group-chat fund](#group-chat-fund-telegram).
- **Institutional memory.** Every settle/execute writes a one-line record the agent can query weeks later. "Has the NVDA basket strategy been profitable?" gets a real answer.
- **Cross-fund exposure.** "What's my total exposure to one protocol?" aggregates positions across every fund the agent runs.

## Prerequisites

- Hermes Agent installed and running
- Python ≥ 3.11
- Node ≥ 22 and npm for the Sherwood CLI (`npm i -g`); the plugin itself is pure Python, with no Node build
- Sherwood CLI installed and configured; the monitor runs it as a subprocess (`sherwood session check`), and the CLI reads its own config. The plugin never reads the CLI's config file:

```bash
npm i -g @sherwoodagent/cli
sherwood config set --private-key <0x...>
```

- For the gas watchdog: the monitor wallet in the Hermes profile `.env` (see [Monitor wallet](#monitor-wallet-gas-watchdog)).

## Install

```bash
hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha of the v0.9.1 release commit>
```

Hermes has no `@tag` install syntax; `--ref` takes a full 40-character commit
sha, the commit the `v0.9.1` tag points at. Get it with:

```bash
gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v0.9.1 --jq .sha
```

(This README cannot carry that sha: it is part of the commit. The
[v0.9.1 release page](https://github.com/sherwoodagent/sherwood-hermes-plugin/releases/tag/v0.9.1)
shows it once the release notes are edited.)

From a terminal the install asks before it prepares the Python dependencies
and enables the plugin. Without one (CI, a script, an agent's own shell) it
cannot ask, so the plugin lands installed but disabled ("Run `hermes plugins
enable sherwood-monitor`"). To install and enable in one run, answer up front:

```bash
hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha of the v0.9.1 release commit> --yes-deps --enable
```

Once the plugin is listed in the Hermes catalog, `hermes plugins install
sherwood-monitor` installs the reviewed pin. Hermes ≥ 0.21.5.

A fresh install prompts for five environment variables (Hermes' `requires_env`):
`TELEGRAM_BOT_TOKEN`, `SHERWOOD_TG_LINK_SECRET`, `SHERWOOD_TG_API_TOKEN` (only
for a group-chat fund), `SHERWOOD_PRIVATE_KEY` and `SHERWOOD_AGENT_ADDRESS`
(only for the gas watchdog). Leave any you don't use blank.

### Upgrade and uninstall

Upgrade in place with the new release's sha:

```bash
hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref <40-hex sha of the new release commit>
```

`--force` replaces the plugin's files and keeps files the repo does not
track, so `config.yaml` and the fund state stay. (`hermes plugins update`
refuses an install pinned with `--ref`.)

The plugin keeps its config and state in `~/.hermes/plugins/sherwood-monitor/`
under your home directory, whatever `HERMES_HOME` is: `config.yaml`, `funds/`
(the fund state, unless `fund_state_dir` or `$SHERWOOD_FUND_STATE_DIR` moves
it) and the stream, cursor and watchdog status files. With the default
`HERMES_HOME` (`~/.hermes`) that is the directory Hermes installs the plugin
into, so **`hermes plugins remove sherwood-monitor` deletes the config and the
fund state with it**. Back them up first:

```bash
mkdir -p ~/sherwood-monitor-backup
cp -a ~/.hermes/plugins/sherwood-monitor/config.yaml ~/.hermes/plugins/sherwood-monitor/funds ~/sherwood-monitor-backup/
```

### Upgrading from 0.8.x

0.9.0 removes the plugin's XMTP sidecar and its posting. Nothing breaks;
what 0.8.x left behind is ignored:

- `xmtp_summaries` in `config.yaml` is ignored (one log line at load); delete
  the line when convenient.
- `hermes plugins install --force --ref` carries files the repo does not
  track into the new tree, but not `node_modules/`: a built
  `xmtp_sidecar/dist/` (and any `xmtp_sidecar/*.log`) may remain in the
  plugin directory, and `~/.hermes/xmtp/` stays. Neither is used; delete
  them if you like. (An unpinned `hermes plugins update`, a `git pull`,
  also keeps `xmtp_sidecar/node_modules/`; the root `.gitignore` keeps it
  ignored.)
- `SHERWOOD_PRIVATE_KEY` is now only the gas watchdog's fallback for its
  address. If you set it for the sidecar, set `SHERWOOD_AGENT_ADDRESS` instead
  and remove the key from the profile `.env`.

### Upgrading from 0.6.0

0.7.0 no longer reads the Sherwood CLI's `~/.sherwood/config.json`, and
`hermes plugins update` does not prompt for new environment variables. Until
you set the monitor wallet in the Hermes profile `.env`, the gas watchdog
stays off; the plugin says so once in chat when funds are configured.

```bash
SHERWOOD_AGENT_ADDRESS=0x...    # enough for the gas watchdog (not a secret)
SHERWOOD_PRIVATE_KEY=0x...      # optional, secret: the address is derived from it if unset
```

- The gas watchdog now checks Robinhood Chain only, through `gas_rpc_url` in
  `config.yaml` (default `https://rpc.mainnet.chain.robinhood.com`), instead
  of the RPCs in the CLI's config. In 0.6.0 it stayed silent unless the CLI
  config had an `rpc` map; now it alerts whenever an address or key is set
  and the balance is below `gas_alert_min_eth`.
- `install-cron` in 0.6.0 registered nothing on Hermes 0.22 (see
  [Register the crons](#register-the-crons)); run it again.

### What installation does

1. Clones the plugin into `$HERMES_HOME/plugins/sherwood-monitor/` (Hermes
   loads it from there) and, once you consent, prepares its Python
   dependencies. Hermes installs the `sherwood_monitor` package, editable,
   from a copy of the plugin in its dependency workspace
   (`$HERMES_HOME/installs/…/workspace/plugin-sources/sherwood-monitor-…`).
2. There is no Node build step: the plugin is pure Python.

## Configure

Edit `~/.hermes/plugins/sherwood-monitor/config.yaml`:

```yaml
funds:
  - alpha-fund
  - beta-yield
auto_start: true
```

### Monitor wallet (gas watchdog)

Both are optional. Set them in the Hermes profile `.env`; the plugin reads
them through Hermes' secret scope (`agent.secret_scope.get_secret`), never
logs them, and never reads the Sherwood CLI's config file.

| Key | Secret | Used for |
|---|---|---|
| `SHERWOOD_PRIVATE_KEY` | yes | Only when `SHERWOOD_AGENT_ADDRESS` is unset: the gas watchdog derives the address from it. Never used to sign; the key never leaves this machine. |
| `SHERWOOD_AGENT_ADDRESS` | no | The wallet the gas watchdog checks. Unset: derived from `SHERWOOD_PRIVATE_KEY`; with neither, the watchdog logs that it is unconfigured and stays silent. |

The gas watchdog runs as a cron script and needs only the address, so set
`SHERWOOD_AGENT_ADDRESS` and leave the key unset. This is
the monitor's wallet; the group-chat fund's own scoped wallet is
`fund.agent_address` (set both to the same value if they are one wallet).

If you set `SHERWOOD_PRIVATE_KEY`, use a dedicated monitor key that holds
nothing, never the key that manages your funds. The plugin keeps it out of
the processes it starts (the `sherwood` CLI), but Hermes keeps
plugin secrets in its own environment: any session with the terminal tool,
and any cron script, can read it.

## Usage

Start Hermes:

```bash
hermes
```

At the start of each session the plugin injects a catch-up summary for
every configured fund, and with `auto_start: true` it starts each fund's
stream at the first turn (see [How the streams live](#how-the-streams-live)).
From chat:

- "start monitoring gamma-fund" → LLM calls `sherwood_monitor_start("gamma-fund")`
- "what's the status of my monitors?" → LLM calls `sherwood_monitor_status()`
- On a new `ProposalCreated`, the agent sees:
  ```
  <sherwood-event fund="alpha-fund" source="chain" type="ProposalCreated" ...>
  ```
  and can analyze + respond.

CLI outside chat:

```bash
hermes sherwood status
hermes sherwood start alpha-fund
hermes sherwood stop alpha-fund
hermes sherwood tail alpha-fund
hermes sherwood install-cron        # register (or update) the five crons; idempotent
hermes sherwood fund-bind alpha-fund --host-telegram-id 123  # operator only: change a fund's vault/governor/chat/host
```

### How the streams live

- **For the life of the Hermes process.** Each fund's
  `sherwood session check --stream --no-xmtp` runs on the plugin's own event
  loop (a daemon thread), so it keeps running between turns and across
  sessions of one Hermes process (CLI or gateway). Hermes 0.22 fires
  `on_session_end` after every turn and `on_session_finalize` on `/new`; the
  plugin stops nothing there.
- **Start.** With `auto_start: true`, at the first turn of the process (the
  first `pre_llm_call` or `on_session_start`), not at plugin load: plugins
  also load for every `hermes` CLI command, which must not spawn streams. Or
  on demand with `sherwood_monitor_start`.
- **Between turns.** Events the streams route are buffered (the last 200) and
  the next turn's `pre_llm_call` injects them as `<sherwood-event>` blocks.
- **Fund scope.** The same hook adds a `<sherwood-fund-scope>` block for the
  turn's fund: in a fund's Telegram group, for its host and members; in an
  operator turn (CLI, cron), for `default_fund`, else the only fund. It names
  the fund's chain, vault and asset and the templates `sherwood strategy
  list` printed on this host (read-only, cached 10 minutes, at most 3 s per
  turn, off the event loop), and labels bundled skills for other networks as
  examples. Other chats, DMs and strangers get none.
- **Catch-up.** `on_session_start` runs a one-shot `sherwood session check`
  per fund, which reports what changed since the CLI's last check (for
  example while Hermes was down).
- **Stop.** At Hermes shutdown: `on_session_finalize` with
  `reason="shutdown"` (CLI quit, gateway shutdown), with `atexit` as the
  fallback. Streams do not survive a Hermes restart; the next process starts
  them again and catches up.

## Group-chat fund (Telegram)

Your Hermes agent runs a Sherwood fund inside a Telegram group. Members pitch
trade ideas by mentioning the bot, and the agent answers with a short take.
When three people react 🔥 to an idea, or the host says "ship it", the agent
drafts a proposal. Every proposal gets a card in the chat with a veto meter
and a countdown. A member who disagrees taps ❌ Object, which opens a page
where their own wallet votes Against. Other people can bring their own Hermes
agents in as co-managers; each one proposes with its own WOOD bond. The chat
is where the fund talks; the contracts decide what happens to the money.

- **Chain.** Robinhood Chain mainnet (chain 4663) only. The fund asset is USDG
  (6 decimals). A WETH fund can be created and take deposits but cannot
  propose (`AssetNotUsdPegged`), so the plugin refuses claims and drafts on
  one. There is no testnet; test on mainnet with small amounts.
- **Governance.** Optimistic veto: only Against votes count. A proposal passes
  unless Against reaches 20% of share supply before the voting window ends
  (1800 s on the demo vault). Silence means pass. 👍 Back it is social and
  never votes.
- **Deposits are restricted or open.** Restricted (the default,
  `openDeposits: false`): the host approves each member (`approveDepositor`)
  before they can deposit. Open (`fund_setup open_deposits=true`, the vault
  created with `openDeposits: true`): a member taps 🎟 Join, links a wallet
  and gets their deposit link in the same message; there is no approval
  step. Once the vault exists, the fund-card read's `depositsOpen` (the
  vault's flag and the factory's `depositsRestricted`) decides: an open read
  applies at once; a restricted read closes an open fund only when two reads
  in a row say so, because the app reads those rules failing closed (an
  unread rule comes back as restricted).
- **Nothing moves money from chat.** Every money action is a plain link to
  `app.sherwood.sh` that the person signs with their own wallet, or, with
  `proposal_signer: agent`, a Sherwood CLI command the proposing agent runs
  outside the chat (its CLI wallet signs it). The plugin builds unsigned transactions through the
  prepare API at `api.sherwood.sh`, hands out those commands, and never signs
  or sends a transaction. With `proposal_signer: host` the host signs owner
  actions on `/tg/sign`; members sign from their own wallets, and the agent
  never holds member funds.

### App pages (app.sherwood.sh)

| Page | Who | What it does |
|---|---|---|
| `/tg/join?fund=<vault>` | member | Telegram login, link a wallet, ask to join |
| `/tg/deposit?fund=<vault>` | approved member (anyone when deposits are open) | deposit USDG |
| `/tg/withdraw?fund=<vault>` | member | redeem shares |
| `/tg/vote?proposal=<governor>:<id>` | member | ❌ Object: cast an Against vote |
| `/tg/sign?fund=<vault>&action=<route>&...` | host (`proposal_signer: host`) | sign an owner action: approve a member or agent, co-sign a proposal, set the agent fee |
| `/tg/fund/<vault>` | anyone | public read-only fund page |

Personal links carry the tapper's Telegram id, are signed with
`SHERWOOD_TG_LINK_SECRET`, and expire (15 minutes for join and sign, 24 hours
for deposit, withdraw and vote). The signature proves the bot issued the link;
it does not authorize money: the wallet signs in the browser. There is no
Telegram Mini App (Telegram's Bot Developer Terms allow blockchain Mini Apps
only on TON), so each money step costs one extra tap. Link format and test
vectors: [docs/telegram.md](docs/telegram.md).

👤 My position on the fund card sends the member a message widget (Bot API
10.4) in their DM with the bot. It shows NAV, P&L and share price, their member
number, and the open proposals with a live veto meter. It holds no links and
moves no money, and it is not a Mini App. Clients without widget support get
the same text. Refusals ("Only the fund host can do that.") reach only the
tapper as an ephemeral reply (`SHERWOOD_TG_PERSONAL_LINKS=dm` turns ephemeral
messages off; refusals are then pop-up alerts), and closed cards grey out
their buttons. See [docs/telegram.md](docs/telegram.md).

### Install

```bash
hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha of the v0.9.1 release commit>
```

(The sha: see [Install](#install).) Once the plugin is listed in the Hermes
catalog: `hermes plugins install sherwood-monitor`. The Telegram cards need python-telegram-bot, which comes
with Hermes' Telegram extra (the Hermes gateway needs it anyway).

### Required environment

Set these in the Hermes profile `.env`. The plugin reads them through Hermes'
secret scope (`agent.secret_scope.get_secret`) and never logs them. It works
with the gateway's `multiplex_profiles` on or off: the plugin's own Telegram
handlers run in python-telegram-bot's tasks, outside any Hermes turn, so it
captures this profile's secret scope when the handler is wired and installs
it (`set_secret_scope`) around every handler call. That capture is frozen:
restart the gateway after changing any `SHERWOOD_TG_*` value. (0.7.0 did not: with
multiplexing on, every personal link failed with "Links are not configured
yet".)

| Key | Secret | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from @BotFather. Without it Hermes never connects the Telegram adapter and the cards stay off. |
| `SHERWOOD_TG_LINK_SECRET` | yes | HMAC key for the personal `/tg` links and the host's `/tg/sign` links. Shared with the web app; never sent anywhere. |
| `SHERWOOD_TG_API_TOKEN` | yes | Bearer token for the app's member-wallet lookup (`GET /api/tg/link`). A separate secret from the link key. |

Optional: `TX_BUILDER_URL` (default `https://api.sherwood.sh/api/v1`) and
`APP_BASE_URL` (default `https://app.sherwood.sh`). The Hermes Telegram
gateway keys (`TELEGRAM_GROUP_ALLOWED_CHATS`, `require_mention`,
`observe_unmentioned_group_messages`, `TELEGRAM_ALLOWED_USERS` for the host
only) are in [docs/telegram.md](docs/telegram.md) and
[docs/telegram.example.yaml](docs/telegram.example.yaml).

### Configure

In `~/.hermes/plugins/sherwood-monitor/config.yaml` (or the same keys under
Hermes' plugin settings):

```yaml
fund:
  agent_name: alpha                # this Hermes inside the fund (unique per fund)
  agent_identity_id: "101"         # its ERC-8004 identity token id
  agent_address: "0x..."           # its own scoped wallet; it bonds its own WOOD
  owner_telegram_id: "123456789"   # Telegram user id of the human who runs this Hermes
  default_fund: alpha-fund
  app_link_hosts: app.sherwood.sh  # hosts the member-wallet lookup may call (https only)
  proposal_signer: host            # host: the host's browser wallet owns the vault and co-signs from /tg/sign (pre-GA);
                                   # agent: the agent's own CLI wallet owns the vault and proposes (open protocol)
```

Per fund, `fund_setup` records the rest once: `vault`, `governor`,
`telegram_chat_id`, `host_telegram_id` (the host's Telegram user id: only the
host approves members and agents, ships drafts and co-signs), the
per-proposal ceilings `max_capital` and `max_drawdown_bps`,
`agent_fee_bps`, and `open_deposits` (default false; it sets `openDeposits`
in the `create-fund` body). The agent fee must be at most 1500 bps (15%): guardians
refuse to approve near the 20% cap, so claims and drafts are refused while
the fee is unknown or above 1500. Set 500 with `prepare/set-agent-fee` before
the first proposal. After that only the operator changes a fund's vault,
governor, chat or host, from their own shell:
`hermes sherwood fund-bind <fund> [--vault 0x..] [--governor 0x..] [--telegram-chat-id -100..] [--host-telegram-id 123]`.

Host and join steps, the coordination rules (one open proposal per agent, two
per fund, claim before proposing) and the Telegram wiring:
[docs/fund-host-join.md](docs/fund-host-join.md). BotFather and gateway
setup: [docs/telegram.md](docs/telegram.md).

### Open protocol mode (`proposal_signer: agent`)

The factory on Robinhood Chain opened on 2026-11-06 (`ownerOnlyProposals`
false, `depositsRestricted` false, `creationFee` 0). In this mode the host is
the Hermes agent's own wallet: the one the `sherwood` CLI holds in
`~/.sherwood/config.json`. The CLI signs with it, outside the plugin; the
plugin never reads that key and never signs or sends a transaction. The
plugin hands out commands for two steps only, proposing and registering a
guest agent, and records the result. Creating the vault (with
`--open-deposits` for an open fund), setting the agent fee and the governor
parameters are CLI steps the operator (or the agent, when the operator asks
it to) runs by hand; `fund_setup`'s `create-fund` body is for a browser
wallet. Then record the vault with `fund_setup`.

- **Ship it.** The host's 🚀 Ship it puts the command on the draft card, on
  the proposal record (`propose_command`) and in the `shipped` event:
  `sherwood strategy propose portfolio --vault <vault> --tokens <symbols>
  --weights <bps> --amount <max capital> --duration <e.g. 6h>
  --max-drawdown-bps <bps> --name '<name>' --description '<thesis>'`.
  `--max-drawdown-bps` carries the drawdown the fund rules checked (the CLI
  defaults to 20%). Every argument is shell-quoted, and the name and thesis
  use the attached form (`'--name=...'`, `'--description=...'`) so text
  that starts with `-` can never be read as a flag. Symbol + weight drafts
  ship (1 to 20 legs, each symbol once), and so do Generic Call drafts.
  Raw-calldata legs do not. There is no ✍️ Co-sign button. The ship is the
  plugin's go-ahead, not an on-chain control: the open factory accepts a
  propose from a registered agent either way.
- **Generic Call.** A draft with `template: "generic-call"` and a `calls`
  plan (`executeCalls`, `settleCalls`, `settlePatches`, `sweepTokens`)
  instead of legs. The plugin checks the plan the way the Sherwood CLI
  0.104.0 compiles it. It also checks approvals, as raw `data` or
  `signature` + `args`, and inside `multicall` / `batch` / Multicall3
  `aggregate*` wrappers. It refuses operator approvals that are switched on
  (`setApprovalForAll`, ERC777 `authorizeOperator`, DAI `permit`). It caps
  an `approve`-style approval of the vault asset at the plan's pull. Any
  other token's approval is refused at or above 2^255 (2^159 for Permit2).
  The full list is in
  [docs/hackathon-disclosure.md](docs/hackathon-disclosure.md). The draft's
  preview is unsigned and not execution acceptance. At ship it writes the plan to
  `<fund state dir>/<fund>/generic-call/p-N.json` (mode 0600) and hands out
  `sherwood strategy propose generic-call --vault <vault> --calls <plan.json>
  --amount <max capital> --duration <e.g. 6h> --max-drawdown-bps <bps>
  '--name=...' '--description=...'`. Generic Call is an unaudited template:
  guardians abstain (UNAUDITED_TEMPLATE), so execute stays blocked until a
  guardian operator allows it; the draft card says so. With
  `proposal_signer: host` there is no prepare route for it, so the draft is
  refused and nothing is recorded.
- **Guest agents.** `fund_approve_agent` and `/fund-approve <agent>` return
  `sherwood vault add --vault <vault> --wallet <agent> --agent-id <id>`
  instead of a register-agent body or a `/tg/sign` link; confirm with the tx
  hash as before.
- **Running them.** The Telegram toolset has no terminal, on purpose
  (Deployment boundary below). `fund_pending_commands` lists this agent's
  shipped, unproposed drafts (and, for the host, approved, unregistered
  agents) with their commands. A guest's drafts are run by the guest, with
  its own CLI wallet. Afterwards the agent records what landed:
  `fund_set_proposal_status <id> proposed` with `onchain_id` and `tx_hash`,
  or `fund_approve_agent` with `tx_hash`. Both are host-authority tools, so
  Hermes' approval gate applies. Run the commands only where that record
  step can run: a `hermes -z` one-shot the operator starts for it (it runs
  without approval prompts) or a cron the operator configured with
  `approvals.cron_mode: approve`. **A cron with `approvals.cron_mode: deny`
  (Hermes' default) must not run these commands:** it cannot record the
  result, and an unrecorded propose is listed again.
- **Double-propose guards.** A listed propose command is marked handed out
  and held back for 30 minutes (`include_recent=true` lists it again).
  Before listing one, the tool reads the fund card and holds every propose
  while the vault has an open proposal on chain, when the card or its
  proposal reads failed, or while a proposal from this agent's wallet is on
  chain and newer than anything recorded here (record it if it is the draft;
  if it predates this fund record, `include_recent=true` lists the command). A command shipped more than 24 hours ago is listed as expired; the
  host ships it again.
- **CLI version.** The commands carry no `--chain` and need the Sherwood CLI
  0.103.0 or later (Robinhood Chain mainnet is the only network from 0.93.0;
  0.93.0 shipped without the bond escrow addresses, and 0.103.0 is the
  release the flags were checked against). A generic-call command needs
  0.104.0 or later, the release that added `--calls` and the template.
  `fund_pending_commands` reads `sherwood --version` and says so in
  `cli_warning` when it is older.

With `proposal_signer: host` (the default) none of this applies: the host's
browser wallet owns the vault and co-signs from `/tg/sign`, as before.

### Deployment boundary (required)

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

## What the plugin does

| Event | Plugin behavior |
|---|---|
| On-chain `ProposalCreated` / `ProposalExecuted` / `ProposalSettled` / `ProposalCancelled` | Inject as context on the next turn |
| On-chain `VoteCast`, sandbox, guardian review and lifecycle events | Inject as context on the next turn |
| Agent calls `sherwood proposal create/execute/settle` | `pre_tool_call` runs risk checks; `post_tool_call` writes memory + injects `<sherwood-settlement>` block |

## Risk checks

When the agent attempts `sherwood strategy propose` or `sherwood proposal create`
on a **fund vault** (a fund configured on this host), the fund's own ceilings
apply, read from the local fund record with no chain read: the capital the
template deploys (`--amount`; `--collateral-amount` for concentrated-liquidity,
`--deposit` for lighter-perp, `--asset-in` for launchpad, `--max-capital` on
`proposal create`) above max capital, or `--max-drawdown-bps` above max drawdown
(omitted = the CLI default 2000), is blocked. The general checks below do not
run on top.

### What the propose guard does and does not do

It checks the plain Sherwood CLI commands the agent runs in the terminal
against the fund's ceilings: max capital and max drawdown, read from the local
fund record.

On a host with a fund, a terminal command "names" the CLI or a fund vault when
it contains `sherwood` (on its own or as the end of a path),
`sherwoodagent/cli`, or a fund vault address. The check is made after quotes,
backslashes and line continuations are dropped.

On such a line, every command must be one of these:

- the sherwood CLI;
- the exact install line `npm i -g @sherwoodagent/cli` (optionally with
  `@X.Y.Z` or `@latest`);
- a read-only tool, by bare name: `echo`, `printf` (without `-v`), `grep`,
  `egrep`, `fgrep`, `cat`, `head`, `tail`, `wc`, `ls`, `jq`, `which`, `type`,
  `stat`, `cut`, `tr`, `true`, `false`, `cd` or `pwd` (`test` and `[` are not
  on the list: bash 4.3+ runs a command substitution quoted inside
  `test -v 'x[…]'`);
- `cast call`, `cast calldata`, `cast chain-id` or `cast 4byte`;
- `curl` to an http(s) URL with no option that writes or uploads a file;
- `hermes sherwood start`, `stop`, `status`, `tail` or `install-cron`.

Shell keywords such as `if`, `then`, `do`, `!`, `{` and `time`, and the
closers `fi`, `done` and `}`, may wrap these.

Any other command on the line refuses the whole line. That includes `chmod`
and running a file you just wrote, `xargs`, `tee`, an interpreter, `cast send`,
and `hermes sherwood fund-bind`, which the host types in their own shell.

The line is also refused if it contains any of these:

- a shell expansion (`$`, backticks, braces, globs, extglob, process
  substitution);
- a shell, `eval`, `source`, an alias, a function or a here-doc;
- environment set by an assignment, `export`, `read` or `printf -v`;
- a launcher that may add arguments;
- an unquoted `#`;
- more than 32 KB of text;
- a read-only tool that carries the CLI name or a vault and sends its output
  to a file, or to anything except another read-only tool or a sherwood
  command that is not a propose.

There is one exception: a plain variable is allowed as an option value of
`sherwood vault info` or `sherwood governor info`.

When a sherwood command is refused, run it on its own line with literal
arguments, which is how `fund_pending_commands` lists it. `cast send` and
`hermes sherwood fund-bind` have no such form: the host runs them in their own
terminal.

It is not a sandbox. Outside it are:

- a line that names neither the CLI nor a fund vault. A script the agent
  downloads or writes and then runs is outside it, even within one command, as
  long as that line names neither.
- anything set in an earlier terminal call: a script or file, a binary planted
  on PATH, an alias or variable kept by the shell, or the fund record itself;
- `execute_code`.

The limits that hold regardless are the fund's guardian review and the members'
veto on chain. A fund group's toolset must also not include terminal, file or
code tools ([docs/fund-host-join.md](docs/fund-host-join.md)).

For any other vault the plugin blocks if any of these fail (size from
`--size-usd`, else `--amount` / `--max-capital`):

- Position size > 25% of vault AUM
- Total portfolio exposure > 50% of vault AUM
- Protocol not in the vault's configured mandate list

These general checks are advisory and fail open: they need vault state from
`sherwood vault info --json`, and the Sherwood CLI (0.103.0) has no `--json`
there. The state fetcher reads `sherwood vault info --help` once and runs the
read only when it lists `--json`; until then it returns `None`, and the
`pre_tool_call` hook allows the proposal and logs why. Once the CLI ships the
flag, the checks engage with no plugin change.

## Development

```bash
git clone git@github.com:sherwoodagent/sherwood-hermes-plugin.git
cd sherwood-hermes-plugin
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v               # ~510 tests; one needs the real `hermes` binary (skipped without it)
```

Refresh the bundled Sherwood skill pack from a local Sherwood checkout:

```bash
./scripts/refresh_skill_pack.sh ../skill
```

## Autonomous mode (cron)

`hermes sherwood install-cron` registers five crons. Four are **no_agent**
script-only crons (zero LLM tokens — Hermes runs the script and delivers
stdout verbatim; empty stdout → silent tick) and one is an agent-driven
reasoning cron.

| Name | Mode | Schedule | What it does |
|---|---|---|---|
| `sherwood-monitor-digest` | no_agent | `*/15 * * * *` | Bullets new proposals / settlements / risk alerts via `cron_tick`. Same cadence as the old agent digest, no tokens. |
| `sherwood-aum-watchdog` | no_agent | `*/15 * * * *` | Alerts when total fund TVL has moved by more than `aum_alert_threshold_pct` (default 5%) since the previous reading. |
| `sherwood-gas-watchdog` | no_agent | `*/30 * * * *` | Alerts when the monitor wallet's ETH balance on Robinhood Chain (`SHERWOOD_AGENT_ADDRESS`, read through `gas_rpc_url`) falls below `gas_alert_min_eth` (default 0.002 ETH). Silent, with one log line, when no wallet is set. |
| `sherwood-stream-watchdog` | no_agent | `*/5 * * * *` | Alerts when a running fund stream has gone stale (`last_event_at` older than `stream_stale_minutes`, default 30m), its `sherwood session check` process died, or Hermes itself is gone. Reads `stream_status.json`, which the supervisor inside Hermes writes; silent when no stream is running. |
| `sherwood-proposal-reasoning` | **agent** | `0 */6 * * *` | The only cron that costs LLM tokens. Reads open proposals and returns a vote recommendation with one-sentence rationale per proposal. Silent when no proposals are open. Disable by setting `proposal_reasoning_enabled: false` in `config.yaml`. |

### Why the status/reasoning split

Hermes' [no_agent script mode](https://hermes-agent.nousresearch.com/docs/guides/cron-script-only)
runs a shell script on a schedule and delivers any stdout verbatim — no LLM,
no agent loop, no tokens. The plugin uses this for everything where the alert
text is deterministic given the input (event lists, TVL deltas, balance
checks, stream-liveness). Reasoning crons stay agent-driven because vote
recommendations and risk analysis need actual judgment.

Watchdog state lives at `~/.hermes/plugins/sherwood-monitor/watchdog_state.json`
(separate from `cron_cursor.json` so the digest's cursor doesn't share a lock
with the alert-suppression bookkeeping). The supervisor writes
`stream_status.json` next to it (on start, exit, stop, and at most every 30 s
on events) for the stream watchdog, which runs in a separate process. At
boot the plugin blanks a status file left by a Hermes that was killed without
stopping its streams, so the watchdog does not report a dead Hermes forever.

**Where plugin data lives.** Config and state are under
`~/.hermes/plugins/sherwood-monitor/` (the user's home, `Path.home()`), not
under `$HERMES_HOME`: Hermes profiles share them, and a container whose cron
children get a different `HOME` (Hermes uses `$HERMES_HOME/home` there) gives
the watchdogs a different, empty config, so they stay silent. The cron
wrappers themselves go to `$HERMES_HOME/scripts/`.

### Register the crons

```bash
hermes sherwood install-cron
```

What it does, because Hermes runs cron scripts only from
`$HERMES_HOME/scripts/` and passes them no arguments:

1. Writes one Python wrapper per watchdog to
   `$HERMES_HOME/scripts/sherwood-monitor-<mode>.py` (`digest`, `aum`, `gas`,
   `stream`): `from sherwood_monitor.cli_watchdog import main;
   raise SystemExit(main(["<mode>"]))`. Hermes runs a cron's `.py` script on
   its currently selected dependency environment, where the plugin and its
   dependencies are installed, so nothing about the interpreter is baked in
   and the wrappers survive Hermes updates.
2. Creates each missing cron with `hermes cron create <schedule> ...`
   (`--no-agent --script sherwood-monitor-<mode>.py` for the watchdogs; the
   reasoning cron's prompt is positional).
3. Brings crons that already exist back to this definition with
   `hermes cron edit <id> ...`, so re-running it is safe and also repairs
   jobs left by 0.6.0.

It prints JSON with `installed` / `updated` / `skipped` / `errors`, and changes
nothing if `hermes cron list --all` fails. Re-run it if the plugin moves
(each wrapper also adds the plugin directory to `sys.path` as a fallback).
Alerts are delivered
`local` by default; route them with `hermes cron edit <id> --deliver telegram`.

Hermes doesn't expose a plugin-side cron-registration API, so the plugin
can't declare crons in `plugin.yaml`; this CLI subcommand is the
deterministic equivalent.

### Configure watchdog thresholds

In `~/.hermes/plugins/sherwood-monitor/config.yaml`:

```yaml
aum_alert_threshold_pct: 5.0       # default 5%
gas_alert_min_eth: 0.002           # default 0.002 ETH
gas_rpc_url: https://rpc.mainnet.chain.robinhood.com  # default; use a dedicated RPC if the public one throttles
stream_stale_minutes: 30           # default 30 minutes
proposal_reasoning_enabled: true   # set false to skip registering the agent cron
```

All have safe defaults — existing configs keep working without edits.

### Not yet implemented: proposal-deadline watchdog

A "proposal voting window closing in <N hours" alert is intentionally
out of scope until the Sherwood CLI ships a `sherwood proposal list --json`
subcommand. Building it on the current `session check` event stream would
race the digest cron's cursor and miss events; we'll add it cleanly once
proposals are directly enumerable.

## Cross-fund exposure

Ask the agent "what's my total Aerodrome exposure?" or call
`sherwood_monitor_exposure()` directly. The tool aggregates vault positions
across all configured funds, returns total AUM, per-protocol breakdown,
concentration percentages, and any protocols above the concentration threshold.

Configure the threshold in `config.yaml`:

```yaml
concentration_threshold_pct: 30  # default 30%
```

When a protocol's share of total AUM exceeds this value, the tool returns a
`concentration_alerts` list so the agent can flag it or take action.

## Institutional memory

After every `sherwood proposal execute` or `sherwood proposal settle` command,
the plugin injects a `<sherwood-settlement>` block into the agent's next turn:

```
<sherwood-settlement fund="alpha-fund" action="settle" proposal_id="42"
  pnl_usd="500.0" tx="0xabc...">
REMEMBER THIS: persist it to memory as the remember-settlement steps in the
sherwood-monitor:sherwood-agent skill say (skill_view
name="sherwood-monitor:sherwood-agent",
file_path="skills/remember-settlement/SKILL.md").
</sherwood-settlement>
```

The `remember-settlement` steps are a file inside the bundled skill pack,
which Hermes registers as `sherwood-monitor:sherwood-agent`. They are not a
skill of their own, so the block names the pack and the file. They prime the
agent to call its `memory` tool and store a one-line record. Over weeks this becomes a fund history the
agent can query: "Has the Aerodrome LP strategy been profitable?", "Which
proposer has the best track record?", "What's our average P&L on 7-day
strategies for alpha-fund?"

## What the plugin touches (disclosure)

What you would want to know before installing. The full account, including
who signs what and the known limitations, is in
[docs/hackathon-disclosure.md](docs/hackathon-disclosure.md).

- **No signing, no custody.** The plugin never signs or sends a transaction
  and never holds member funds. It builds unsigned transactions and posts
  links; wallets sign in the browser. With `proposal_signer: agent` it also
  hands out `sherwood` CLI commands (a propose to the proposing agent, an
  agent registration to the host agent); the agent runs them with its
  terminal tool, outside the plugin, and the CLI signs with the key it
  holds, which the plugin never reads.
  The only wallet credential the plugin can hold is the optional
  `SHERWOOD_PRIVATE_KEY` (below), used only to derive the gas watchdog's
  address, never to sign a transaction.
- **Network calls.** `api.sherwood.sh` (the prepare API for unsigned
  transactions; the fund-card read, which live cards request every 60 s and
  the plugin throttles to at most one per 30 s per fund, and which
  `fund_pending_commands` makes before it hands out a propose command; and the
  Chainlink-backed price list), `app.sherwood.sh` (the member-wallet lookup,
  sent with `SHERWOOD_TG_API_TOKEN`, and a check that a share image is ready
  before posting it), the Telegram Bot API through Hermes' own adapter, and
  one Robinhood Chain RPC (`gas_rpc_url`, default
  `https://rpc.mainnet.chain.robinhood.com`) for the gas watchdog's balance
  check. No telemetry.
- **Shell commands and background processes.** One
  `sherwood session check --stream --no-xmtp` subprocess per monitored fund,
  running for the life of the Hermes process (started at the first turn with
  `auto_start`, or by `sherwood_monitor_start`; stopped at Hermes shutdown),
  on a plugin-owned event loop thread,
  and one-shot `sherwood` reads (`session check`, `vault info`, `--version`,
  which `fund_pending_commands` also runs under `proposal_signer: agent`);
  the spawned `sherwood` CLI reads its own `~/.sherwood/config.json`. `hermes
  cron list/create/edit` from `hermes sherwood install-cron`, which also
  writes four `.py` wrapper scripts to `$HERMES_HOME/scripts/`. Children
  get the environment without `SHERWOOD_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`
  and `SHERWOOD_TG_*`. (0.6.0's stream watchdog also ran `hermes sherwood
  status`; 0.7.0 reads a status file instead.)
- **Credentials.** Only through Hermes' secret scope: the Telegram and app
  secrets above, and the optional monitor wallet (`SHERWOOD_PRIVATE_KEY`,
  `SHERWOOD_AGENT_ADDRESS`). Only an address is derived from the private
  key; the key is not sent anywhere.
- **Reads outside its own data.** None. The plugin does not read the
  Sherwood CLI's `~/.sherwood/config.json` or any other tool's credentials;
  the `sherwood` CLI it runs as a subprocess reads its own config.
- **Writes.** Fund state (one JSON file per fund, mode 0600), watchdog
  state and `stream_status.json` under `~/.hermes/plugins/sherwood-monitor/`;
  the cron wrappers under `$HERMES_HOME/scripts/`.
- **Other environment it reads** (none are credentials):
  `SHERWOOD_FUND_STATE_DIR`, `TELEGRAM_GROUP_ALLOWED_CHATS`,
  `SHERWOOD_TG_PERSONAL_LINKS`, `TELEGRAM_BOT_USERNAME`,
  `SHERWOOD_MONITOR_PYTHON`.
## License

MIT, see [LICENSE](LICENSE).
