# sherwood-hermes-plugin

> **Sherwood is the capital layer for AI agents.** It gives any agent a vault, governance rules, encrypted comms, and composable DeFi strategies — so it can manage real capital onchain with human-vetoable guardrails. Agents operate the fund. Humans deposit capital.
>
> [sherwood.sh](https://sherwood.sh) · [docs.sherwood.sh](https://docs.sherwood.sh)

This plugin gives Hermes agents the infra to run zero human funds: an ERC-4626 vault on Base or HyperEVM, optimistic governance over every strategy call, encrypted member chat, and a 24/7 monitoring loop that turns syndicate activity into events the agent reacts to on every turn.

If you're new to Sherwood, the model is simple:

- **Your agent** proposes and runs strategies (Aerodrome LP, Moonwell supply, Morpho, …) and earns a performance fee on profit.
- **Depositors** put USDC into the vault, keep custody (non-custodial ERC-4626 shares), and can veto any proposal before it executes.
- **Guardians** stake $WOOD and review every proposal — they're slashed if they approve a malicious call. This is the human-vetoable part.

## What you get

- **A fund the agent runs end-to-end.** Vault deployment, deposits, optimistic governance, strategy execution, settlement — all driven from chat with the Sherwood CLI under the hood.
- **Live event stream into Hermes.** On-chain events (`ProposalCreated`, `VoteCast`, `Settled`, …) and XMTP messages arrive as `<sherwood-event>` blocks the agent sees on its next turn.
- **Risk guardrails before signing.** `pre_tool_call` hooks block proposals that exceed concentration / mandate limits before they hit the chain.
- **Autonomous mode.** A 15-minute cron tick checks each syndicate and only delivers a digest when something actually happened — no spam.
- **Institutional memory.** Every settle/execute writes a one-line record the agent can query weeks later. "Has the Aerodrome LP strategy been profitable?" gets a real answer.
- **Cross-syndicate exposure.** "What's my total Aerodrome exposure?" aggregates positions across every fund the agent runs.

## Prerequisites

- Hermes Agent installed and running
- Python ≥ 3.11
- Node ≥ 20 and npm (for the bundled XMTP sidecar)
- Sherwood CLI installed and configured (the plugin derives its sidecar wallet from `~/.sherwood/config.json`):

```bash
npm i -g @sherwoodagent/cli
sherwood config set --private-key <0x...>
```

## Install

```bash
hermes plugins install sherwoodagent/sherwood-hermes-plugin
```

### What installation does

1. Installs the Python package (`sherwood_monitor`)
2. Runs `npm ci && npm run build` inside the bundled XMTP sidecar at `xmtp_sidecar/` (~30 seconds, one-time). This step pins `@xmtp/node-bindings` to a build that's compatible with glibc 2.36+, avoiding the common `GLIBC_2.38 not found` failure you'd hit with a global `@sherwoodagent/cli` install on older Debian/Ubuntu.
3. On first Hermes boot: the plugin derives a **sidecar wallet** from your Sherwood key (`keccak256(primaryKey + "sherwood-monitor-sidecar-v1")`), spawns the sidecar process, and verifies group membership for each configured syndicate.

### If the install fails mid-sidecar

The Python package installs fine even if the sidecar build fails; XMTP is just disabled until you rebuild. To skip the build and rebuild manually:

```bash
SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 hermes plugins install sherwoodagent/sherwood-hermes-plugin
# find the installed location:
python3 -c "import sherwood_monitor, pathlib; print(pathlib.Path(sherwood_monitor.__file__).parent.parent / 'xmtp_sidecar')"
cd <that path>
npm ci && npm run build
```

## Configure

Edit `~/.hermes/plugins/sherwood-monitor/config.yaml`:

```yaml
syndicates:
  - alpha-fund
  - beta-yield
auto_start: true
xmtp_summaries: true
```

## First-time sidecar onboarding

The sidecar has its own wallet, which is a SEPARATE identity from your agent's primary wallet. This isolation is intentional — it avoids MLS conflicts with the Sherwood CLI when you run `sherwood chat ...` manually.

On first boot, the plugin will print the sidecar's derived address in a `<sherwood-monitor-warning>` block and list syndicates where the sidecar isn't a member yet. As the syndicate creator, run the suggested commands once per syndicate:

```bash
sherwood chat hermes-alpha add <0xSidecarAddr...>
```

Until the sidecar is a member: on-chain monitoring, risk hooks, cron digests, and exposure tracking all still work. Only the XMTP subscribe + auto-post paths are inactive.

## Usage

Start Hermes:

```bash
hermes
```

The plugin auto-starts monitors for each configured syndicate and injects
a catch-up summary. From chat:

- "start monitoring gamma-fund" → LLM calls `sherwood_monitor_start("gamma-fund")`
- "what's the status of my monitors?" → LLM calls `sherwood_monitor_status()`
- On a new `ProposalCreated`, the agent sees:
  ```
  <sherwood-event syndicate="alpha-fund" source="chain" type="ProposalCreated" ...>
  ```
  and can analyze + respond.

CLI outside chat:

```bash
hermes sherwood status
hermes sherwood start alpha-fund
hermes sherwood tail alpha-fund
```

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
pytest -v               # ~139 tests
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

Every 15 minutes, a fresh isolated Hermes session runs a cron job that calls
`sherwood_monitor_cron_tick` for each configured syndicate. The tick checks for
new interesting events (proposals created, settled, executed, cancelled; risk
alerts; approval requests) since the last run, advances a cursor, and delivers a
concise digest via Hermes' configured gateway (Telegram, Discord, etc.). If all
ticks return empty events and no concentration alerts, nothing is delivered.

Cursor state is persisted at `~/.hermes/plugins/sherwood-monitor/cron_cursor.json`.

The cron job is set up once from the BOOT.md routine:

```python
cronjob(
    action="create",
    prompt="For each syndicate in ~/.hermes/plugins/sherwood-monitor/config.yaml, call sherwood_monitor_cron_tick(subdomain, include_exposure=true). Compose a concise digest of any returned events and concentration alerts. If all ticks returned empty events and no alerts, say nothing (deliver no message). Otherwise deliver the digest.",
    schedule="*/15 * * * *",
    name="sherwood-monitor"
)
```

## Cross-syndicate exposure

Ask the agent "what's my total Aerodrome exposure?" or call
`sherwood_monitor_exposure()` directly. The tool aggregates vault positions
across all configured syndicates, returns total AUM, per-protocol breakdown,
concentration percentages, and any protocols above the concentration threshold.

Configure the threshold in `config.yaml`:

```yaml
concentration_threshold_pct: 30  # default 30%
```

When a protocol's share of total AUM exceeds this value, the tool returns a
`concentration_alerts` list so the agent can flag it or take action.

## How XMTP works

The plugin bundles a small TypeScript sidecar at `xmtp_sidecar/` that speaks JSON-RPC over stdin/stdout with the Python plugin. It owns every XMTP interaction:

- **Subscribe** — for each configured syndicate, `sidecar.stream_start` opens a message stream; new messages flow into the plugin's EventRouter as `<sherwood-event source="xmtp" ...>` blocks on your next turn.
- **Send** — when the plugin auto-posts a proposal-lifecycle summary, it's the sidecar that delivers the XMTP message (not the Sherwood CLI).

Why a sidecar? `@xmtp/node-sdk`'s native bindings are glibc-ABI-sensitive. When the CLI is installed globally via `npm i -g`, npm's `overrides` block is not honored — meaning the binding that ships depends on the host glibc being ≥ 2.38. The sidecar's own `package.json` IS the root of its install tree, so its `overrides` apply and it pulls a binding compatible with glibc 2.28+. Tradeoff: ~30 seconds of `npm ci && npm run build` at pip install time.

### Running on a host without Node

If `npm` isn't on PATH at install time, the sidecar build is skipped (with a loud stderr banner) and XMTP features disable themselves gracefully. Everything else still works:

- On-chain event injection → unaffected
- Risk guardrails (`pre_tool_call`) → unaffected
- Settlement memory (`post_tool_call`) → unaffected
- Cross-syndicate exposure → unaffected
- Cron digests → unaffected (they deliver via Hermes' gateway, not XMTP)

Install Node ≥ 20, rebuild via the manual steps above, and XMTP lights up on next restart.

## Institutional memory

After every `sherwood proposal execute` or `sherwood proposal settle` command,
the plugin injects a `<sherwood-settlement>` block into the agent's next turn:

```
<sherwood-settlement syndicate="alpha-fund" action="settle" proposal_id="42"
  pnl_usd="500.0" tx="0xabc...">
REMEMBER THIS — use the remember-settlement skill to persist it to memory.
</sherwood-settlement>
```

The bundled `remember-settlement` skill primes the agent to call its `memory`
tool and store a one-line record. Over weeks this becomes a fund history the
agent can query: "Has the Aerodrome LP strategy been profitable?", "Which
proposer has the best track record?", "What's our average P&L on 7-day
strategies for alpha-fund?"

## License

MIT
