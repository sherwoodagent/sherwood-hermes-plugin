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
| `SKILL.md` Reference → Key flags table | `--chain <network>` enum | Must match CLI's `network.ts` registry |

Sub-skills under `skills/*/SKILL.md` carry their OWN `version` frontmatter — bump those independently when their behavior changes.

## When to bump the CLI pin in this repo

After a CLI release lands on `main` of `sherwoodagent/sherwood` and is published to npm. Pull the version from `cli/package.json` in that repo. Do NOT track `@latest` — agents need a deterministic install for reproducibility.

## When to bump the Hermes plugin pin in this repo

After a release lands on `main` of `sherwoodagent/sherwood-hermes-plugin` and a git tag (e.g. `v0.5.0`) exists. Pull the version from `plugin.yaml` in that repo.

## Don't drift from `cli/src/lib/network.ts`

The `--chain` enum in `SKILL.md` (Reference → Key flags table) MUST match the live CLI's `CHAIN_REGISTRY`. If the CLI adds a chain or renames one, update this skill in the same release window — agents fail closed if they emit an unknown `--chain` value.

## Don't reintroduce testnet

Beta is mainnet-only on Base + HyperEVM. Robinhood L2 (chain 46630) is the sole "testnet" exception in beta — keep the existing Robinhood L2 Testnet section in `ADDRESSES.md`. Do not re-add Base Sepolia / hyperevm-testnet examples without an explicit decision in the PR.
