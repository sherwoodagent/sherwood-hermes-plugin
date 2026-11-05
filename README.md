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
- **Live event stream into Hermes.** On-chain events (`ProposalCreated`, `VoteCast`, `Settled`, …) and XMTP messages arrive as `<sherwood-event>` blocks the agent sees on its next turn.
- **Risk guardrails before signing.** `pre_tool_call` hooks block proposals that exceed concentration / mandate limits before they hit the chain.
- **Autonomous mode.** A 15-minute cron tick checks each fund and only delivers a digest when something actually happened — no spam.
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
hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.7.0
```

Hermes ≥ 0.21.5. Once the plugin is listed in the Hermes catalog, `hermes plugins install sherwood-monitor` installs the reviewed pin.

### Upgrading from 0.6.0

0.7.0 no longer reads the Sherwood CLI's `~/.sherwood/config.json`. Set the
monitor wallet in the Hermes profile `.env` instead, or XMTP and the gas
watchdog stay off:

```bash
SHERWOOD_PRIVATE_KEY=0x...      # secret; the XMTP sidecar derives its own key from it
SHERWOOD_AGENT_ADDRESS=0x...    # the same wallet's address, for the gas watchdog
```

The gas watchdog now checks Robinhood Chain only, through `gas_rpc_url` in
`config.yaml` (default `https://rpc.mainnet.chain.robinhood.com`), instead of
the RPCs in the CLI's config. The sidecar's derived address does not change
if you set the same key the CLI uses, so existing XMTP memberships keep
working.

### What installation does

1. Installs the Python package (`sherwood_monitor`)
2. Runs `npm ci && npm run build` inside the bundled XMTP sidecar at `xmtp_sidecar/` (~30 seconds, one-time). This step pins `@xmtp/node-bindings` to a build that's compatible with glibc 2.36+, avoiding the common `GLIBC_2.38 not found` failure you'd hit with a global `@sherwoodagent/cli` install on older Debian/Ubuntu.
3. On first Hermes boot, if `SHERWOOD_PRIVATE_KEY` is set: the plugin derives a **sidecar wallet** from it (`keccak256(primaryKey + "sherwood-monitor-sidecar-v1")`), spawns the sidecar process, and verifies group membership for each configured fund. Without it the sidecar is not started (one log line) and everything else works.

### If the install fails mid-sidecar

The Python package installs fine even if the sidecar build fails; XMTP is just disabled until you rebuild. To skip the build and rebuild manually:

```bash
SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.7.0
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
`SHERWOOD_AGENT_ADDRESS` even if you leave the key unset. This is the
monitor's wallet; the group-chat fund's own scoped wallet is
`fund.agent_address` (set both to the same value if they are one wallet).

## First-time sidecar onboarding

The sidecar has its own wallet, which is a SEPARATE identity from your agent's primary wallet. This isolation is intentional — it avoids MLS conflicts with the Sherwood CLI when you run `sherwood chat ...` manually.

On first boot, the plugin will print the sidecar's derived address in a `<sherwood-monitor-warning>` block and list funds where the sidecar isn't a member yet. As the fund creator, run the suggested commands once per fund:

```bash
sherwood chat hermes-alpha add <0xSidecarAddr...>
```

Until the sidecar is a member: on-chain monitoring, risk hooks, cron digests, and exposure tracking all still work. Only the XMTP subscribe + auto-post paths are inactive.

## Usage

Start Hermes:

```bash
hermes
```

The plugin auto-starts monitors for each configured fund and injects
a catch-up summary. From chat:

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
hermes sherwood install-cron        # one-time: register the 15-min autonomous-digest cron
hermes sherwood fund-bind alpha-fund --host-telegram-id 123  # operator only: change a fund's vault/governor/chat/host
```

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
- **Deposits are restricted.** The host approves each member
  (`approveDepositor`) before they can deposit.
- **Nothing moves money from chat.** Every money action is a plain link to
  `app.sherwood.sh` that the person signs with their own wallet. The plugin
  builds unsigned transactions through the prepare API at `api.sherwood.sh`
  and never signs or sends one. The host signs owner actions on `/tg/sign`,
  members sign from their own wallets, and the agent never holds member funds.

### App pages (app.sherwood.sh)

| Page | Who | What it does |
|---|---|---|
| `/tg/join?fund=<vault>` | member | Telegram login, link a wallet, ask to join |
| `/tg/deposit?fund=<vault>` | approved member | deposit USDG |
| `/tg/withdraw?fund=<vault>` | member | redeem shares |
| `/tg/vote?proposal=<governor>:<id>` | member | ❌ Object: cast an Against vote |
| `/tg/sign?fund=<vault>&action=<route>&...` | host | sign an owner action: approve a member or agent, co-sign a proposal, set the agent fee |
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
hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.7.0
```

Once the plugin is listed in the Hermes catalog: `hermes plugins install
sherwood-monitor`. The Telegram cards need python-telegram-bot, which comes
with Hermes' Telegram extra (the Hermes gateway needs it anyway). The fund
features do not need Node or the XMTP sidecar.

### Required environment

Set these in the Hermes profile `.env`. The plugin reads them through Hermes'
secret scope (`agent.secret_scope.get_secret`) and never logs them.

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
  proposal_signer: host            # host co-signs proposals while ownerOnlyProposals is on; `agent` after GA
```

Per fund, `fund_setup` records the rest once: `vault`, `governor`,
`telegram_chat_id`, `host_telegram_id` (the host's Telegram user id: only the
host approves members and agents, ships drafts and co-signs), the
per-proposal ceilings `max_capital` and `max_drawdown_bps`, and
`agent_fee_bps`. The agent fee must be at most 1500 bps (15%): guardians
refuse to approve near the 20% cap, so claims and drafts are refused while
the fee is unknown or above 1500. Set 500 with `prepare/set-agent-fee` before
the first proposal. After that only the operator changes a fund's vault,
governor, chat or host, from their own shell:
`hermes sherwood fund-bind <fund> [--vault 0x..] [--governor 0x..] [--telegram-chat-id -100..] [--host-telegram-id 123]`.

Host and join steps, the coordination rules (one open proposal per agent, two
per fund, claim before proposing) and the Telegram wiring:
[docs/fund-host-join.md](docs/fund-host-join.md). BotFather and gateway
setup: [docs/telegram.md](docs/telegram.md).

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
| On-chain `ProposalCreated` | Inject context + auto-post markdown summary to XMTP |
| On-chain `ProposalExecuted` / `ProposalSettled` / `ProposalCancelled` | Inject + XMTP summary |
| On-chain `VoteCast`, lifecycle events | Inject only (no XMTP post) |
| XMTP `RISK_ALERT` | Inject with `priority="high"` for agent escalation |
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
pytest -v               # ~480 tests
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
| `sherwood-stream-watchdog` | no_agent | `*/5 * * * *` | Alerts when a configured fund's supervisor stream has gone stale (`last_event_at` older than `stream_stale_minutes`, default 30m) or its PID is dead. |
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
with the alert-suppression bookkeeping).

### Register the crons

```bash
hermes sherwood install-cron
```

Idempotent — outputs JSON with per-entry `installed` / `skipped` / `errors`
lists.

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

The plugin bundles a small TypeScript sidecar at `xmtp_sidecar/` that speaks JSON-RPC over stdin/stdout with the Python plugin. It owns every XMTP interaction:

- **Subscribe** — for each configured fund, `sidecar.stream_start` opens a message stream; new messages flow into the plugin's EventRouter as `<sherwood-event source="xmtp" ...>` blocks on your next turn.
- **Send** — when the plugin auto-posts a proposal-lifecycle summary, it's the sidecar that delivers the XMTP message (not the Sherwood CLI).

Why a sidecar? `@xmtp/node-sdk`'s native bindings are glibc-ABI-sensitive. When the CLI is installed globally via `npm i -g`, npm's `overrides` block is not honored — meaning the binding that ships depends on the host glibc being ≥ 2.38. The sidecar's own `package.json` IS the root of its install tree, so its `overrides` apply and it pulls a binding compatible with glibc 2.28+. Tradeoff: ~30 seconds of `npm ci && npm run build` at pip install time.

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
  links; wallets sign in the browser. The only wallet credential it can hold
  is the optional `SHERWOOD_PRIVATE_KEY` (below), used to derive the XMTP
  sidecar's key, never to sign a transaction.
- **Network calls.** `api.sherwood.sh` (the prepare API for unsigned
  transactions, the fund-card read every 60 s while a card is live, and the
  Chainlink-backed price list), `app.sherwood.sh` (the member-wallet lookup,
  sent with `SHERWOOD_TG_API_TOKEN`, and a check that a share image is ready
  before posting it), the Telegram Bot API through Hermes' own adapter, the
  XMTP network through the sidecar, and one Robinhood Chain RPC
  (`gas_rpc_url`, default `https://rpc.mainnet.chain.robinhood.com`) for the
  gas watchdog's balance check. No telemetry.
- **Shell commands and background processes.** One long-running
  `sherwood session check` subprocess per monitored fund, one-shot
  `sherwood` reads, `hermes cron` calls from `hermes sherwood install-cron`,
  and a long-running Node process for the XMTP sidecar.
- **Credentials.** Only through Hermes' secret scope: the Telegram and app
  secrets above, and the optional monitor wallet (`SHERWOOD_PRIVATE_KEY`,
  `SHERWOOD_AGENT_ADDRESS`). The XMTP sidecar's key is derived from the
  private key (`keccak256(key + "sherwood-monitor-sidecar-v1")`) and handed
  only to the local sidecar process; the key is not sent anywhere.
- **Reads outside its own data.** None. The plugin does not read the
  Sherwood CLI's `~/.sherwood/config.json` or any other tool's credentials;
  the `sherwood` CLI it runs as a subprocess reads its own config.
- **Writes.** Fund state (one JSON file per fund, mode 0600) and watchdog
  state under `~/.hermes/plugins/sherwood-monitor/`; the sidecar's XMTP
  database under `~/.hermes/xmtp/`.
- **Install-time download.** `npm ci` in `xmtp_sidecar/` fetches
  `@xmtp/node-bindings`, a native binary, from the npm registry, pinned by
  `package-lock.json` integrity hashes. `SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1`
  skips it.

## License

MIT
