# CLAUDE.md — Sherwood Hermes Plugin

## What this repo is

The Sherwood plugin for Hermes Agent — adds always-on event streaming, no_agent watchdog crons + an agent reasoning cron, risk guardrails (`pre_tool_call`), and institutional memory on top of the Sherwood CLI, plus the group-chat fund (a Sherwood fund run in a Telegram group: `sherwood_monitor/fund/`, `sherwood_monitor/telegram/`, `docs/fund-host-join.md`). Plugin name in Hermes: `sherwood-monitor`.

## Version-bump checklist (REQUIRED on every release PR)

When bumping the plugin version, ALL of these must move together:

| File | What to update |
|---|---|
| `plugin.yaml` | `version` field |
| `pyproject.toml` | `version` field |
| `BOOT.md` (if behavior changed) | reflect new cron entries / hooks |
| `README.md` (if install / UX changed) | install command, cron list, config keys |
| `catalog/sherwood-monitor.yaml` | `version`, the `sha` placeholder `"<sha of the v<version> tag commit>"` (and its comment), and `capabilities` if tools / hooks / env changed (`tests/test_catalog_entry.py` checks all of it against `plugin.yaml`); the real `sha` goes in only in the catalog PR, after the tag (`docs/catalog-submission.md`) |
| Git tag | `.github/workflows/release.yml` creates `v<version>` on the merge commit when the version has no tag; check that it exists |

Then open follow-up PRs in the consumer repos:

| Repo | File | What to update |
|---|---|---|
| `sherwoodagent/mintlify-docs` | `cli/cron-jobs.mdx` | Cron list, install pin (`--ref <sha>`) |
| `sherwoodagent/mintlify-docs` | `cli/installation.mdx` | Install, headless-install and upgrade lines (`--ref <sha>`) |
| `sherwoodagent/mintlify-docs` | `guides/set-up-grok.mdx` | Install pin (`--ref <sha>`) |
| `sherwoodagent/skill` | `SKILL.md` line 27 and "Running on Hermes Agent" → Install and upgrade lines | `--ref <sha>` pin; a skill release (bump its version) |
| `sherwoodagent/skill` | `CLAUDE.md` | Update reference if needed |
| `sherwoodagent/sherwood` (main) | `CLAUDE.md` § Version bump checklist, and the `hermes-plugin`, `mintlify-docs`, `skill` submodule pointers | The Hermes plugin rows if a surface moved; the pointers once the consumer PRs merge |

Hermes has no `@tag` install syntax: every pin is the 40-hex commit the tag points at
(`gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v<version> --jq .sha`).
`docs/consumer-updates.md` lists each release's exact hunks.

## `MIN_CLI_VERSION` (separate floor)

`sherwood_monitor/preflight.py` pins `MIN_CLI_VERSION` — the minimum Sherwood CLI version this plugin works against. Do NOT bump it unless a new plugin feature actually requires a newer CLI subcommand or flag. The current floor is `0.71.0` — the release that ships the `sherwood fund` command (the `syndicate` → `fund` product rename), which the plugin's docs and bundled skill pack reference. (0.68.0/0.69.0 were claimed by the parallel Robinhood-testnet work and 0.70.0 by the per-vault governor #421, so the rename landed in 0.71.0.) (The earlier floor was `0.40.5` for `sherwood session check --no-xmtp`, which the supervisor still passes.)

If you bump `MIN_CLI_VERSION`, the consumer-repo updates above also need to bump their CLI pin to ≥ the new floor.

## Cron stack (current as of v0.7.0)

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

How registration works on Hermes 0.22 (do not regress it; `tests/test_cli.py`
runs the real `hermes` binary in a scratch `HERMES_HOME` when one is on PATH):
`hermes cron create <schedule> [<prompt>]` takes both positionally; Hermes runs
cron scripts only from `$HERMES_HOME/scripts/` and passes them no arguments, so
`install-cron` writes one `sherwood-monitor-<mode>.py` wrapper per watchdog
there and edits existing jobs by id instead of creating duplicates. The stream
watchdog reads `stream_status.json`, written by the in-process Supervisor; a
fresh `hermes sherwood status` process cannot see the running streams.

Stream lifetime (review #29 P5): Hermes 0.22 fires `on_session_end` after EVERY
turn and `on_session_finalize` on `/new` too. Never stop streams there. They run
on the plugin's own loop (`sherwood_monitor/bgloop.py`) for the life of the
process; teardown is `on_session_finalize` with `reason == "shutdown"` plus
`atexit`. Hermes' own tool loops do not run between turns (CLI) or are closed
after each call (gateway), so nothing long-lived may be scheduled on them.
`tests/test_lifecycle.py` pins this.

Install command: Hermes has no `owner/repo@tag` syntax. Docs say
`hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha>`
(or the catalog name once listed).

## Don't reintroduce testnet examples

Sherwood is mainnet-only on Robinhood Chain (4663); the group-chat fund uses USDG there. This plugin deploys no contracts. Don't add `--testnet` flags or testnet chains to README / BOOT / docs examples.
