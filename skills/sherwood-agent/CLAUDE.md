# CLAUDE.md — Sherwood Skill

This file is for any agent (Claude Code, Codex, etc.) that opens a PR against this repo.

## What this repo is

The Sherwood agent skill pack — installed by users via `npx skills install sherwoodagent/skill` (Claude Code) or via the bundled hermes-plugin's skill bundle. Source of truth for the agent-facing behavior of every Sherwood CLI command.

## Version-bump checklist (REQUIRED on every release-bumping PR)

When bumping any of:
- `.claude-plugin/plugin.json` `version` field (this skill's own version)
- The pinned Hermes plugin version (`sherwoodagent/sherwood-hermes-plugin@vX.Y.Z` in `SKILL.md`)

…the PR MUST also touch every spot the version is referenced. Today those are:

| File | Pin | What it controls |
|---|---|---|
| `.claude-plugin/plugin.json` | `version` | Skill plugin manifest |
| `SKILL.md` "Running on Hermes Agent" → Install | `sherwood-hermes-plugin@vX.Y.Z` | Hermes plugin install command |
| `SKILL.md` `metadata.version` | same as `plugin.json` | `scripts/check-skill.sh` fails if they differ; the release tag is `v<version>` |

Sub-skills under `skills/*/SKILL.md` carry their OWN `version` frontmatter — bump those independently when their behavior changes.

## CLI version

The skill pins the exact CLI release it was checked against (`npm i -g @sherwoodagent/cli@X.Y.Z`, SHE-331), never `@latest`: a bad or hijacked npm publish must not reach every agent that follows the skill. Every install command names an exact version (`@metamask/agent-wallet@X.Y.Z`, `--package=@privy-io/agent-wallet-cli@X.Y.Z`, `pip install lighter-sdk==X.Y.Z`); `scripts/check-skill.sh` enforces it.

- **Bumping the CLI pin.** After a CLI release the skill should use (a flag it documents, a fix agents need), run `scripts/bump-cli-pin.sh X.Y.Z`. It refuses a version npm does not have and moves every pin at once, including the exact-command entry in `allowed-tools`. Then bump the skill version and release (below). A CLI release that the skill does not depend on needs no bump.
- **Minimum line.** Keep one floor ("0.96.0 or later") in `SKILL.md` Install and in each sub-skill's `compatibility`; raise it only when the skill depends on a command or flag older releases lack. The floor is what an existing install must meet; the pin is what a fresh install gets.
- Every command and flag in the skill must exist in the **pinned** release.

## allowed-tools

`allowed-tools` pre-approves commands for the turn a skill is invoked; it does not restrict anything. Keep it to commands that cannot fetch, install, read secrets or sign on their own: `Bash(sherwood *)`, read-only `cast` subcommands (`cast call *`, `cast calldata *`, `cast chain-id *`, `cast 4byte*`), `Bash(which sherwood)`, `Bash(mm chains list)`, and the **exact** install commands (`Bash(npm i -g @sherwoodagent/cli@X.Y.Z)`). Never a blanket `Bash(curl:*)`, `cat`, `npm`, `npx`, `pnpm`, `pip`, `forge`, `git`, `mm` or `cast`: `cast send`, wallet signing and downloads must prompt the user. `scripts/check-skill.sh` fails on a blanket entry.

## Releases

Publishing is a tag, not a merge. `.github/workflows/release.yml` runs on every push to `main`: when `.claude-plugin/plugin.json` carries a version with no `v<version>` tag, it runs `scripts/check-skill.sh`, tags the merged commit, and publishes a GitHub release holding `SKILL.md`, `skill-guardian.md`, `skill-launchpad.md`, `skill-vault-owner.md` (the files sherwood.sh serves), `SKILL.md.sha256`, `SHA256SUMS` and a build-provenance attestation per file (`gh attestation verify SKILL.md --repo sherwoodagent/skill`).

1. Content PRs merge to `main` as usual; CI (`check`) must be green.
2. To release: bump `.claude-plugin/plugin.json` `version` and `SKILL.md` `metadata.version` together (and any sub-skill `version` whose behavior changed), merge. The workflow creates `vX.Y.Z`.
3. sherwood.sh serves the release assets (SHE-331 app change), so content merged without a version bump is not live until the next release. Batch content into one release per day or per CLI pin bump rather than one per PR.
4. Never move or delete a `v*` tag: published sha256 values and attestations point at it.

The guardian sub-skills verify the address book against the GuardianRegistry pinned in the skill (`PINNED_GUARDIAN_REGISTRY` in `skills/guardian/SKILL.md` and `skills/network-guardian/SKILL.md`). Change that pin only with a GuardianRegistry redeployment, in a release of its own.

## When to bump the Hermes plugin pin in this repo

After a release lands on `main` of `sherwoodagent/sherwood-hermes-plugin` and a git tag (e.g. `v0.5.0`) exists. Pull the version from `plugin.yaml` in that repo.

## Chain: Robinhood Chain mainnet (4663)

Sherwood v1 is **live on Robinhood Chain mainnet (4663)**: run 1 on 2026-10-06, run 2 (the economic-security stack, which opened vault creation) on 2026-10-13. `SKILL.md` and the sub-skills are written for it. Never publish a Sherwood mainnet address that is not in the protocol's `chains/4663.json` (`v1-deploy` branch); if a contract is ever missing from that book, write "not in the book" and resolve it live from the governor. Every parameter value must trace to `v1-deploy` source, not to the protocol's narrative docs, which drift.

The Robinhood mainnet fork (a Tenderly vnet, chain 9994663) hosted the beta, which has ended. Do not present it as a chain or add instructions for it; "measured on a fork of this chain" is the only acceptable mention. Robinhood testnet (46630) runs pre-v1 contracts the CLI cannot talk to — do not document it.

`sherwood.sh/skill.md` serves `SKILL.md` verbatim: from `main` until the app switches to release assets (SHE-331), from the latest `v*` release after. Either way treat `main` as publishable: no "beta ended", "coming soon" or "test capital" wording, and no half-finished sections.

Do not re-add Base / Base Sepolia / HyperEVM / hyperevm-testnet as deployment targets without an explicit decision in the PR. Frame it as "Sherwood deploys on Robinhood chains", never "Base/HyperEVM support removed". Base appears only as the chain x402 research payments settle on (USDC on Base).

Wallets: the MetaMask Agent Wallet is the first choice (when `mm chains list` shows 4663), Privy the second; both are sign-only (`--calldata-only`), and Privy stays fully documented as an alternative, not a footnote. Every command and flag in `SKILL.md` must exist in the pinned CLI release: check `cli/src` at that release's tag (`vX.Y.Z`) in `sherwoodagent/sherwood` before documenting one.
