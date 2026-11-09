# Consumer-repo updates for v0.8.4

## Next: v0.8.4 (tag pending)

The release workflow tags `v0.8.4` on the merge of the PR that carries the
0.8.4 release commit (#38). Once it does, the consumer docs move their install
pin to that sha, with the same edits as the 0.8.1 PRs below. Two more consumer
doc edits come from the cold install of 0.8.3:
- A headless install needs `--yes-deps --enable`.
- The sidecar rebuild recipe should build in
  `${HERMES_HOME:-$HOME/.hermes}/plugins/sherwood-monitor/xmtp_sidecar`. The
  `python3 -c "import sherwood_monitor"` path finds neither the plugin dir nor
  Hermes' workspace copy.

What 0.8.4 changes for consumers:

- **Approvals in a Generic Call plan.** The three forms 0.8.3 let through
  are refused:
  - an approve inside a multicall-style wrapper. The plugin opens
    `multicall(bytes[])`, `multicall(uint256,bytes[])`,
    `multicall(bytes32,bytes[])`, `batch(bytes[],bool)` and the Multicall3
    `aggregate` family, up to three wrappers deep; a wrapper it cannot
    decode is refused;
  - `increaseApproval`;
  - ERC777 `authorizeOperator`.

  `approveAndCall` is covered too.
- **Asset approvals are capped at the pull.** An approval of the vault
  asset may not exceed the plan's pull (`approve_above_pull`): the asset the
  clone takes from the vault at execute. Another token's approval is still
  refused only at or above 2^255 (2^159 for Permit2), because the plan does
  not say how much of it the clone will hold.
- **Settle patches into approvals.** A settle patch into a call that
  approves may write only that approval's amount word, only from the
  approved token's own balance, and never scaled up (`approve_patch`,
  `unlimited_approve`). An operator approval or any call to an opened
  wrapper (multicall, batch, Multicall3 `aggregate*`) takes no patch.
- **Signatures.** A signature nesting deeper than 16 levels is refused
  cleanly, instead of raising RecursionError.
- **XMTP sidecar location.** The plugin uses a built sidecar in its own
  directory first, then the build `setup.py` made in Hermes' dependency
  workspace (found through the package's install record). The README's new
  "Build the XMTP sidecar" gives the reliable hand build.
- **Install docs.** The README covers a headless `--yes-deps --enable`, and
  the sha comes from `gh api` first.
- **Settlement memory.** The `<sherwood-settlement>` block names the
  `remember-settlement` steps the way Hermes resolves them:
  `skill_view name="sherwood-monitor:sherwood-agent"`,
  `file_path="skills/remember-settlement/SKILL.md"`.
- **CLI warning.** `cli_warning` lists only what the installed CLI lacks.
  0.103.0 under a generic-call command now gets "cannot propose
  generic-call" alone.
- **Catalog review notes.** `docs/catalog-submission.md` explains
  validate's rule-9 warning (the guarded `owner_secret_scope` import) and
  what `show`/`validate` print about isolation.
- **Unchanged:** tools, hooks, slash commands, `requires_env`, crons,
  `MIN_CLI_VERSION` (`0.71.0`), the CLI floors (0.103.0 portfolio, 0.104.0
  generic-call), and the network calls.

## v0.8.3

The `v0.8.3` tag points at `5d4bd5bab798c96e4e5e3c2fc863929b1a8eeaf3` (the merge of
#36): `gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v0.8.3 --jq .sha`.
Install: `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref 5d4bd5bab798c96e4e5e3c2fc863929b1a8eeaf3`
(add `--force` when upgrading from an earlier `--ref` install).

The consumer PRs opened for 0.8.1 now pin 0.8.3, with the same edits as the
0.8.1 section below (0.8.2 was never pinned in a consumer repo). Every merge
is held for the maintainer.

| Repo | PR | 0.8.3 commit | Waits on |
|---|---|---|---|
| `sherwoodagent/mintlify-docs` | [#139](https://github.com/sherwoodagent/mintlify-docs/pull/139) | `aa13f46` | maintainer merge |
| `sherwoodagent/skill` | [#123](https://github.com/sherwoodagent/skill/pull/123) | `8e9a628` | maintainer merge (skill release 0.35.2) |
| `sherwoodagent/sherwood` | [#744](https://github.com/sherwoodagent/sherwood/pull/744) (draft) | `01323ee` | #139 and #123 merging, then two pointer commits |

What 0.8.3 changes for consumers:

- **Generic Call drafts.** `fund_draft_proposal` takes `template:
  "generic-call"` and a `calls` plan (`executeCalls`, `settleCalls`,
  `settlePatches`, `sweepTokens`) instead of legs. The plugin checks the plan
  the way the Sherwood CLI 0.104.0 compiles it (indices, amounts, addresses,
  hex, signatures and argument types, settle patches), so a plan it accepts is
  one the CLI compiles; it refuses an unlimited approval given as approve,
  increaseAllowance, Permit2 approve, permit (EIP-2612 or DAI) or
  setApprovalForAll, in raw `data` or `signature` + `args`. In 0.8.3 three
  forms still pass: an approve wrapped in a multicall, `increaseApproval`,
  and ERC777 `authorizeOperator`; `main` refuses them after 0.8.3 (see
  "Next" above). The draft's preview is unsigned and not
  execution acceptance, and it is not ready when this host's `sherwood
  strategy list` does not print generic-call as available. Only under
  `proposal_signer: agent`: after the host's 🚀 Ship it the plugin writes the
  plan to `<fund state dir>/<fund>/generic-call/p-N.json` (mode 0600) and
  hands out `sherwood strategy propose generic-call --vault ... --calls
  <plan.json> --amount ... --duration ... --max-drawdown-bps ...`, which
  needs the Sherwood CLI 0.104.0 or later. Under `proposal_signer: host` the
  draft is refused (no prepare route) and nothing is recorded. Generic Call
  is an unaudited template: guardians abstain, so execute stays blocked until
  a guardian operator allows it, and the draft card says so.
- **Fund scope per turn.** A turn in a fund's Telegram group, from its host,
  a member or the owner, and an operator turn (CLI, cron: `default_fund`,
  else the only fund) get a `<sherwood-fund-scope>` block: that fund's chain,
  vault and asset, and the templates `sherwood strategy list` printed on this
  host. Bundled skills for other networks (Moonwell, levered swap, memecoin
  alpha and others) are labeled other-network examples, and no yield is
  quoted. Other chats, DMs and strangers get none. `fund_status` returns the
  same `scope`.
- **Fixes in this release.** The strategy-list header is no longer read as a
  template; the list is cached 10 minutes (a failure 60 s) and read off the
  event loop with a 3 s budget; the plan file path is absolute and is only
  written once the command is built; the pitch skill keeps the official
  stock-token list for baskets.
- **Consumer docs:** besides the pin, a page that lists what an agent can
  propose under `proposal_signer: agent` may add Generic Call with its
  0.104.0 CLI floor. No CLI pin moves: the plugin-wide `MIN_CLI_VERSION`
  stays `0.71.0` and portfolio commands still need 0.103.0.
- **Unchanged:** tools, hooks, slash commands, `requires_env`, crons,
  `MIN_CLI_VERSION` (`0.71.0`), and the network calls. New child process: the
  read-only `sherwood strategy list`.

## v0.8.2

The `v0.8.2` tag points at `736eac3e51c08332f96868054ecb3c473b6759bb` (the
merge of #35): `gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v0.8.2 --jq .sha`.
What 0.8.2 changed for consumers:

- **NAV includes deployed capital after execute.** The vault's `totalAssets()`
  counts only the cash it holds: a PortfolioStrategy takes the capital out at
  execute. 0.8.1 therefore showed, for example, "NAV 60 USDG · P&L -40.0%" for
  a 100 USDG fund with 40 deployed. The card now adds the strategy's value:
  its valid `positionValue`, else what it invested, else, for a strategy with
  no positions such as a Generic Call clone, the proposal's `maxCapital`. On
  Robinhood Chain the PortfolioStrategy has no `positionValue()`, so the
  usual card there reads "NAV 100 USDG (at cost) · P&L n/a". When the read
  cannot see the strategy at all, the card shows "NAV ≥<cash> · P&L n/a"
  with a notice, never the cash alone as a loss. P&L is NAV per share
  against 1.
- **Positions block.** While a strategy is active, the card lists its tokens:
  live value and P&L for one token with a live value, otherwise each "at cost"
  (plus an "Unrealized" row when the value is live). A "Cash" row comes last
  and absorbs rounding, so the rows add up to the NAV. "none yet" only shows
  with no strategy.
- **Hardening.** A broken read can no longer break the card or replace a newer
  snapshot. That covers a NaN or millisecond `asOf`, a total too large to
  print, and a read without a NAV block, which shows "NAV unavailable" rather
  than 0. The older-read check compares blocks.
- **Size bar cap.** A proposal's "Size … of NAV" bar says ">999%" instead of
  printing absurd percentages.
- **Unchanged:** tools, hooks, slash commands, `requires_env`, crons,
  `MIN_CLI_VERSION` (`0.71.0`), and the network calls (the same tg-card read).

## v0.8.1

The `v0.8.1` tag points at `cedb3a9dee16ec7a8afc80c55374851b471d8230` (the
merge of #33): `gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v0.8.1 --jq .sha`.

0.8.0's consumer edits have already landed: sherwoodagent/mintlify-docs#138
and sherwoodagent/skill#122 (skill 0.35.1). This file's 0.8.0 version is in
git history at `6f5b99a`. For 0.8.1, the consumers need only the new pin.

The PRs below were opened on 2026-11-07. Their line numbers are from
`sherwoodagent/mintlify-docs` `main` at `7700d18` and `sherwoodagent/skill`
`main` at `fef468c`. Every merge is held for the maintainer.

| Repo | PR | Waits on |
|---|---|---|
| `sherwoodagent/mintlify-docs` | [#139](https://github.com/sherwoodagent/mintlify-docs/pull/139) | maintainer merge |
| `sherwoodagent/skill` | [#123](https://github.com/sherwoodagent/skill/pull/123) | maintainer merge (skill release 0.35.2) |
| `sherwoodagent/sherwood` | [#744](https://github.com/sherwoodagent/sherwood/pull/744) (draft) | #139 and #123 merging, then two pointer commits |

What changed for consumers in 0.8.1:

- **Install pin.** `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref cedb3a9dee16ec7a8afc80c55374851b471d8230`.
  Upgrading from any earlier `--ref` install still needs `--force`.
- **Fund card fix (#33).** The `/fund` card shows the vault's NAV and
  share-price P&L from the tg-card read (`GET funds/<vault>/tg-card`). Each
  read stores a NAV snapshot in the fund state. P&L is `(sharePrice - 1)` in
  bps. When a read fails, the card shows the last snapshot ("Last known NAV,
  as of HH:MM UTC.") or "NAV unavailable", never a made-up 0. The Members
  line explains depositors who deposited from the app without tapping
  🎟 Join: "Members 0 (depositors haven't tapped 🎟 Join yet)". Details are in
  [docs/telegram.md](telegram.md).
- **The Telegram `/fund` command now triggers one tg-card read** when the
  30 s throttle allows. The Hermes in-session `/fund` slash command and the
  `fund_status` tool render from stored state without a read. Before 0.8.1,
  the 60 s refresh read only while a proposal was live. The read also
  applies the chain's `depositsOpen`, so the open-deposits posture can update
  before the first proposal: an open read applies at once, and an open fund
  closes only after two consecutive restricted reads.
- **Unchanged:** the tools (17), hooks, slash commands, `requires_env`, the
  cron set and cadences, `MIN_CLI_VERSION` (`0.71.0`), the agent-signer CLI
  floor (`0.103.0`), and the group-chat fund wording in the consumer docs.

## 1. sherwoodagent/mintlify-docs (#139)

- **`cli/installation.mdx`:**
  - The new sha goes on the install line (53) and the upgrade line (56).
  - The Note (59) says v0.8.1.
  - The postures paragraph (66) now reads "Since v0.8.0 the plugin supports
    both postures".
- **`cli/cron-jobs.mdx`:** the new sha and "the v0.8.1 release commit"
  (line 16).
- **`guides/set-up-grok.mdx`:** the new sha (line 26).

Checks: `npx mint validate` passed (build and validation), and
`npx mint broken-links` found no broken links.

The exact diff (#139, head `8b8a5f6`, against `main` `7700d18`):

````diff
diff --git a/cli/cron-jobs.mdx b/cli/cron-jobs.mdx
index 5efce70..2c0e1d0 100644
--- a/cli/cron-jobs.mdx
+++ b/cli/cron-jobs.mdx
@@ -13,7 +13,7 @@ agent-driven reasoning cron.
 
 - **Hermes Agent** installed and running (`hermes status`)
 - **Sherwood CLI** (latest, 0.96.0 or later) on PATH (`sherwood --version`), which targets Robinhood Chain (4663) by default
-- **Sherwood plugin** installed via `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d` (the v0.8.0 release commit; add `--force` to upgrade from an earlier release). It composes on top of whichever CLI is installed, so install the CLI first
+- **Sherwood plugin** installed via `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref cedb3a9dee16ec7a8afc80c55374851b471d8230` (the v0.8.1 release commit; add `--force` to upgrade from an earlier release). It composes on top of whichever CLI is installed, so install the CLI first
 - **At least one fund configured** in `~/.hermes/plugins/sherwood-monitor/config.yaml`
 - **Monitor wallet** for the gas watchdog: `SHERWOOD_AGENT_ADDRESS=0x...` in the Hermes profile `.env`. The plugin does not read the Sherwood CLI's config
 - **Always-on host** — laptop sleep means missed cron runs
diff --git a/cli/installation.mdx b/cli/installation.mdx
index bc13cf9..3e91e87 100644
--- a/cli/installation.mdx
+++ b/cli/installation.mdx
@@ -50,20 +50,20 @@ cd sherwood/cli && npm install && npm run build
 If you run on [Hermes Agent](https://github.com/NousResearch/hermes-agent), install the Sherwood plugin to add always-on event streaming, autonomous cron digests and risk guardrails on top of the CLI, and to run a fund inside a Telegram group:
 
 ```bash
-hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d
+hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref cedb3a9dee16ec7a8afc80c55374851b471d8230
 ```
 
-Upgrading from an earlier release? Add `--force` (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref e92bd8573c0296853faa6de7801114874ac2455d`): it replaces the old install and prompts for any environment variables you have not set. `hermes plugins update` refuses an install pinned with `--ref`, and it never prompts.
+Upgrading from an earlier release? Add `--force` (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref cedb3a9dee16ec7a8afc80c55374851b471d8230`): it replaces the old install and prompts for any environment variables you have not set. `hermes plugins update` refuses an install pinned with `--ref`, and it never prompts.
 
 <Note>
-Plugin v0.8.0 runs on Robinhood Chain (4663). It composes on top of whichever CLI is installed, so install the CLI first. It does not read the CLI's config. For the gas watchdog, set `SHERWOOD_AGENT_ADDRESS` (the monitor wallet's address) in the Hermes profile `.env`. `SHERWOOD_PRIVATE_KEY` is only for XMTP, which works only for funds whose XMTP group id is in their ENS record on Base; Robinhood Chain funds get nothing from it, so leave it unset. If you do set it, use a dedicated key that holds nothing, never the key that manages your funds. For the group-chat fund (Telegram), see the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
+Plugin v0.8.1 runs on Robinhood Chain (4663). It composes on top of whichever CLI is installed, so install the CLI first. It does not read the CLI's config. For the gas watchdog, set `SHERWOOD_AGENT_ADDRESS` (the monitor wallet's address) in the Hermes profile `.env`. `SHERWOOD_PRIVATE_KEY` is only for XMTP, which works only for funds whose XMTP group id is in their ENS record on Base; Robinhood Chain funds get nothing from it, so leave it unset. If you do set it, use a dedicated key that holds nothing, never the key that manages your funds. For the group-chat fund (Telegram), see the plugin's [README](https://github.com/sherwoodagent/sherwood-hermes-plugin#group-chat-fund-telegram).
 </Note>
 
 See the [Cron Jobs](/cli/cron-jobs) page for the autonomous monitoring stack. Skip this section if you're on Claude Code, Codex, or another runtime.
 
 ### Group-chat fund
 
-The plugin can run a fund inside a Telegram group: members pitch trade ideas, the agent drafts a proposal when three people react 🔥 to an idea or the host says "ship it", members back or object, and guest Hermes agents join as co-managers. The factory on Robinhood Chain opened on 2026-11-06 (`ownerOnlyProposals` false, `depositsRestricted` false, `creationFee` 0; [read the flags live](/explanations/launch-posture#reading-the-posture-yourself) before you rely on them). Anyone with an [ERC-8004](/reference/integrations/erc-8004) identity can create a vault, deposits are open or restricted per vault, and any registered agent of a vault can propose. Plugin v0.8.0 supports both postures with two independent settings:
+The plugin can run a fund inside a Telegram group: members pitch trade ideas, the agent drafts a proposal when three people react 🔥 to an idea or the host says "ship it", members back or object, and guest Hermes agents join as co-managers. The factory on Robinhood Chain opened on 2026-11-06 (`ownerOnlyProposals` false, `depositsRestricted` false, `creationFee` 0; [read the flags live](/explanations/launch-posture#reading-the-posture-yourself) before you rely on them). Anyone with an [ERC-8004](/reference/integrations/erc-8004) identity can create a vault, deposits are open or restricted per vault, and any registered agent of a vault can propose. Since v0.8.0 the plugin supports both postures with two independent settings:
 
 - **Deposits, per fund.** Restricted (the default): the host approves each member before they can deposit. Open (`open_deposits`, for a vault created with open deposits): a member taps 🎟 Join, links a wallet and deposits, with no approval step.
 - **Who signs, per Hermes (`proposal_signer`).** `host` (the default): the host's browser wallet owns the vault and signs owner actions on `app.sherwood.sh/tg/sign`. `agent` (open protocol): the agent's own wallet, the one the Sherwood CLI holds, owns the vault. For a draft the host shipped, or a guest agent the host approved, the plugin hands the agent the exact CLI command (`sherwood strategy propose portfolio ...` or `sherwood vault add ...`). The agent runs it with its CLI wallet in a `hermes -z` one-shot the operator starts, or in a cron set to `approvals.cron_mode: approve`, never from a Telegram turn and never on boot. The group's toolset must have no terminal, file or code tools (Hermes' default `hermes-telegram` toolset has them): see the plugin's [Deployment boundary](https://github.com/sherwoodagent/sherwood-hermes-plugin#deployment-boundary-required). These commands need Sherwood CLI 0.103.0 or later. Creating the vault, setting the agent fee and the governor parameters stay CLI steps the operator runs.
diff --git a/guides/set-up-grok.mdx b/guides/set-up-grok.mdx
index dd69d43..a990f05 100644
--- a/guides/set-up-grok.mdx
+++ b/guides/set-up-grok.mdx
@@ -23,7 +23,7 @@ Sherwood is a skill pack plus a CLI on top of the agent you already run. Grok is
     sherwood --version
     ```
 
-    Load the Sherwood skill pack into the runtime so the agent knows the commands and the safety rules: `npx skills install sherwoodagent/skill`. On Hermes, also install the plugin: `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d`.
+    Load the Sherwood skill pack into the runtime so the agent knows the commands and the safety rules: `npx skills install sherwoodagent/skill`. On Hermes, also install the plugin: `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref cedb3a9dee16ec7a8afc80c55374851b471d8230`.
 
     - [CLI installation](/cli/installation)
     - [Cron jobs](/cli/cron-jobs)
````

## 2. sherwoodagent/skill (#123)

This is skill release 0.35.2. `v0.35.1` (#122) is already tagged, and the
release workflow tags only a version that has no tag yet, so the new pin
needs its own release. `.claude-plugin/plugin.json` `version` and `SKILL.md`
`metadata.version` (line 9) move together.

- **`SKILL.md`:**
  - The new sha goes at line 27 (quick start), 1770 (Install block), 1782
    (rebuild block) and 1787 (upgrade line).
  - Line 1773 says "the commit the v0.8.1 tag points at" and links the
    v0.8.1 release page.
  - Line 1744 (the intro) says v0.8.1.
  - The group-chat fund paragraph (1820) is unchanged.

Checks: `scripts/check-skill.sh` passed (skill version 0.35.2, CLI pin
0.104.0).

The exact diff (#123, head `678600e`, against `main` `fef468c`):

````diff
diff --git a/.claude-plugin/plugin.json b/.claude-plugin/plugin.json
index 653fe1c..118e897 100644
--- a/.claude-plugin/plugin.json
+++ b/.claude-plugin/plugin.json
@@ -1,6 +1,6 @@
 {
   "name": "sherwood",
-  "version": "0.35.1",
+  "version": "0.35.2",
   "description": "Agent-managed ERC-4626 investment vaults on Robinhood Chain (4663) \u2014 deploy vaults, run tokenized-stock, lending and liquidity strategies, and manage vault operations via CLI or HTTP API",
   "author": {
     "name": "Sherwood",
diff --git a/SKILL.md b/SKILL.md
index e6e2017..ca544e4 100644
--- a/SKILL.md
+++ b/SKILL.md
@@ -6,7 +6,7 @@ license: MIT
 compatibility: Requires Node.js 22+, the @sherwoodagent/cli npm package, network access to Robinhood Chain (4663) RPC and https://api.sherwood.sh
 metadata:
   author: sherwood
-  version: '0.35.1'
+  version: '0.35.2'
 ---
 
 # Sherwood
@@ -24,7 +24,7 @@ If it does exist, run `sherwood --version`. It must be **0.96.0 or later**; upda
 
 Requires Node.js v22+ (including Node 24). XMTP chat runs on `@xmtp/node-sdk`, whose native bindings can fail on older glibc hosts (see [Running on Hermes Agent](#running-on-hermes-agent) for the symptom).
 
-**Running on Hermes Agent?** After installing the CLI, also install the companion plugin — `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d` (the v0.8.0 release commit; once the plugin is in the Hermes catalog, `hermes plugins install sherwood-monitor`) — which adds always-on event streaming, cron digests and risk guardrails on top of the CLI, and can run a fund inside a Telegram group. Full details in [Running on Hermes Agent](#running-on-hermes-agent) below. Skip if you're on Claude Code, Codex, or another runtime.
+**Running on Hermes Agent?** After installing the CLI, also install the companion plugin — `hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref cedb3a9dee16ec7a8afc80c55374851b471d8230` (the v0.8.1 release commit; once the plugin is in the Hermes catalog, `hermes plugins install sherwood-monitor`) — which adds always-on event streaming, cron digests and risk guardrails on top of the CLI, and can run a fund inside a Telegram group. Full details in [Running on Hermes Agent](#running-on-hermes-agent) below. Skip if you're on Claude Code, Codex, or another runtime.
 
 **HTTP API (no CLI install).** Live base: `https://api.sherwood.sh` with root paths (`/chains`, `/prepare/identity-mint`, `/vaults/:address`). That host is already v1 — do **not** add a `/v1` prefix (`https://api.sherwood.sh/v1/...` 404s). `https://www.sherwood.sh/api/v1` also 404s. Catalog: `GET https://api.sherwood.sh/`. See [references/external-signer-integration.md](references/external-signer-integration.md).
 
@@ -1741,7 +1741,7 @@ One line each; the linked section has the detail.
 
 ## Running on Hermes Agent
 
-If you (the agent) are running on the [Hermes agent runtime](https://github.com/NousResearch/hermes-agent), there is a dedicated plugin — **`sherwood-monitor`** — that gives you always-on reactive awareness of your vaults beyond what the CLI alone provides. This is a **separate install** from the skill pack and CLI above. Skip this section if you're on Claude Code, Codex, or another runtime. The current plugin release (v0.8.0) runs on Robinhood Chain (4663). Its risk guardrails are advisory (they fail open when vault state is unavailable), so rely on the CLI's own preflights. XMTP streaming and posting work only for vaults whose XMTP group id is in their ENS record on Base; Robinhood Chain vaults have no such record, so for them the plugin streams on-chain events only.
+If you (the agent) are running on the [Hermes agent runtime](https://github.com/NousResearch/hermes-agent), there is a dedicated plugin — **`sherwood-monitor`** — that gives you always-on reactive awareness of your vaults beyond what the CLI alone provides. This is a **separate install** from the skill pack and CLI above. Skip this section if you're on Claude Code, Codex, or another runtime. The current plugin release (v0.8.1) runs on Robinhood Chain (4663). Its risk guardrails are advisory (they fail open when vault state is unavailable), so rely on the CLI's own preflights. XMTP streaming and posting work only for vaults whose XMTP group id is in their ENS record on Base; Robinhood Chain vaults have no such record, so for them the plugin streams on-chain events only.
 
 ### What the plugin adds
 
@@ -1767,10 +1767,10 @@ command -v hermes && hermes plugins list | grep -q sherwood-monitor && echo "ins
 ### Install
 
 ```bash
-hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d
+hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref cedb3a9dee16ec7a8afc80c55374851b471d8230
 ```
 
-Hermes has no `@tag` install syntax: `--ref` takes the full commit sha, here the commit the v0.8.0 tag points at (also on the [v0.8.0 release page](https://github.com/sherwoodagent/sherwood-hermes-plugin/releases/tag/v0.8.0)). Once the plugin is in the Hermes catalog, `hermes plugins install sherwood-monitor` installs the reviewed pin.
+Hermes has no `@tag` install syntax: `--ref` takes the full commit sha, here the commit the v0.8.1 tag points at (also on the [v0.8.1 release page](https://github.com/sherwoodagent/sherwood-hermes-plugin/releases/tag/v0.8.1)). Once the plugin is in the Hermes catalog, `hermes plugins install sherwood-monitor` installs the reviewed pin.
 
 Requirements: Python ≥ 3.11, **Node ≥ 22 and npm** (for the bundled sidecar build), and a configured Sherwood CLI (the monitor runs it; the CLI reads its own config). The plugin never reads `~/.sherwood/config.json`: for XMTP and the gas watchdog, set the monitor wallet in the Hermes profile `.env` (`SHERWOOD_PRIVATE_KEY`, optional, the sidecar derives its own key from it; or `SHERWOOD_AGENT_ADDRESS` alone for the gas watchdog). The install runs `npm ci && npm run build` inside the sidecar directory (~30s, one-time).
 
@@ -1779,12 +1779,12 @@ The plugin runs a preflight on load. If it doesn't find `sherwood --version`, or
 If the install fails mid-sidecar (no Node, npm offline, etc.), everything except XMTP still works. Rebuild later with:
 
 ```bash
-SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref e92bd8573c0296853faa6de7801114874ac2455d
+SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref cedb3a9dee16ec7a8afc80c55374851b471d8230
 cd "$(python3 -c 'import sherwood_monitor, pathlib; print(pathlib.Path(sherwood_monitor.__file__).parent.parent / "xmtp_sidecar")')"
 npm ci && npm run build
 ```
 
-To upgrade from an earlier release, add `--force` (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref e92bd8573c0296853faa6de7801114874ac2455d`): it replaces the old install and prompts for any environment variables you have not set. `hermes plugins update` refuses an install pinned with `--ref`, and it never prompts.
+To upgrade from an earlier release, add `--force` (`hermes plugins install sherwoodagent/sherwood-hermes-plugin --force --ref cedb3a9dee16ec7a8afc80c55374851b471d8230`): it replaces the old install and prompts for any environment variables you have not set. `hermes plugins update` refuses an install pinned with `--ref`, and it never prompts.
 
 ### One-time onboarding per vault
 
````

## 3. sherwoodagent/sherwood (monorepo, #744, draft)

Already on the branch (`chore/hermes-plugin-0.8.0-submodules`):

- **`hermes-plugin` pointer:** `b373218` (v0.7.0) → `e92bd85` (v0.8.0) →
  `cedb3a9` (v0.8.1).
- **`CLAUDE.md` "Hermes plugin bump":** consumer pins use
  `--ref <SHA>`, and the release workflow tags on merge. The rows are
  sha-generic, so 0.8.1 needs no edit.

The `mintlify-docs` and `skill` pointers stay at their old commits for now.
Those mains pin plugin 0.8.0, and moving the pointers there would pair 0.8.0
docs with the 0.8.1 plugin. After #139 and #123 merge, add the two pointers as
signed commits and take the PR out of draft:

```bash
git submodule update --init mintlify-docs skill
git -C mintlify-docs fetch origin && git -C mintlify-docs checkout origin/main
git -C skill fetch origin && git -C skill checkout origin/main
git add mintlify-docs skill
git commit -S -m "chore: bump mintlify-docs and skill submodules (Hermes plugin 0.8.1)"
git log --format='%h %G?' origin/main..HEAD   # G on every commit
```

## 4. Back in this repo

- **Refresh the bundled skill pack after skill #123 merges (skill 0.35.2).**
  At v0.8.1, the bundled `skills/sherwood-agent/` is still skill 0.34.3 (it
  was never refreshed to 0.35.1). It carries the 0.7.0 pin and the 11-tool
  fund list. Refresh it in its own PR:

  ```bash
  ./scripts/refresh_skill_pack.sh ../skill
  ```

  Do not hand-edit `skills/sherwood-agent/`: it is a copy of the skill repo.
- **Release notes.** The v0.8.1 release notes do not carry the install sha
  yet. Use `docs/catalog-submission.md` step 2. That step is part of the
  release and is held for the maintainer.
- **The catalog reviews the pinned tree.** This PR's fixes to
  `docs/catalog-submission.md` (0.8.0 → 0.8.1), `docs/hackathon-disclosure.md`
  (title, and the Telegram `/fund` read trigger) and `docs/fund-host-join.md` (the
  same read trigger) land after the v0.8.1 tag. So the tree at `cedb3a9`
  still says v0.8.0 in the disclosure title, and it still says the
  open-deposits read happens only during a live proposal. Whether that
  warrants a 0.8.2 before the catalog PR is the maintainer's call.
