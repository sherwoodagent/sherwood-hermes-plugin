# CLAUDE.md — Sherwood Skill

This file is for any agent (Claude Code, Codex, etc.) that opens a PR against this repo.

## What this repo is

The Sherwood agent skill pack — installed by users via `npx skills install sherwoodagent/skill` (Claude Code) or via the bundled hermes-plugin's skill bundle. Source of truth for the agent-facing behavior of every Sherwood CLI command.

## Version-bump checklist (REQUIRED on every release-bumping PR)

When bumping any of:
- `.claude-plugin/plugin.json` `version` field (this skill's own version)
- The pinned Sherwood CLI version (`@sherwoodagent/cli@X.Y.Z` in `SKILL.md`)
- The pinned Hermes plugin version (`sherwoodagent/sherwood-hermes-plugin@vX.Y.Z` in `SKILL.md`)

…the PR MUST also touch every spot the version is referenced. Today those are:

| File | Pin | What it controls |
|---|---|---|
| `.claude-plugin/plugin.json` | `version` | Skill plugin manifest |
| `SKILL.md` install section (Option A) | `@sherwoodagent/cli@X.Y.Z` | npm install command for the CLI |
| `SKILL.md` "Running on Hermes Agent" → Install | `sherwood-hermes-plugin@vX.Y.Z` | Hermes plugin install command |

Sub-skills under `skills/*/SKILL.md` carry their OWN `version` frontmatter — bump those independently when their behavior changes.

## When to bump the CLI pin in this repo

After a CLI release lands on `main` of `sherwoodagent/sherwood` and is published to npm. Pull the version from `cli/package.json` in that repo. Do NOT track `@latest` — agents need a deterministic install for reproducibility.

## When to bump the Hermes plugin pin in this repo

After a release lands on `main` of `sherwoodagent/sherwood-hermes-plugin` and a git tag (e.g. `v0.5.0`) exists. Pull the version from `plugin.yaml` in that repo.

## Single-chain: Robinhood testnet (chain 46630)

Sherwood currently deploys on **Robinhood testnet (chain 46630) only** — the initial deployment target, not a permanent single-chain product (multi-chain expected later). The CLI targets it by default and exposes **no `--chain` flag and no `ENABLE_TESTNET`**. Keep `SKILL.md` / `ADDRESSES.md` presenting exactly this one chain. Do not re-add Base / Base Sepolia / HyperEVM / hyperevm-testnet / Robinhood mainnet (4663) as deployment targets, or reintroduce a `--chain` flag, without an explicit decision in the PR. Frame it as "currently deploys on Robinhood testnet", never "Base/HyperEVM support removed".
