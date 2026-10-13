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

## Chain: Robinhood Chain mainnet (4663)

Sherwood v1 is **live on Robinhood Chain mainnet (4663)**: run 1 on 2026-10-06, run 2 (the economic-security stack, which opened vault creation) on 2026-10-13. `SKILL.md` and the sub-skills are written for it. Never publish a Sherwood mainnet address that is not in the protocol's `chains/4663.json` (`v1-deploy` branch); if a contract is ever missing from that book, write "not in the book" and resolve it live from the governor. Every parameter value must trace to `v1-deploy` source, not to the protocol's narrative docs, which drift.

The Robinhood mainnet fork (a Tenderly vnet, chain 9994663) hosted the beta, which has ended. Do not present it as a chain or add instructions for it; "measured on a fork of this chain" is the only acceptable mention. Robinhood testnet (46630) runs pre-v1 contracts the CLI cannot talk to — do not document it.

`sherwood.sh/skill.md` serves `SKILL.md` from `main` verbatim once the skill files are public, so a merge to `main` is a publish: no "beta ended", "coming soon" or "test capital" wording, and no half-finished sections.

Do not re-add Base / Base Sepolia / HyperEVM / hyperevm-testnet as deployment targets without an explicit decision in the PR. Frame it as "Sherwood deploys on Robinhood chains", never "Base/HyperEVM support removed". Base appears only as the chain x402 research payments settle on (USDC on Base).

Wallets: the MetaMask Agent Wallet is the first choice (when `mm chains list` shows 4663), Privy the second; both are sign-only (`--calldata-only`), and Privy stays fully documented as an alternative, not a footnote. Every command and flag in `SKILL.md` must exist in the pinned CLI version: check `cli/src` in `sherwoodagent/sherwood` before documenting one.
