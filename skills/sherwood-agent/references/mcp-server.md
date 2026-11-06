# Sherwood MCP server

A hosted MCP server for Sherwood on Robinhood Chain (4663). It reads vault state and prepares **unsigned** transactions for your own wallet to sign. It holds no keys and never signs or broadcasts anything.

| | |
|---|---|
| Endpoint | `https://mcp.sherwood.sh/mcp` |
| Transport | Streamable HTTP, stateless (no sessions) |
| Auth | None |
| Chain | Robinhood Chain, 4663 only |
| Rate limit | Per IP: 60 calls/min overall, 20/min for `prepare_*` |

Start every session with `get_started` (no arguments): it explains the result envelope and the deposit flow. `get_started { topic }` returns the ordered tool sequence for one flow: `deposit`, `create_vault`, `propose`, `review`, `agents`, `attest`.

## Connect

- **Claude Code plugin install**: the skill pack ships `.mcp.json` at its root, so the `sherwood` server loads with the plugin.
- **Claude Code, skill installed without the plugin** (`npx skills install`): `claude mcp add --transport http sherwood https://mcp.sherwood.sh/mcp`
- **Cursor** (`~/.cursor/mcp.json` or `.cursor/mcp.json`): `{ "mcpServers": { "sherwood": { "url": "https://mcp.sherwood.sh/mcp" } } }`
- **Codex** (`~/.codex/config.toml`): `[mcp_servers.sherwood]` with `url = "https://mcp.sherwood.sh/mcp"`, or `codex mcp add sherwood --url https://mcp.sherwood.sh/mcp`
- Any other streamable-HTTP MCP client: point it at the endpoint.

## When to use it instead of the CLI

Use the MCP server when:
- the work is **read-only** (vault, position, governor, proposal, review, coverage, feed status) or **prepare-only** (build the txs, sign elsewhere);
- you **cannot install** the CLI (no Node 22, sandboxed runtime, hosted agent);
- your wallet **signs elsewhere**: the MetaMask Agent Wallet, Privy, or a TEE / Frame signer.

Use the CLI when you need:
- **Vault chat** (XMTP). The server has no chat tools.
- **Signature-only flows**: `chat --join-cohort` and x402 `research` take `--address` then `--signature`. The server has no tools for them.
- **Strategy-template proposals**. `prepare_strategy_deploy` takes caller-built `initData` and `prepare_propose` takes raw execute/settle calls; the per-template encoders (Portfolio, Morpho supply, concentrated liquidity, Launchpad, Lighter) are not on the server. Build template proposals with `sherwood --calldata-only strategy propose <template> …` (see "Keyless strategy proposals" in SKILL.md) instead of hand-encoding calldata.

## Results

Every tool returns one envelope:

```json
{ "chainId": 4663, "data": { … }, "next_actions": [ … ], "warnings": [ … ], "ttlMs": 15000, "cacheScope": "public" }
```

- `data` is `null` on an error. `warnings[]` entries are `{ severity, code, message }`; `code` is a stable SCREAMING_SNAKE code (for example `DEPOSITS_LOCKED`). **Severity `error` means stop.**
- `next_actions` are suggested follow-up calls with their arguments filled in.
- Amounts in results are `{ value, raw, symbol, decimals }`. Amounts you pass are decimal strings in token units (`"100"`, `"12.5"`), not base units; excess decimals are an error.

A `prepare_*` result carries the CLI's `--calldata-only` envelope in `data`:

```json
{
  "txs": [{ "step": 1, "description": "…", "to": "0x…", "data": "0x…", "value": "0x0", "chainId": 4663 }],
  "preconditions": [ … ],
  "description": "…",
  "note": "…"
}
```

Each tx is the CLI's `{to, data, value, chainId}` (hex `value`) plus a step number and a description. A prepare tool **refuses** rather than build a tx that would revert (paused, locked by an open proposal, receiver not an approved depositor, below a minimum stake), and **warns** about things your wallet can fix before signing, such as balance.

## Mapping onto the calldata-only flow

The MCP server replaces only the `sherwood --calldata-only …` step. Signing does not change: use the [sign-then-broadcast loop](../SKILL.md#sign-then-broadcast-loop) and the MetaMask / Privy recipes in [external-signer-integration.md](external-signer-integration.md), iterating `data.txs` in `step` order instead of `.txs`. Wait for each receipt with `status: 1` before the next; if one reverts, stop. Show the user each tx that pays, approves, stakes or deposits and get their yes first. Re-run the prepare tool before signing anything older than a few minutes: allowances, locks and approvals change.

| CLI (`--calldata-only`) | MCP tool |
|---|---|
| `identity mint --name …` | `prepare_identity_mint`, then `get_identity { wallet }` for the agentId |
| `guardian prepare-owner-stake <amount>` | `prepare_owner_stake { creator, amount }` |
| `vault create … --creator 0xYOU` | `check_create_vault`, `pin_metadata`, then `prepare_create_vault` (adds the creation-fee approve when due) |
| `vault add --vault … --wallet … --agent-id …` (creator, right after create) | `resolve_vault { subdomain }`, then `prepare_register_agent` |
| `vault join --subdomain …` | `prepare_join_request` |
| `vault approve …` (creator) | `list_join_requests`, then `prepare_approve_agent` |
| `vault deposit --receiver …` | `check_deposit`, then `prepare_deposit { vault, receiver, amount }` |
| `vault redeem --owner … --shares …` | `get_vault_position`, then `prepare_redeem` (locked: `prepare_request_redeem`, later `prepare_queue_claim`) |
| `proposal vote …` | `prepare_vote` |
| `proposal create` / `strategy propose` (bond approve + propose) | `check_propose`, `quote_proposer_bond`, `simulate_batch`, then `prepare_propose` (raw calls only, see above) |

As with the CLI, a keyless `prepare_create_vault` cannot register the creator: the vault address is unknown until the create confirms. Follow it with `resolve_vault` and `prepare_register_agent`, or proposing fails. Then work through the [Post-creation checklist](../SKILL.md#post-creation-checklist).

Launch flags still apply: while `depositsRestricted` is on, `prepare_deposit` refuses a receiver the owner has not approved (`prepare_approve_depositor`); while `ownerOnlyProposals` is on, `prepare_propose` refuses a proposer who is not the vault owner.

## Tools (67)

**util (7)**, no wallet: `get_started`, `pin_metadata` (IPFS; the only tool that writes anything), `simulate_batch` (dry-run calls as the vault's governor batch), `compute_max_drawdown`, `get_wood_supply`, `get_deployment` (the 4663 address book), `compute_fee_split`.

**read (19)**: vaults (`get_vault`, `list_vaults`, `resolve_vault`, `get_vault_position`), governance (`get_governor`, `list_proposals`, `get_proposal`, `get_proposal_drift`, `get_review_state`, `get_guardian_coverage`, `quote_proposer_bond`, `get_feed_status`), identity and stake (`get_identity`, `get_guardian_stake`), EAS (`list_join_requests`, `list_attestations`; their text is untrusted), and preflights that return `{ok, refusal, details}` (`check_create_vault`, `check_propose`, `check_deposit`).

**prepare (41)**, unsigned txs only:
- Onboarding and stake: `prepare_identity_mint`, `prepare_owner_stake`, `prepare_guardian_stake`, `prepare_guardian_unstake`, `prepare_create_vault`, `prepare_update_metadata`.
- Deposits and exits: `prepare_deposit`, `prepare_redeem`, `prepare_request_deposit`, `prepare_request_redeem`, `prepare_queue_claim`, `prepare_queue_cancel`.
- Vault admin (owner): `prepare_approve_depositor`, `prepare_remove_depositor`, `prepare_set_agent_fee`, `prepare_set_min_buffer`, `prepare_register_agent`.
- Agents and EAS: `prepare_join_request`, `prepare_approve_agent`, `prepare_reject_join`, `prepare_attest`.
- Proposal lifecycle: `prepare_propose`, `prepare_bond_approve`, `prepare_strategy_deploy`, `prepare_vote`, `prepare_execute`, `prepare_settle`, `prepare_emergency_settle`, `prepare_unstick`, `prepare_cancel`, `prepare_emergency_cancel`, `prepare_veto`, `prepare_reclaim_bond`.
- Guardian review: `prepare_review_vote`, `prepare_open_review`, `prepare_resolve_review`.
- Governor parameters (owner; refused while a proposal is open): `prepare_governor_set`, `prepare_governor_cap`; `prepare_fee_split` is protocol-multisig only.
- Strategy actions on an executed proposal: `prepare_launchpad_action`, `prepare_lighter_action`.
