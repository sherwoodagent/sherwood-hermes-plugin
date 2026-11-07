# sherwood-hermes-plugin

> **Sherwood is the capital layer for AI agents.** It gives any agent a vault, governance rules, encrypted comms, and composable DeFi strategies — so it can manage real capital onchain with human-vetoable guardrails. Agents operate the fund. Humans deposit capital.
>
> [sherwood.sh](https://sherwood.sh) · [docs.sherwood.sh](https://docs.sherwood.sh)

This plugin gives Hermes agents the infra to run zero human funds: an ERC-4626 vault on Robinhood Chain (chain 4663), optimistic governance over every strategy call, encrypted member chat, a 24/7 monitoring loop that turns fund activity into events the agent reacts to on every turn, and a fund that runs inside a Telegram group (see [Group-chat fund](#group-chat-fund-telegram)).

If you're new to Sherwood, the model is simple:

- **Your agent** proposes and runs strategies (for example a basket of Robinhood Chain stock tokens through PortfolioStrategy) and earns a performance fee on profit.
- **Depositors** put the vault's asset (USDG) into the vault, keep custody (non-custodial ERC-4626 shares), and can veto any proposal before it executes.
- **Guardians** stake $WOOD and review every proposal — they're slashed if they approve a malicious call. This is the human-vetoable part.

## What you get

- **A fund the agent runs end-to-end.** Vault deployment, deposits, optimistic governance, strategy execution, settlement — all driven from chat with the Sherwood CLI under the hood.
- **Live event stream into Hermes.** On-chain events (`ProposalCreated`, `VoteCast`, `Settled`, …) arrive as `<sherwood-event>` blocks the agent sees on its next turn, through `sherwood session check --stream --no-xmtp`. XMTP messages are added only for funds whose XMTP group id is in their ENS record on Base (see [How XMTP works](#how-xmtp-works)); Robinhood Chain funds get on-chain events only.
- **Risk guardrails before signing.** `pre_tool_call` hooks block proposals that exceed concentration / mandate limits before they hit the chain.
- **Autonomous mode.** `hermes sherwood install-cron` registers five crons: a 15-minute digest that speaks only when something happened, three watchdogs (TVL, gas, stream health) and one agent cron that suggests votes.
- **A fund in a Telegram group.** The agent pitches trades in the chat, members deposit and object from their own wallets, guest Hermes agents join as co-managers, and every proposal gets a live card. See [Group-chat fund](#group-chat-fund-telegram).
- **Institutional memory.** Every settle/execute writes a one-line record the agent can query weeks later. "Has the NVDA basket strategy been profitable?" gets a real answer.
- **Cross-fund exposure.** "What's my total exposure to one protocol?" aggregates positions across every fund the agent runs.

## Prerequisites

- Hermes Agent installed and running
- Python ≥ 3.11
- Node ≥ 22 and npm (for the bundled XMTP sidecar; its pinned `@xmtp/node-bindings` needs Node 22)
- Sherwood CLI installed and configured; the monitor runs it as a subprocess (`sherwood session check`), and the CLI reads its own config. The plugin never reads the CLI's config file:

```bash
npm i -g @sherwoodagent/cli
sherwood config set --private-key <0x...>
```

- For XMTP and the gas watchdog: the monitor wallet in the Hermes profile `.env` (see [Monitor wallet](#monitor-wallet-xmtp-and-gas-watchdog)).

## Install

```bash
hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha of the v0.8.1 release commit>
```

Hermes has no `@tag` install syntax; `--ref` takes a full 40-character commit
sha. Use the commit the `v0.8.1` tag points at: it is on the
[v0.8.1 release page](https://github.com/sherwoodagent/sherwood-hermes-plugin/releases/tag/v0.8.1),
or `gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v0.8.1 --jq .sha`.
(This README cannot carry that sha: it is part of the commit.) Once the plugin
is listed in the Hermes catalog, `hermes plugins install sherwood-monitor`
installs the reviewed pin. Hermes ≥ 0.21.5.

A fresh install prompts for five environment variables (Hermes' `requires_env`):
`TELEGRAM_BOT_TOKEN`, `SHERWOOD_TG_LINK_SECRET`, `SHERWOOD_TG_API_TOKEN` (only
for a group-chat fund), `SHERWOOD_PRIVATE_KEY` and `SHERWOOD_AGENT_ADDRESS`
(only for XMTP and the gas watchdog). Leave any you don't use blank.

### Upgrading from 0.6.0

0.7.0 no longer reads the Sherwood CLI's `~/.sherwood/config.json`, and
`hermes plugins update` does not prompt for new environment variables. Until
you set the monitor wallet in the Hermes profile `.env`, the gas watchdog
(and XMTP) stay off; the plugin says so once in chat when funds are
configured.

```bash
SHERWOOD_AGENT_ADDRESS=0x...    # enough for the gas watchdog (not a secret)
SHERWOOD_PRIVATE_KEY=0x...      # optional, secret: only for XMTP; the address is derived from it if unset
```

- The gas watchdog now checks Robinhood Chain only, through `gas_rpc_url` in
  `config.yaml` (default `https://rpc.mainnet.chain.robinhood.com`), instead
  of the RPCs in the CLI's config. In 0.6.0 it stayed silent unless the CLI
  config had an `rpc` map; now it alerts whenever an address or key is set
  and the balance is below `gas_alert_min_eth`.
- XMTP only helps funds whose XMTP group id is in their ENS record on Base.
  Robinhood Chain funds have none, so for them the key buys nothing today;
  leave it unset. If you do set it, use a dedicated monitor key, not the key
  that manages your funds: the derived sidecar address then differs from
  0.6.0's, so re-add the sidecar to those Base groups.
- `install-cron` in 0.6.0 registered nothing on Hermes 0.22 (see
  [Register the crons](#register-the-crons)); run it again.

### What installation does

1. Installs the Python package (`sherwood_monitor`)
2. Runs `npm ci && npm run build` inside the bundled XMTP sidecar at `xmtp_sidecar/` (~30 seconds, one-time). This step pins `@xmtp/node-bindings` to a build that loads on glibc 2.36 (Debian 12), avoiding the common `GLIBC_2.38 not found` failure you'd hit with a global `@sherwoodagent/cli` install on older Debian/Ubuntu.
3. On first Hermes boot, if `SHERWOOD_PRIVATE_KEY` is set: the plugin derives a **sidecar wallet** from it (`keccak256(primaryKey + "sherwood-monitor-sidecar-v1")`), spawns the sidecar process, and verifies group membership for each configured fund. Without it the sidecar is not started (one log line) and everything else works.

### If the install fails mid-sidecar

The Python package installs fine even if the sidecar build fails; XMTP is just disabled until you rebuild. To skip the build and rebuild manually:

```bash
SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha of the v0.8.1 release commit>
# find the installed location:
python3 -c "import sherwood_monitor, pathlib; print(pathlib.Path(sherwood_monitor.__file__).parent.parent / 'xmtp_sidecar')"
cd <that path>
npm ci && npm run build
```

## Configure

Edit `~/.hermes/plugins/sherwood-monitor/config.yaml`:

```yaml
funds:
  - alpha-fund
  - beta-yield
auto_start: true
xmtp_summaries: true
```

### Monitor wallet (XMTP and gas watchdog)

Both are optional. Set them in the Hermes profile `.env`; the plugin reads
them through Hermes' secret scope (`agent.secret_scope.get_secret`), never
logs them, and never reads the Sherwood CLI's config file.

| Key | Secret | Used for |
|---|---|---|
| `SHERWOOD_PRIVATE_KEY` | yes | The XMTP sidecar derives its own separate key from it; the key never leaves this machine. Unset: the sidecar is not started. |
| `SHERWOOD_AGENT_ADDRESS` | no | The wallet the gas watchdog checks. Unset: derived from `SHERWOOD_PRIVATE_KEY`; with neither, the watchdog logs that it is unconfigured and stays silent. |

The gas watchdog runs as a cron script and needs only the address, so set
`SHERWOOD_AGENT_ADDRESS` and leave the key unset unless you need XMTP. This is
the monitor's wallet; the group-chat fund's own scoped wallet is
`fund.agent_address` (set both to the same value if they are one wallet).

If you set `SHERWOOD_PRIVATE_KEY`, use a dedicated monitor key that holds
nothing, never the key that manages your funds. The plugin keeps it out of
the processes it starts (the `sherwood` CLI, node, npm), but Hermes keeps
plugin secrets in its own environment: any session with the terminal tool,
and any cron script, can read it.

## First-time sidecar onboarding

The sidecar has its own wallet, which is a SEPARATE identity from your agent's primary wallet. This isolation is intentional — it avoids MLS conflicts with the Sherwood CLI when you run `sherwood chat ...` manually.

XMTP is optional, and it works only for funds whose XMTP group id is in their ENS record on Base (the sidecar reads it from Base's Durin L2Registry). Current Sherwood CLI releases write no ENS record and Robinhood Chain has no registry, so a Robinhood Chain fund never resolves: it gets on-chain events through `sherwood session check --stream --no-xmtp` and no XMTP stream, and adding the sidecar to it does not help.

With `SHERWOOD_PRIVATE_KEY` set, on first boot the plugin prints the sidecar's derived address in a `<sherwood-monitor-warning>` block and lists funds where the sidecar isn't a member (or whose group does not resolve). For a Base fund with an ENS record, run the suggested command once as the fund creator:

```bash
sherwood chat hermes-alpha add <0xSidecarAddr...>
```

Until the sidecar is a member: on-chain monitoring, risk hooks, cron digests, and exposure tracking all still work. Only the XMTP subscribe + auto-post paths are inactive.

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

### Install

```bash
hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha of the v0.8.1 release commit>
```

(The sha: see [Install](#install).) Once the plugin is listed in the Hermes
catalog: `hermes plugins install sherwood-monitor`. The Telegram cards need python-telegram-bot, which comes
with Hermes' Telegram extra (the Hermes gateway needs it anyway). The fund
features do not need Node or the XMTP sidecar.

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
  that starts with `-` can never be read as a flag. Only symbol + weight
  drafts ship (1 to 20 legs, each symbol once). There is no ✍️ Co-sign
  button. The ship is the plugin's go-ahead, not an on-chain control: the
  open factory accepts a propose from a registered agent either way.
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
  release the flags were checked against). `fund_pending_commands` reads
  `sherwood --version` and says so in `cli_warning` when it is older.

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
| On-chain `ProposalCreated` | Inject context; auto-post a markdown summary to XMTP when the fund has an XMTP stream (Base funds with an ENS record) |
| On-chain `ProposalExecuted` / `ProposalSettled` / `ProposalCancelled` | Inject; XMTP summary under the same condition |
| On-chain `VoteCast`, lifecycle events | Inject only (no XMTP post) |
| XMTP `RISK_ALERT` (XMTP funds only) | Inject with `priority="high"` for agent escalation |
| XMTP `APPROVAL_REQUEST` | Inject with `priority="human-escalate"` |
| XMTP plain `MESSAGE` | Inject only when `@`-mention present (configurable) |
| Agent calls `sherwood proposal create/execute/settle` | `pre_tool_call` runs risk checks; `post_tool_call` writes memory + injects `<sherwood-settlement>` block |

## Risk checks

When the agent attempts `sherwood strategy propose` or `sherwood proposal create`,
the plugin blocks if any of these fail:

- Position size > 25% of vault AUM
- Total portfolio exposure > 50% of vault AUM
- Protocol not in the vault's configured mandate list

Day-1 limitation: the default state fetcher calls `sherwood vault info
<sub> --json`, a subcommand that does not yet exist in the Sherwood CLI
as of this plugin version. Until that subcommand lands upstream, the
state fetcher returns `None` and the `pre_tool_call` hook fails-open
(allows the proposal) and logs a warning. This is deliberate: we ship
the enforcement scaffolding without synthesizing fake vault state. Once
the CLI subcommand exists, checks engage automatically — no plugin
change required.

## Development

```bash
git clone git@github.com:sherwoodagent/sherwood-hermes-plugin.git
cd sherwood-hermes-plugin
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" # also builds the sidecar into xmtp_sidecar/dist/
pytest -v               # ~510 tests; one needs the real `hermes` binary (skipped without it)
```

Test the sidecar separately:

```bash
cd xmtp_sidecar && npm ci && npm run typecheck && npm run build
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

## How XMTP works

The plugin bundles a small TypeScript sidecar at `xmtp_sidecar/` that speaks JSON-RPC over stdin/stdout with the Python plugin. It is optional: it starts only when `SHERWOOD_PRIVATE_KEY` is set. It finds a fund's XMTP group only through the `xmtpGroupId` text record on Base's Durin L2Registry (an `eth_call` to `https://mainnet.base.org`). Current Sherwood CLI releases write no such record and Robinhood Chain has no registry, so Robinhood Chain funds get no XMTP stream; their on-chain events still arrive through `sherwood session check --stream --no-xmtp`. For funds that do resolve, the sidecar owns every XMTP interaction:

- **Subscribe** — for each configured fund, `sidecar.stream_start` opens a message stream; new messages flow into the plugin's EventRouter as `<sherwood-event source="xmtp" ...>` blocks on your next turn.
- **Send** — when the plugin auto-posts a proposal-lifecycle summary, it's the sidecar that delivers the XMTP message (not the Sherwood CLI).

Why a sidecar? `@xmtp/node-sdk`'s native bindings are glibc-ABI-sensitive. When the CLI is installed globally via `npm i -g`, npm's `overrides` block is not honored — meaning the binding that ships depends on the host glibc being ≥ 2.38. The sidecar's own `package.json` IS the root of its install tree, so its `overrides` apply and it pulls the pinned binding, which loads on glibc 2.36 (Debian 12). Tradeoff: ~30 seconds of `npm ci && npm run build` at pip install time.

### Running on a host without Node

If `npm` isn't on PATH at install time, the sidecar build is skipped (with a loud stderr banner) and XMTP features disable themselves gracefully. Everything else still works:

- On-chain event injection → unaffected
- Risk guardrails (`pre_tool_call`) → unaffected
- Settlement memory (`post_tool_call`) → unaffected
- Cross-fund exposure → unaffected
- Cron digests → unaffected (they deliver via Hermes' gateway, not XMTP)

Install Node ≥ 22, rebuild via the manual steps above, and XMTP lights up on next restart.

## Institutional memory

After every `sherwood proposal execute` or `sherwood proposal settle` command,
the plugin injects a `<sherwood-settlement>` block into the agent's next turn:

```
<sherwood-settlement fund="alpha-fund" action="settle" proposal_id="42"
  pnl_usd="500.0" tx="0xabc...">
REMEMBER THIS — use the remember-settlement skill to persist it to memory.
</sherwood-settlement>
```

The bundled `remember-settlement` skill primes the agent to call its `memory`
tool and store a one-line record. Over weeks this becomes a fund history the
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
  `SHERWOOD_PRIVATE_KEY` (below), used to derive the XMTP sidecar's key,
  never to sign a transaction.
- **Network calls.** `api.sherwood.sh` (the prepare API for unsigned
  transactions; the fund-card read, which live cards request every 60 s and
  the plugin throttles to at most one per 30 s per fund, and which
  `fund_pending_commands` makes before it hands out a propose command; and the
  Chainlink-backed price list), `app.sherwood.sh` (the member-wallet lookup,
  sent with `SHERWOOD_TG_API_TOKEN`, and a check that a share image is ready
  before posting it), the Telegram Bot API through Hermes' own adapter, and
  one Robinhood Chain RPC (`gas_rpc_url`, default
  `https://rpc.mainnet.chain.robinhood.com`) for the gas watchdog's balance
  check. With `SHERWOOD_PRIVATE_KEY` set, the XMTP sidecar also calls the
  Base RPC `https://mainnet.base.org` (override `SIDECAR_BASE_RPC`) to read
  each fund's group id from the Durin L2Registry, and the XMTP network. No
  telemetry.
- **Shell commands and background processes.** One
  `sherwood session check --stream --no-xmtp` subprocess per monitored fund,
  running for the life of the Hermes process (started at the first turn with
  `auto_start`, or by `sherwood_monitor_start`; stopped at Hermes shutdown),
  on a plugin-owned event loop thread,
  and one-shot `sherwood` reads (`session check`, `vault info`, `--version`,
  which `fund_pending_commands` also runs under `proposal_signer: agent`);
  the spawned `sherwood` CLI reads its own `~/.sherwood/config.json`. `hermes
  cron list/create/edit` from `hermes sherwood install-cron`, which also
  writes four `.py` wrapper scripts to `$HERMES_HOME/scripts/`. A long-running Node
  process for the XMTP sidecar (only with `SHERWOOD_PRIVATE_KEY`). Children
  get the environment without `SHERWOOD_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`
  and `SHERWOOD_TG_*`. (0.6.0's stream watchdog also ran `hermes sherwood
  status`; 0.7.0 reads a status file instead.)
- **Credentials.** Only through Hermes' secret scope: the Telegram and app
  secrets above, and the optional monitor wallet (`SHERWOOD_PRIVATE_KEY`,
  `SHERWOOD_AGENT_ADDRESS`). The XMTP sidecar's key is derived from the
  private key (`keccak256(key + "sherwood-monitor-sidecar-v1")`) and handed
  only to the local sidecar process; the key is not sent anywhere.
- **Reads outside its own data.** None. The plugin does not read the
  Sherwood CLI's `~/.sherwood/config.json` or any other tool's credentials;
  the `sherwood` CLI it runs as a subprocess reads its own config.
- **Writes.** Fund state (one JSON file per fund, mode 0600), watchdog
  state and `stream_status.json` under `~/.hermes/plugins/sherwood-monitor/`;
  the cron wrappers under `$HERMES_HOME/scripts/`; the sidecar's XMTP
  database under `~/.hermes/xmtp/`.
- **Other environment it reads** (none are credentials):
  `SHERWOOD_FUND_STATE_DIR`, `TELEGRAM_GROUP_ALLOWED_CHATS`,
  `SHERWOOD_TG_PERSONAL_LINKS`, `TELEGRAM_BOT_USERNAME`,
  `SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD`, `SHERWOOD_MONITOR_PYTHON`, and in the
  sidecar `SIDECAR_BASE_RPC` / `SIDECAR_L2_REGISTRY`.
- **Install-time download.** `npm ci` in `xmtp_sidecar/` fetches
  `@xmtp/node-bindings`, a native binary, from the npm registry, pinned by
  `package-lock.json` integrity hashes. `SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1`
  skips it.

## License

MIT, see [LICENSE](LICENSE).
