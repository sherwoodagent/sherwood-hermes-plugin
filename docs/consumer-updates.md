# Consumer-repo updates for v0.7.0

Open these PRs **after** the `v0.7.0` tag exists (the release workflow creates
it when the release PR merges; see `docs/catalog-submission.md` step 1). They
are listed here so a later lane can open them as written. Line numbers are
from each repo's `main` on 2026-11-05: `sherwoodagent/mintlify-docs`
`bbdb69c`, `sherwoodagent/skill` `081d67f`. Re-check them before editing.

What changed for consumers in 0.7.0:

- **Install command.** Hermes has no `@tag` install syntax (`owner/repo@v0.6.0`
  fails: Hermes turns it into a repository URL), and `--ref` takes only a full
  40-character commit sha. Every consumer pin becomes
  `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <SHA>`,
  where `<SHA>` is the commit the `v0.7.0` tag points at
  (`gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v0.7.0 --jq .sha`,
  see `docs/catalog-submission.md` step 2). Put the real 40-hex value in the
  consumer PRs; only this repo's own tree keeps a placeholder. Once the plugin
  is in the Hermes catalog, `hermes plugins install sherwood-monitor` works too.
- **XMTP is Base-only.** The sidecar resolves a fund's XMTP group only from
  the ENS record on Base, which current CLI releases do not write, so
  Robinhood Chain funds get on-chain events only. Consumer text that promises
  XMTP streaming for Robinhood Chain funds is wrong.
- **install-cron works now** (0.6.0's registered nothing on Hermes 0.22); the
  cron set and cadences are unchanged.
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
hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <SHA>
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
- **Sherwood plugin** installed via `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <SHA>` (`<SHA>`: the v0.7.0 release commit). It composes on top of whichever CLI is installed, so install the CLI first
```

After line 17 (`- **At least one fund configured** ...`), add:

```
- **Monitor wallet** for the gas watchdog: `SHERWOOD_AGENT_ADDRESS=0x...` in the Hermes profile `.env`. The plugin does not read the Sherwood CLI's config
```

In "Install (one-time)", after the code block, add:

```
It writes one wrapper per watchdog to `$HERMES_HOME/scripts/` (Hermes runs cron scripts only from there) and creates or updates the five crons. Re-run it after upgrading from 0.6.0, whose install-cron registered nothing on Hermes 0.22.
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

This repo's bundled copy (`skills/sherwood-agent/SKILL.md`) already carries
the 0.7.0 text: it is patched at the pin because the catalog reviews that
tree. Port the same hunks upstream, with the real sha in place of
`<40-hex sha of the v0.7.0 release commit>`:

```bash
# in this repo, after the tag:
git diff v0.6.0 v0.7.0 -- skills/sherwood-agent/SKILL.md
```

The hunks, by upstream line (skill `main` at `081d67f`):

- **Line 27** ("Running on Hermes Agent?"): the install command becomes
  `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <SHA>` with the catalog alternative, and the sentence adds the Telegram
  group fund.
- **Line 1731** (section intro): replace "The current plugin release (v0.6.0)
  predates the v1 protocol: ..." with the 0.7.0 sentence: Robinhood Chain
  (4663), guardrails advisory, and XMTP streaming/posting only for vaults
  whose XMTP group id is in their ENS record on Base.
- **Line 1757** (Install block) and **line 1767** (rebuild block): the
  `--ref` command, plus the note that Hermes has no `@tag` syntax.
- **Line 1759** (Requirements): drop "`~/.sherwood/config.json` with a
  `privateKey`"; the plugin never reads it. XMTP and the gas watchdog use
  `SHERWOOD_PRIVATE_KEY` (optional) or `SHERWOOD_AGENT_ADDRESS` alone for
  the gas watchdog, in the Hermes profile `.env`.
- **Line 1761** (preflight): no config-file check; the sidecar warning only
  with `SHERWOOD_PRIVATE_KEY` set.
- **Line 1773** (onboarding): only with `SHERWOOD_PRIVATE_KEY`, and only for
  vaults with a Base ENS record; adding the sidecar to a Robinhood Chain
  vault does not help.

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

`skills/sherwood-agent/SKILL.md` was patched locally for 0.7.0 (review
#29, N1/N6): the catalog reviews the pinned tree, and the upstream skill
still described 0.6.0. The next `./scripts/refresh_skill_pack.sh ../skill`
overwrites the bundled copy with upstream, so land the upstream skill edit
(section 2) first, then refresh in its own PR:

```bash
./scripts/refresh_skill_pack.sh ../skill
```

Apart from this one patch, do not hand-edit `skills/sherwood-agent/`: it is a
copy of the skill repo.
