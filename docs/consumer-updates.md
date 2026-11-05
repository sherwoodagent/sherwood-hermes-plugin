# Consumer-repo updates for v0.7.0

Open these PRs **after** the `v0.7.0` tag exists (the release workflow creates
it when the release PR merges; see `docs/catalog-submission.md` step 1). They
are listed here so a later lane can open them as written. Line numbers are
from each repo's `main` on 2026-11-05: `sherwoodagent/mintlify-docs`
`bbdb69c`, `sherwoodagent/skill` `081d67f`. Re-check them before editing.

What changed for consumers in 0.7.0:

- Install pin `@v0.6.0` becomes `@v0.7.0`.
- The cron set is unchanged (same five entries, same cadences), so the cron
  tables need no edit.
- `MIN_CLI_VERSION` stays `0.71.0`, so no CLI floor moves.
- New: the group-chat fund (a Sherwood fund run in a Telegram group). The old
  "predates the v1 launch" wording is now wrong: 0.7.0 targets Robinhood
  Chain (4663).
- **Breaking for XMTP and the gas watchdog:** the plugin no longer reads the
  Sherwood CLI's `~/.sherwood/config.json` (Hermes catalog rule 11). The
  monitor wallet comes from `SHERWOOD_PRIVATE_KEY` (secret, optional) and
  `SHERWOOD_AGENT_ADDRESS` (optional) in the Hermes profile `.env`; the gas
  watchdog checks Robinhood Chain through the plugin's `gas_rpc_url` config
  key (default `https://rpc.mainnet.chain.robinhood.com`). Any consumer text
  that says the plugin uses `~/.sherwood/config.json` or its `privateKey` is
  now wrong.

## 1. sherwoodagent/mintlify-docs

The Mintlify pages live in their own repo, mounted in the monorepo as the
`mintlify-docs/` submodule. Edit them there; merging to `main` publishes
docs.sherwood.sh.

### `cli/installation.mdx`

Line 49, before:

```
If you run on [Hermes Agent](https://github.com/NousResearch/hermes-agent), install the Sherwood plugin to add always-on event streaming, autonomous cron digests, and risk guardrails on top of the CLI:
```

After:

```
If you run on [Hermes Agent](https://github.com/NousResearch/hermes-agent), install the Sherwood plugin to add always-on event streaming, autonomous cron digests and risk guardrails on top of the CLI, and to run a fund inside a Telegram group:
```

Line 53, before / after:

```
hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.6.0
hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.7.0
```

Line 57 (inside `<Note>`), before:

```
Plugin v0.6.0 predates the v1 launch on Robinhood Chain. It composes on top of whichever CLI is installed, so install the CLI first.
```

After:

```
Plugin v0.7.0 runs on Robinhood Chain (4663). It composes on top of whichever CLI is installed (0.71.0 or later), so install the CLI first. It does not read the CLI's config: for XMTP and the gas watchdog, set `SHERWOOD_PRIVATE_KEY` and `SHERWOOD_AGENT_ADDRESS` in the Hermes profile `.env`. For the group-chat fund (Telegram), see the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
```

### `cli/cron-jobs.mdx`

Line 16, before:

```
- **Sherwood plugin** installed via `hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.6.0`. This plugin release predates the v1 launch and composes on top of whichever CLI is installed, so install the CLI first
```

After:

```
- **Sherwood plugin** installed via `hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.7.0`. It composes on top of whichever CLI is installed, so install the CLI first
```

After line 17 (`- **At least one fund configured** ...`), add:

```
- **Monitor wallet** for the gas watchdog: `SHERWOOD_AGENT_ADDRESS=0x...` in the Hermes profile `.env` (or `SHERWOOD_PRIVATE_KEY`, which also enables XMTP). The plugin does not read the Sherwood CLI's config
```

Line 34 (the `sherwood-gas-watchdog` row), before:

```
| `sherwood-gas-watchdog` | no_agent | every 30 min | Alerts when the agent wallet's ETH balance on Robinhood Chain falls below `gas_alert_min_eth` (default 0.002 ETH). |
```

After:

```
| `sherwood-gas-watchdog` | no_agent | every 30 min | Alerts when the monitor wallet's (`SHERWOOD_AGENT_ADDRESS`) ETH balance on Robinhood Chain falls below `gas_alert_min_eth` (default 0.002 ETH). Silent when no wallet is set. |
```

After line 51 (`gas_alert_min_eth: 0.002 ...`) in the thresholds block, add:

```
gas_rpc_url: https://rpc.mainnet.chain.robinhood.com   # default; set a dedicated RPC if the public one throttles
```

The other cron rows and "Verify" (`5 active sherwood-* entries`) stay as
they are: the cron set did not change.

## 2. sherwoodagent/skill

Per its `CLAUDE.md`, a pin change ships as a skill release: bump
`.claude-plugin/plugin.json` `version` and `SKILL.md` `metadata.version`
together (both `0.34.2` today; a patch bump to `0.34.3` fits), merge, and the
release workflow tags it. `scripts/check-skill.sh` must pass.

### `SKILL.md`

Line 27, before:

```
**Running on Hermes Agent?** After installing the CLI, also install the companion plugin — `hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.6.0` — which adds always-on event streaming, cron digests, and risk guardrails on top of the CLI. Full details in [Running on Hermes Agent](#running-on-hermes-agent) below. Skip if you're on Claude Code, Codex, or another runtime.
```

After:

```
**Running on Hermes Agent?** After installing the CLI, also install the companion plugin — `hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.7.0` — which adds always-on event streaming, cron digests and risk guardrails on top of the CLI, and can run a fund inside a Telegram group. Full details in [Running on Hermes Agent](#running-on-hermes-agent) below. Skip if you're on Claude Code, Codex, or another runtime.
```

Line 1731, replace the last sentence:

```
The current plugin release (v0.6.0) predates the v1 protocol: its monitoring and digests work, but treat its risk guardrails as advisory and rely on the CLI's own preflights.
```

with:

```
The current plugin release (v0.7.0) runs on Robinhood Chain (4663) and adds a group-chat fund: a Sherwood fund run inside a Telegram group, where members deposit and object from their own wallets and the plugin never signs. Its risk guardrails on `sherwood proposal create` are advisory (they fail open when vault state is unavailable), so rely on the CLI's own preflights.
```

Line 1746 ("How XMTP works"), replace `a separate XMTP identity from your
Sherwood agent key` with `a separate XMTP identity derived from
SHERWOOD_PRIVATE_KEY (set in the Hermes profile .env; the plugin does not read
the CLI's config)`.

Lines 1757 and 1767: `sherwood-hermes-plugin@v0.6.0` becomes
`sherwood-hermes-plugin@v0.7.0` (the plain install and the
`SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1` install).

Line 1759, before:

```
Requirements: Python ≥ 3.11, **Node ≥ 22 and npm** (for the bundled sidecar build), and a configured Sherwood CLI (`~/.sherwood/config.json` with a `privateKey`). The install runs `npm ci && npm run build` inside the sidecar directory (~30s, one-time).
```

After:

```
Requirements: Python ≥ 3.11, **Node ≥ 22 and npm** (for the bundled sidecar build), and a configured Sherwood CLI (the monitor runs it). For XMTP and the gas watchdog, set the monitor wallet in the Hermes profile `.env`: `SHERWOOD_PRIVATE_KEY` (secret; the sidecar derives its own key from it) and `SHERWOOD_AGENT_ADDRESS`. The plugin never reads the CLI's `~/.sherwood/config.json`. The install runs `npm ci && npm run build` inside the sidecar directory (~30s, one-time).
```

Line 1761, before:

```
The plugin runs a preflight on load. If it doesn't find `sherwood --version`, a configured `~/.sherwood/config.json`, or a built sidecar (`xmtp_sidecar/dist/index.js`), it injects a one-time warning with remediation steps. The plugin cannot create vaults, trade, or sign transactions on its own — it composes on top of the CLI.
```

After:

```
The plugin runs a preflight on load. If it doesn't find `sherwood --version` or a built sidecar (`xmtp_sidecar/dist/index.js`), it injects a one-time warning with remediation steps; without `SHERWOOD_PRIVATE_KEY` it skips the sidecar and logs one line. The plugin cannot create vaults, trade, or sign transactions on its own — it composes on top of the CLI.
```

Line 1773 ("One-time onboarding per vault"), before:

```
On first Hermes boot after install, the plugin derives the sidecar wallet and checks membership in each configured vault's XMTP group.
```

After:

```
On first Hermes boot after install (with `SHERWOOD_PRIVATE_KEY` set), the plugin derives the sidecar wallet and checks membership in each configured vault's XMTP group.
```

Line 1787, the config block: the plugin reads `funds:`, not `syndicates:`
(`sherwood_monitor/config.py`). Before / after:

```
syndicates:
funds:
```

After the tools table (ends at line 1801), add:

```
The group-chat fund adds the `sherwood-fund` toolset (`fund_status`, `fund_setup`, `fund_claim_topic`, `fund_release_claim`, `fund_draft_proposal`, `fund_set_proposal_status`, `fund_record_vote`, `fund_record_settle`, `fund_approve_member`, `fund_approve_agent`, `fund_remove_agent`), the slash commands `/fund`, `/join-fund`, `/fund-approve`, `/fund-agents`, `/leaderboard`, and the skills `fund-pitch`, `fund-propose`, `fund-settle`. Setup and the required deployment boundary: the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
```

Check every command and flag against the pinned CLI release, as the skill's
`CLAUDE.md` requires; these edits add none.

### `CLAUDE.md`

No edit needed. Line 50 uses `v0.5.0` only as an example of a plugin tag, and
the version-bump table names `sherwood-hermes-plugin@vX.Y.Z` generically.

## 3. sherwoodagent/sherwood (monorepo)

### `CLAUDE.md` § "Version bump checklist"

No edit needed. The "Hermes plugin bump" table (lines 95-106) names
`sherwoodagent/sherwood-hermes-plugin@vX.Y.Z` generically, and the CLI table's
`MIN_CLI_VERSION` row (line 93) is unaffected because the floor stays
`0.71.0`. (This repo's `CLAUDE.md` calls it "the row for hermes plugin"; there
is no version-specific row to change.)

### Submodule pointers

After the mintlify-docs and skill PRs merge, and this plugin's release PR
has merged, bump the three pointers in one PR (the monorepo's `CLAUDE.md`,
"Submodule pointer hygiene"):

```bash
cd hermes-plugin && git fetch && git checkout v0.7.0 && cd ..
cd mintlify-docs && git fetch && git checkout main && git pull && cd ..
cd skill && git fetch && git checkout main && git pull && cd ..
git add hermes-plugin mintlify-docs skill
git commit -S -m "chore: bump hermes-plugin to v0.7.0, mintlify-docs and skill submodules"
```

## 4. Back in this repo

After the skill release: refresh the bundled skill snapshot, which still pins
`@v0.6.0` in `skills/sherwood-agent/SKILL.md`, in its own PR:

```bash
./scripts/refresh_skill_pack.sh ../skill
```

Do not hand-edit `skills/sherwood-agent/`: it is a copy of the skill repo.
