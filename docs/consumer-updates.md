# Consumer-repo updates for v0.8.0

The `v0.8.0` tag points at `e92bd8573c0296853faa6de7801114874ac2455d` (the
merge of #31): `gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v0.8.0 --jq .sha`.
The consumer PRs below were opened on 2026-11-07 against
`sherwoodagent/mintlify-docs` `main` at `f23726b` and `sherwoodagent/skill`
`main` at `1f784ea`. Line numbers are from those commits. Every merge is held
for the maintainer.

| Repo | PR | Waits on |
|---|---|---|
| `sherwoodagent/mintlify-docs` | [#138](https://github.com/sherwoodagent/mintlify-docs/pull/138) | maintainer merge |
| `sherwoodagent/skill` | [#122](https://github.com/sherwoodagent/skill/pull/122) | maintainer merge (skill release 0.34.4) |
| `sherwoodagent/sherwood` | [#744](https://github.com/sherwoodagent/sherwood/pull/744) (draft) | #138 and #122 merging, then two pointer commits |

What changed for consumers in 0.8.0:

- **Install pin.** `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d`.
  Hermes still has no `@tag` install syntax, and `--ref` still takes only a
  full commit sha. Once the plugin is in the Hermes catalog,
  `hermes plugins install sherwood-monitor` works too.
- **Upgrading needs `--force`, from any earlier release.**
  `hermes plugins install ... --force --ref <SHA>` replaces the old install
  and prompts for unset `requires_env` variables. `hermes plugins update`
  refuses an install pinned with `--ref`, and it never prompts.
- **The factory is open.** It opened on 2026-11-06: `ownerOnlyProposals`
  false, `depositsRestricted` false, `creationFee` 0. A live `eth_call` at
  block 108,724,500 read all three as 0. Anyone with an ERC-8004 identity can
  create a vault, deposits are open or restricted per vault, and any
  registered agent of a vault can propose.
- **Open deposits (per fund).** `fund_setup open_deposits=true` (the vault
  created with `openDeposits: true`): a member taps 🎟 Join, links a wallet
  and deposits, with no approval step. Restricted stays the default. Once the
  vault exists, the fund card's `depositsOpen` decides.
- **`proposal_signer: agent` (per Hermes, open protocol).**
  - The agent's own wallet, held by the Sherwood CLI, owns the vault.
  - For a shipped draft or an approved guest agent, the plugin hands the
    agent the exact `sherwood strategy propose portfolio ...` /
    `sherwood vault add ...` command. A new tool, `fund_pending_commands`,
    lists them.
  - The agent runs them in a `hermes -z` one-shot or a cron with
    `approvals.cron_mode: approve`, never from a Telegram turn and never on
    boot, then records the result.
  - These commands need Sherwood CLI 0.103.0 or later (`MIN_CLI_FOR_COMMANDS`).
  - The plugin still never signs or sends a transaction. `proposal_signer: host`
    (the browser wallet on `/tg/sign`) stays the default.
- **17 tools.** 5 monitor tools plus 12 in `sherwood-fund`, adding
  `fund_pending_commands`.
- **Telegram handlers run inside the profile's secret scope.** Personal links
  now work with the gateway's `multiplex_profiles` on. Restart the gateway
  after changing a `SHERWOOD_TG_*` value.
- **Unchanged:** the cron set and cadences (the cron tables need no edit),
  `requires_env`, `MIN_CLI_VERSION` (`0.71.0`), and XMTP being Base-only.

## 1. sherwoodagent/mintlify-docs (#138)

- **`cli/installation.mdx`:**
  - The install pin (line 53) moves to the new sha.
  - The upgrade sentence (line 56) now covers any earlier release.
  - The Note (line 59) says v0.8.0.
  - A new `### Group-chat fund` subsection comes after line 62, the end of
    the Hermes section. It covers the open factory (with a link to read the
    flags live), the two settings, where agent commands run, the CLI 0.103.0
    floor for them, and that the plugin never signs.
- **`cli/cron-jobs.mdx`:** the install pin and `--force` clause (line 16). The
  cron table is unchanged.
- **`guides/set-up-grok.mdx`:** the install pin (line 26).

Checks: `npx mint validate` passed (build and validation), and
`npx mint broken-links` found no broken links.

The exact diff (#138, head `7094f8a`):

````diff
diff --git a/cli/cron-jobs.mdx b/cli/cron-jobs.mdx
index 14dcc5f..5efce70 100644
--- a/cli/cron-jobs.mdx
+++ b/cli/cron-jobs.mdx
@@ -13,7 +13,7 @@ agent-driven reasoning cron.
 
 - **Hermes Agent** installed and running (`hermes status`)
 - **Sherwood CLI** (latest, 0.96.0 or later) on PATH (`sherwood --version`), which targets Robinhood Chain (4663) by default
-- **Sherwood plugin** installed via `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref b37321824e7c41c8eeac8eda66e7571301169f32` (the v0.7.0 release commit; add `--force` to upgrade from 0.6.0). It composes on top of whichever CLI is installed, so install the CLI first
+- **Sherwood plugin** installed via `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d` (the v0.8.0 release commit; add `--force` to upgrade from an earlier release). It composes on top of whichever CLI is installed, so install the CLI first
 - **At least one fund configured** in `~/.hermes/plugins/sherwood-monitor/config.yaml`
 - **Monitor wallet** for the gas watchdog: `SHERWOOD_AGENT_ADDRESS=0x...` in the Hermes profile `.env`. The plugin does not read the Sherwood CLI's config
 - **Always-on host** — laptop sleep means missed cron runs
diff --git a/cli/installation.mdx b/cli/installation.mdx
index 64c3c59..0b7269f 100644
--- a/cli/installation.mdx
+++ b/cli/installation.mdx
@@ -50,13 +50,22 @@ cd sherwood/cli && npm install && npm run build
 If you run on [Hermes Agent](https://github.com/NousResearch/hermes-agent), install the Sherwood plugin to add always-on event streaming, autonomous cron digests and risk guardrails on top of the CLI, and to run a fund inside a Telegram group:
 
 ```bash
-hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref b37321824e7c41c8eeac8eda66e7571301169f32
+hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d
 ```
 
-Upgrading from 0.6.0? Add `--force` (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref b37321824e7c41c8eeac8eda66e7571301169f32`): it replaces the old install and prompts for the new environment variables, which `hermes plugins update` does not.
+Upgrading from an earlier release? Add `--force` (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref e92bd8573c0296853faa6de7801114874ac2455d`): it replaces the old install and prompts for any environment variables you have not set. `hermes plugins update` refuses an install pinned with `--ref`, and it never prompts.
 
 <Note>
-Plugin v0.7.0 runs on Robinhood Chain (4663). It composes on top of whichever CLI is installed, so install the CLI first. It does not read the CLI's config. For the gas watchdog, set `SHERWOOD_AGENT_ADDRESS` (the monitor wallet's address) in the Hermes profile `.env`. `SHERWOOD_PRIVATE_KEY` is only for XMTP, which works only for funds whose XMTP group id is in their ENS record on Base; Robinhood Chain funds get nothing from it, so leave it unset. If you do set it, use a dedicated key that holds nothing, never the key that manages your funds. For the group-chat fund (Telegram), see the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
+Plugin v0.8.0 runs on Robinhood Chain (4663). It composes on top of whichever CLI is installed, so install the CLI first. It does not read the CLI's config. For the gas watchdog, set `SHERWOOD_AGENT_ADDRESS` (the monitor wallet's address) in the Hermes profile `.env`. `SHERWOOD_PRIVATE_KEY` is only for XMTP, which works only for funds whose XMTP group id is in their ENS record on Base; Robinhood Chain funds get nothing from it, so leave it unset. If you do set it, use a dedicated key that holds nothing, never the key that manages your funds. For the group-chat fund (Telegram), see the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
 </Note>
 
 See the [Cron Jobs](/cli/cron-jobs) page for the autonomous monitoring stack. Skip this section if you're on Claude Code, Codex, or another runtime.
+
+### Group-chat fund
+
+The plugin can run a fund inside a Telegram group: the agent pitches trades, members back or object, and guest Hermes agents join as co-managers. The factory on Robinhood Chain opened on 2026-11-06 (`ownerOnlyProposals` false, `depositsRestricted` false, `creationFee` 0; [read the flags live](/explanations/launch-posture#reading-the-posture-yourself) before you rely on them). Anyone with an [ERC-8004](/reference/integrations/erc-8004) identity can create a vault, deposits are open or restricted per vault, and any registered agent of a vault can propose. Plugin v0.8.0 supports both postures with two independent settings:
+
+- **Deposits, per fund.** Restricted (the default): the host approves each member before they can deposit. Open (`open_deposits`, for a vault created with open deposits): a member taps 🎟 Join, links a wallet and deposits, with no approval step.
+- **Who signs, per Hermes (`proposal_signer`).** `host` (the default): the host's browser wallet owns the vault and signs owner actions on `app.sherwood.sh/tg/sign`. `agent` (open protocol): the agent's own wallet, the one the Sherwood CLI holds, owns the vault. For a draft the host shipped, or a guest agent the host approved, the plugin hands the agent the exact CLI command (`sherwood strategy propose portfolio ...` or `sherwood vault add ...`). The agent runs it with its CLI wallet in a `hermes -z` one-shot the operator starts, or in a cron set to `approvals.cron_mode: approve`, never from a Telegram turn: the group's toolset has no terminal. These commands need Sherwood CLI 0.103.0 or later. Creating the vault, setting the agent fee and the governor parameters stay CLI steps the operator runs.
+
+Either way the plugin never signs or sends a transaction. Members sign from their own wallets; the plugin builds unsigned transactions through the prepare API and hands out links or commands.
diff --git a/guides/set-up-grok.mdx b/guides/set-up-grok.mdx
index 041ad35..dd69d43 100644
--- a/guides/set-up-grok.mdx
+++ b/guides/set-up-grok.mdx
@@ -23,7 +23,7 @@ Sherwood is a skill pack plus a CLI on top of the agent you already run. Grok is
     sherwood --version
     ```
 
-    Load the Sherwood skill pack into the runtime so the agent knows the commands and the safety rules: `npx skills install sherwoodagent/skill`. On Hermes, also install the plugin: `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref b37321824e7c41c8eeac8eda66e7571301169f32`.
+    Load the Sherwood skill pack into the runtime so the agent knows the commands and the safety rules: `npx skills install sherwoodagent/skill`. On Hermes, also install the plugin: `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d`.
 
     - [CLI installation](/cli/installation)
     - [Cron jobs](/cli/cron-jobs)
````

Not changed here, and still stale: the factory flags are open, but these
pages describe the closed launch posture:

- `explanations/launch-posture.mdx`
- `learn/concepts.mdx`
- `protocol/architecture.mdx`
- `protocol/governance/overview.mdx`
- `protocol/governance/collaborative-proposals.mdx`
- `protocol/vault-liquidity.mdx`
- `protocol/strategies/walkthrough.mdx`
- `cli/commands.mdx` (lines 10 and 108)
- `cli/strategy-commands.mdx` (line 14)
- `index.mdx` (line 38)

That rewrite belongs to the protocol docs and must trace to `v1-deploy`. It
blocks site consistency, not these PRs.

## 2. sherwoodagent/skill (#122)

This is skill release 0.34.4: `.claude-plugin/plugin.json` `version` and
`SKILL.md` `metadata.version` (line 9) move together. 0.34.4 is the next
patch: draft skill PR #120 still targets 0.35.0, so whichever merges second
re-bumps first.

- **`SKILL.md`:**
  - The install pin moves to the new sha at line 27 (quick start), 1757
    (Install block), 1760 (its note and the v0.8.0 release-page link), 1769
    (rebuild block) and 1774 (upgrade line, now any earlier release).
  - Line 1731 (the section intro) says v0.8.0.
  - Line 1807 (the group-chat fund paragraph) adds `fund_pending_commands`,
    a list of the two settings (deposits, `proposal_signer`) with where
    agent-signer commands run and how to record them, the CLI 0.103.0 floor
    ("newer than this skill's pin"), and a line that a guest agent proposes
    only while `ownerOnlyProposals` is off (read live).
  - The propose command is not spelled out: its `--max-drawdown-bps` flag is
    not in the skill, and the skill's `CLAUDE.md` requires every documented
    flag to exist in the pinned CLI.
- **`CLAUDE.md`:** lines 13 and 20 describe the Hermes pin as
  `--ref <40-hex sha of the vX.Y.Z tag>`, not `@vX.Y.Z`.

Checks: `scripts/check-skill.sh` passed (skill version 0.34.4, CLI pin
0.100.5).

Follow-ups for the skill and CLI lane, outside this PR:

- **The launch section is stale.** "Launch rules (Robinhood Chain,
  invite-only launch)" (lines 43-55), "Single-operator vaults" (427-431),
  the `--open-deposits` rows (523, 606), line 1501 and the decision tree
  still describe the closed posture.
- **The CLI pin is below the agent-signer floor.** The skill pins CLI
  0.100.5, below the 0.103.0 that agent-signer commands need. Bumping it with
  `scripts/bump-cli-pin.sh` is its own skill release.

The exact diff (#122, head `f6afd03`):

````diff
diff --git a/.claude-plugin/plugin.json b/.claude-plugin/plugin.json
index f9e2c82..7e3b889 100644
--- a/.claude-plugin/plugin.json
+++ b/.claude-plugin/plugin.json
@@ -1,6 +1,6 @@
 {
   "name": "sherwood",
-  "version": "0.34.3",
+  "version": "0.34.4",
   "description": "Agent-managed ERC-4626 investment vaults on Robinhood Chain (4663) \u2014 deploy vaults, run tokenized-stock, lending and liquidity strategies, and manage vault operations via CLI or HTTP API",
   "author": {
     "name": "Sherwood",
diff --git a/CLAUDE.md b/CLAUDE.md
index 1b65f94..9f55c58 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -10,14 +10,14 @@ The Sherwood agent skill pack — installed by users via `npx skills install she
 
 When bumping any of:
 - `.claude-plugin/plugin.json` `version` field (this skill's own version)
-- The pinned Hermes plugin version (`sherwoodagent/sherwood-hermes-plugin@vX.Y.Z` in `SKILL.md`)
+- The pinned Hermes plugin release (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha of the vX.Y.Z tag>` in `SKILL.md`; Hermes has no `@tag` install syntax)
 
 …the PR MUST also touch every spot the version is referenced. Today those are:
 
 | File | Pin | What it controls |
 |---|---|---|
 | `.claude-plugin/plugin.json` | `version` | Skill plugin manifest |
-| `SKILL.md` "Running on Hermes Agent" → Install | `sherwood-hermes-plugin@vX.Y.Z` | Hermes plugin install command |
+| `SKILL.md` line 27 and "Running on Hermes Agent" → Install, rebuild and upgrade lines | `--ref <40-hex sha of the vX.Y.Z tag>` (`gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/vX.Y.Z --jq .sha`) | Hermes plugin install command |
 | `SKILL.md` `metadata.version` | same as `plugin.json` | `scripts/check-skill.sh` fails if they differ; the release tag is `v<version>` |
 
 Sub-skills under `skills/*/SKILL.md` carry their OWN `version` frontmatter — bump those independently when their behavior changes.
diff --git a/SKILL.md b/SKILL.md
index 099e2f5..5079340 100644
--- a/SKILL.md
+++ b/SKILL.md
@@ -6,7 +6,7 @@ license: MIT
 compatibility: Requires Node.js 22+, the @sherwoodagent/cli npm package, network access to Robinhood Chain (4663) RPC and https://api.sherwood.sh
 metadata:
   author: sherwood
-  version: '0.34.3'
+  version: '0.34.4'
 ---
 
 # Sherwood
@@ -24,7 +24,7 @@ If it does exist, run `sherwood --version`. It must be **0.96.0 or later**; upda
 
 Requires Node.js v22+ (including Node 24). XMTP chat runs on `@xmtp/node-sdk`, whose native bindings can fail on older glibc hosts (see [Running on Hermes Agent](#running-on-hermes-agent) for the symptom).
 
-**Running on Hermes Agent?** After installing the CLI, also install the companion plugin — `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref b37321824e7c41c8eeac8eda66e7571301169f32` (the v0.7.0 release commit; once the plugin is in the Hermes catalog, `hermes plugins install sherwood-monitor`) — which adds always-on event streaming, cron digests and risk guardrails on top of the CLI, and can run a fund inside a Telegram group. Full details in [Running on Hermes Agent](#running-on-hermes-agent) below. Skip if you're on Claude Code, Codex, or another runtime.
+**Running on Hermes Agent?** After installing the CLI, also install the companion plugin — `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d` (the v0.8.0 release commit; once the plugin is in the Hermes catalog, `hermes plugins install sherwood-monitor`) — which adds always-on event streaming, cron digests and risk guardrails on top of the CLI, and can run a fund inside a Telegram group. Full details in [Running on Hermes Agent](#running-on-hermes-agent) below. Skip if you're on Claude Code, Codex, or another runtime.
 
 **HTTP API (no CLI install).** Live base: `https://api.sherwood.sh` with root paths (`/chains`, `/prepare/identity-mint`, `/vaults/:address`). That host is already v1 — do **not** add a `/v1` prefix (`https://api.sherwood.sh/v1/...` 404s). `https://www.sherwood.sh/api/v1` also 404s. Catalog: `GET https://api.sherwood.sh/`. See [references/external-signer-integration.md](references/external-signer-integration.md).
 
@@ -1728,7 +1728,7 @@ One line each; the linked section has the detail.
 
 ## Running on Hermes Agent
 
-If you (the agent) are running on the [Hermes agent runtime](https://github.com/NousResearch/hermes-agent), there is a dedicated plugin — **`sherwood-monitor`** — that gives you always-on reactive awareness of your vaults beyond what the CLI alone provides. This is a **separate install** from the skill pack and CLI above. Skip this section if you're on Claude Code, Codex, or another runtime. The current plugin release (v0.7.0) runs on Robinhood Chain (4663). Its risk guardrails are advisory (they fail open when vault state is unavailable), so rely on the CLI's own preflights. XMTP streaming and posting work only for vaults whose XMTP group id is in their ENS record on Base; Robinhood Chain vaults have no such record, so for them the plugin streams on-chain events only.
+If you (the agent) are running on the [Hermes agent runtime](https://github.com/NousResearch/hermes-agent), there is a dedicated plugin — **`sherwood-monitor`** — that gives you always-on reactive awareness of your vaults beyond what the CLI alone provides. This is a **separate install** from the skill pack and CLI above. Skip this section if you're on Claude Code, Codex, or another runtime. The current plugin release (v0.8.0) runs on Robinhood Chain (4663). Its risk guardrails are advisory (they fail open when vault state is unavailable), so rely on the CLI's own preflights. XMTP streaming and posting work only for vaults whose XMTP group id is in their ENS record on Base; Robinhood Chain vaults have no such record, so for them the plugin streams on-chain events only.
 
 ### What the plugin adds
 
@@ -1754,10 +1754,10 @@ command -v hermes && hermes plugins list | grep -q sherwood-monitor && echo "ins
 ### Install
 
 ```bash
-hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref b37321824e7c41c8eeac8eda66e7571301169f32
+hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d
 ```
 
-Hermes has no `@tag` install syntax: `--ref` takes the full commit sha, here the commit the v0.7.0 tag points at (also on the [v0.7.0 release page](https://github.com/sherwoodagent/sherwood-hermes-plugin/releases/tag/v0.7.0)). Once the plugin is in the Hermes catalog, `hermes plugins install sherwood-monitor` installs the reviewed pin.
+Hermes has no `@tag` install syntax: `--ref` takes the full commit sha, here the commit the v0.8.0 tag points at (also on the [v0.8.0 release page](https://github.com/sherwoodagent/sherwood-hermes-plugin/releases/tag/v0.8.0)). Once the plugin is in the Hermes catalog, `hermes plugins install sherwood-monitor` installs the reviewed pin.
 
 Requirements: Python ≥ 3.11, **Node ≥ 22 and npm** (for the bundled sidecar build), and a configured Sherwood CLI (the monitor runs it; the CLI reads its own config). The plugin never reads `~/.sherwood/config.json`: for XMTP and the gas watchdog, set the monitor wallet in the Hermes profile `.env` (`SHERWOOD_PRIVATE_KEY`, optional, the sidecar derives its own key from it; or `SHERWOOD_AGENT_ADDRESS` alone for the gas watchdog). The install runs `npm ci && npm run build` inside the sidecar directory (~30s, one-time).
 
@@ -1766,12 +1766,12 @@ The plugin runs a preflight on load. If it doesn't find `sherwood --version`, or
 If the install fails mid-sidecar (no Node, npm offline, etc.), everything except XMTP still works. Rebuild later with:
 
 ```bash
-SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref b37321824e7c41c8eeac8eda66e7571301169f32
+SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d
 cd "$(python3 -c 'import sherwood_monitor, pathlib; print(pathlib.Path(sherwood_monitor.__file__).parent.parent / "xmtp_sidecar")')"
 npm ci && npm run build
 ```
 
-To upgrade an existing 0.6.0 install, add `--force` (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref b37321824e7c41c8eeac8eda66e7571301169f32`): it replaces the old install and prompts for the new environment variables, which `hermes plugins update` does not.
+To upgrade from an earlier release, add `--force` (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref e92bd8573c0296853faa6de7801114874ac2455d`): it replaces the old install and prompts for any environment variables you have not set. `hermes plugins update` refuses an install pinned with `--ref`, and it never prompts.
 
 ### One-time onboarding per vault
 
@@ -1804,7 +1804,14 @@ concentration_threshold_pct: 30.0
 | `sherwood_monitor_exposure()` | Answer cross-fund exposure questions |
 | `sherwood_monitor_cron_tick(subdomain, include_exposure=true)` | What the autonomous cron calls; you can call manually |
 
-The group-chat fund adds the `sherwood-fund` toolset (`fund_status`, `fund_setup`, `fund_claim_topic`, `fund_release_claim`, `fund_draft_proposal`, `fund_set_proposal_status`, `fund_record_vote`, `fund_record_settle`, `fund_approve_member`, `fund_approve_agent`, `fund_remove_agent`), the Hermes slash commands `/fund`, `/join-fund`, `/fund-approve`, `/fund-agents`, `/leaderboard` (in the Telegram group: `/fund`, `/leaderboard`), and the skills `fund-pitch`, `fund-propose`, `fund-settle`. Setup and the required deployment boundary: the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
+The group-chat fund adds the `sherwood-fund` toolset (`fund_status`, `fund_setup`, `fund_claim_topic`, `fund_release_claim`, `fund_draft_proposal`, `fund_set_proposal_status`, `fund_record_vote`, `fund_record_settle`, `fund_approve_member`, `fund_approve_agent`, `fund_remove_agent`, `fund_pending_commands`), the Hermes slash commands `/fund`, `/join-fund`, `/fund-approve`, `/fund-agents`, `/leaderboard` (in the Telegram group: `/fund`, `/leaderboard`), and the skills `fund-pitch`, `fund-propose`, `fund-settle`. Setup and the required deployment boundary: the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
+
+Two settings shape a fund, independently:
+
+- **Deposits, per fund.** Restricted (the default): the host approves each member before they can deposit. Open (`open_deposits`, for a vault created with open deposits): a member links a wallet and deposits with no approval step. Once the vault exists, the chain's deposit rules decide.
+- **Who signs, per Hermes (`proposal_signer`).** `host` (the default): the host's browser wallet owns the vault and signs owner actions on `app.sherwood.sh/tg/sign`. `agent` (open protocol): your own wallet, the one the Sherwood CLI holds, owns the vault. The plugin still never signs: for a draft the host shipped, or a guest agent the host approved, `fund_pending_commands` lists the exact `strategy propose portfolio` or `vault add` command. Run it with your CLI wallet only in a `hermes -z` one-shot the operator started or a cron set to `approvals.cron_mode: approve`, never from a Telegram turn and never on boot, then record the result (`fund_set_proposal_status <id> proposed` with `onchain_id` and `tx_hash`, or `fund_approve_agent` with `tx_hash`). These commands need Sherwood CLI 0.103.0 or later, newer than this skill's pin; `fund_pending_commands` warns when the installed CLI is older. Creating the vault, the agent fee and the governor parameters are CLI steps the operator runs.
+
+A guest agent can propose only while the factory's `ownerOnlyProposals` flag is off: read it live ([Single-operator vaults](#single-operator-vaults)). The plugin's docs record the factory as open since 2026-11-06 (`ownerOnlyProposals` false, `depositsRestricted` false, `creationFee` 0).
 
 ### Reference
 
````

## 3. sherwoodagent/sherwood (monorepo, #744, draft)

Already on the branch (`chore/hermes-plugin-0.8.0-submodules`):

- **`hermes-plugin` pointer:** `b373218` (v0.7.0) → `e92bd85` (v0.8.0).
- **`CLAUDE.md` "Hermes plugin bump":**
  - The release workflow tags on merge.
  - Consumer pins use `--ref <SHA>`.
  - New rows: `guides/set-up-grok.mdx`, the skill's upgrade and rebuild
    lines, and the submodule pointer.
  - The CLI table's `MIN_CLI_VERSION` row is unaffected: the floor stays
    `0.71.0`.

It waits on mintlify-docs #138 and skill #122. After both merge, add the two
pointers as signed commits and take the PR out of draft:

```bash
git submodule update --init mintlify-docs skill
git -C mintlify-docs fetch origin && git -C mintlify-docs checkout origin/main
git -C skill fetch origin && git -C skill checkout origin/main
git add mintlify-docs skill
git commit -S -m "chore: bump mintlify-docs and skill submodules (Hermes plugin 0.8.0)"
git log --format='%h %G?' origin/main..HEAD   # G on every commit
```

## 4. Back in this repo

- **Refresh the bundled skill pack after skill #122 merges.** The bundled
  `skills/sherwood-agent/` at v0.8.0 is skill 0.34.3: it still carries the
  0.7.0 pin and the 11-tool fund list. Refresh it in its own PR:

  ```bash
  ./scripts/refresh_skill_pack.sh ../skill
  ```

  Do not hand-edit `skills/sherwood-agent/`: it is a copy of the skill repo.
- **Release notes.** The v0.8.0 release notes do not carry the install sha
  yet. `docs/catalog-submission.md` step 2 has the `gh release edit` command.
  It is part of the release, held for the maintainer.
- **`docs/catalog-submission.md` is current for 0.8.0.**
  - It names v0.8.0 throughout and says 17 tools, matching `plugin.yaml`
    `provides_tools`. "16 tools" appears nowhere in the repo.
  - Its remaining 0.7.0 mention is the history note "Rule 11 ... resolved in
    0.7.0", which is still accurate.
