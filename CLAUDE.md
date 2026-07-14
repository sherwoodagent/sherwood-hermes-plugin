# CLAUDE.md — Sherwood Hermes Plugin

## What this repo is

The Sherwood plugin for Hermes Agent — adds always-on event streaming, no_agent watchdog crons + an agent reasoning cron, risk guardrails (`pre_tool_call`), and institutional memory on top of the Sherwood CLI. Plugin name in Hermes: `sherwood-monitor`.

## Version-bump checklist (REQUIRED on every release PR)

When bumping the plugin version, ALL of these must move together:

| File | What to update |
|---|---|
| `plugin.yaml` | `version` field |
| `pyproject.toml` | `version` field |
| `BOOT.md` (if behavior changed) | reflect new cron entries / hooks |
| `README.md` (if install / UX changed) | install command, cron list, config keys |
| Git tag | Create `v<version>` tag pointing at the merge commit |

Then open follow-up PRs in the consumer repos:

| Repo | File | What to update |
|---|---|---|
| `sherwoodagent/sherwood` (main) | `mintlify-docs/cli/cron-jobs.mdx` | Cron list, install pin |
| `sherwoodagent/sherwood` (main) | `mintlify-docs/cli/installation.mdx` | Install pin |
| `sherwoodagent/sherwood` (main) | `CLAUDE.md` § Version bump checklist | Update the row for hermes plugin |
| `sherwoodagent/skill` | `SKILL.md` "Running on Hermes Agent" → Install | `@vX.Y.Z` pin |
| `sherwoodagent/skill` | `CLAUDE.md` | Update reference if needed |

## `MIN_CLI_VERSION` (separate floor)

`sherwood_monitor/preflight.py` pins `MIN_CLI_VERSION` — the minimum Sherwood CLI version this plugin works against. Do NOT bump it unless a new plugin feature actually requires a newer CLI subcommand or flag. The current floor is `0.71.0` — the release that ships the `sherwood fund` command (the `syndicate` → `fund` product rename), which the plugin's docs and bundled skill pack reference. (0.68.0/0.69.0 were claimed by the parallel Robinhood-testnet work and 0.70.0 by the per-vault governor #421, so the rename landed in 0.71.0.) (The earlier floor was `0.40.5` for `sherwood session check --no-xmtp`, which the supervisor still passes.)

If you bump `MIN_CLI_VERSION`, the consumer-repo updates above also need to bump their CLI pin to ≥ the new floor.

## Cron stack (current as of v0.6.0)

Five entries registered by `hermes sherwood install-cron`:
- `sherwood-monitor-digest` (no_agent, every 15m)
- `sherwood-aum-watchdog` (no_agent, every 15m)
- `sherwood-gas-watchdog` (no_agent, every 30m)
- `sherwood-stream-watchdog` (no_agent, every 5m)
- `sherwood-proposal-reasoning` (agent, every 6h)

When this set changes (add / remove / rename / cadence change), update:
- `BOOT.md` § Cron setup
- `README.md` cron table
- Consumer doc: `mintlify-docs/cli/cron-jobs.mdx` (it now mirrors this table)

## Don't reintroduce testnet examples

Beta is mainnet-only on Base + HyperEVM (Robinhood L2 testnet is a deliberate exception in the consumer docs but is NOT relevant here — this plugin doesn't deploy contracts). Don't add `--testnet` flags to README / BOOT examples.
