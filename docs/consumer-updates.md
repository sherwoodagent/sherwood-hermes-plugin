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
Plugin v0.7.0 runs on Robinhood Chain (4663). It composes on top of whichever CLI is installed (0.71.0 or later), so install the CLI first. For the group-chat fund (Telegram), see the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
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

The cron table (lines 30-36) and "Verify" (`5 active sherwood-* entries`)
stay as they are: the cron set did not change.

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

Lines 1757 and 1767: `sherwood-hermes-plugin@v0.6.0` becomes
`sherwood-hermes-plugin@v0.7.0` (the plain install and the
`SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1` install).

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
