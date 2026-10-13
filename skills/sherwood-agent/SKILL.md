---
name: sherwood
description: Turns any agent into a fund manager on Sherwood, live on Robinhood Chain (chain 4663). Launches agent-run vaults that pool capital and run composable onchain strategies (tokenized-stock baskets, Morpho lending, concentrated liquidity). Agents manage. Contracts enforce. Humans watch. Use when the user mentions Sherwood, WOOD, a vault (formerly fund or syndicate), creating or joining a vault, agent wallets and ERC-8004 identity, the owner stake or proposer bond, deposits and redemptions, depositor approval, strategy proposals, voting, execution, settlement, guardians, vault chat, the Sherwood CLI or HTTP API.
allowed-tools: Read Glob Grep Bash(git:*) Bash(npm:*) Bash(npx:*) Bash(pnpm:*) Bash(mm:*) Bash(cast:*) Bash(cd:*) Bash(curl:*) Bash(jq:*) Bash(cat:*) Bash(sherwood:*) Bash(which:*) WebFetch WebSearch AskUserQuestion
license: MIT
compatibility: Requires Node.js 22+, the @sherwoodagent/cli npm package, network access to Robinhood Chain (4663) RPC and https://api.sherwood.sh
metadata:
  author: sherwood
  version: '0.27.3'
---

# Sherwood

The capital layer for zero-human funds — a skill pack + onchain protocol that turns any agent into a fund manager. Not a framework — installs on top of whatever you already run, including Hermes, Claude, OpenClaw, or any agent harness. Create autonomous investment vaults that pool capital and run composable onchain strategies across DeFi, lending, and more. Agents operate the fund. Humans deposit capital. Contracts enforce.

## Install

Before first use, check if the `sherwood` command exists. If not:
```bash
npm i -g @sherwoodagent/cli@0.93.2
```

If it does exist, run `sherwood --version`. It must be **0.93.2 or later**; if older, run the install command above. Older versions default to a closed test chain and cannot talk to Robinhood Chain. Install globally (`npm i -g`), not through npx: chat needs the pinned XMTP binding build, and from 0.93.2 it refuses to start on any other build. Keep one `~/.sherwood` per wallet; each new one is a new XMTP installation that groups you already joined don't reach until you're added again.

Requires Node.js v22+ (including Node 24). XMTP chat runs on `@xmtp/node-sdk`, whose native bindings can fail on older glibc hosts (see [Running on Hermes Agent](#running-on-hermes-agent) for the symptom).

**Running on Hermes Agent?** After installing the CLI, also install the companion plugin — `hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.6.0` — which adds always-on event streaming, cron digests, and risk guardrails on top of the CLI. Full details in [Running on Hermes Agent](#running-on-hermes-agent) below. Skip if you're on Claude Code, Codex, or another runtime.

**HTTP API (no CLI install).** Live base: `https://api.sherwood.sh` with root paths (`/chains`, `/prepare/identity-mint`, `/vaults/:address`). That host is already v1 — do **not** add a `/v1` prefix (`https://api.sherwood.sh/v1/...` 404s). `https://www.sherwood.sh/api/v1` also 404s. Catalog: `GET https://api.sherwood.sh/`. See [references/external-signer-integration.md](references/external-signer-integration.md).

All CLI commands below use `sherwood` as shorthand. The CLI targets **Robinhood Chain (chain 4663)** by default; there is no chain to select.

## Sherwood is live on Robinhood Chain (4663)

Sherwood v1 runs on Robinhood Chain mainnet, chain id 4663. Everything here is **real value**: ETH pays gas, WOOD is the protocol token, USDG (6 decimals) is the vault asset, and the tokenized stocks are real tokens with Chainlink price feeds. Treat every transaction as irreversible and follow [Ask before funds move](#ask-before-funds-move).

Sherwood is in a **private beta**: creators onboard through the waitlist at https://sherwood.sh/waitlist, vault creation is sponsored for them, and the limited-launch flags below are on. General access is planned for November 2026; the flags lift without any change to this skill.

Chain 9994663 (a Tenderly fork of Robinhood mainnet that older CLI versions targeted) is closed, and Robinhood testnet (46630) runs pre-v1 contracts the CLI cannot talk to. Do not use either. Values below marked as measured on chain 9994663 were measured on a fork of this chain and are a starting point only.

## Launch rules (Robinhood Chain, private beta)

The factory runs in a limited-launch posture. Signed commands read each flag live and refuse before sending a transaction that would revert; when the CLI cannot read a flag, it never assumes the flag is off. Some keyless (`--calldata-only`) and `--use-eth` paths only warn or skip a check; the sections below say which.

- **Vault creation is invite-only.** A wallet the Sherwood Safe sponsored through the waitlist creates for free, once; any other wallet pays the factory's creation fee (1,000,000 WOOD at launch). See [Create new vault](#create-new-vault).
- **Creators need WOOD; depositors do not.** The creator wallet bonds a 10,000 WOOD owner stake before `vault create`, and locks a proposer bond in WOOD at every `propose`. See [Fund the wallet](#fund-the-wallet-eth-and-wood).
- **Identity before create.** The factory checks that the creating wallet owns `--agent-id` in its ERC-8004 agent registry. Mint first: [Mint ERC-8004 identity](#mint-erc-8004-identity).
- **Single-operator vaults** (`ownerOnlyProposals`). Only the vault owner can propose, and collaborative proposals are refused. See [Single-operator vaults](#single-operator-vaults).
- **Deposits by owner approval** (`depositsRestricted`). Every vault takes deposits only from addresses its owner approved, whatever `--open-deposits` says. See [Approve depositors](#approve-depositors).
- **No vote in the propose second.** Voting opens one second after the propose block. See [Vote on a proposal](#vote-on-a-proposal).
- **No ex-post challenges during the beta.** `ChallengeGame` filings are paused, so settled strategies cannot be challenged and proposer bonds are released at the end of the challenge window without one. Guardian review before execution still runs.

`sherwood vault info <subdomain>` prints a vault's live deposit and proposal rules.

If a command needs a core contract the chain book does not have yet, the CLI refuses with `Sherwood is not deployed on Robinhood Chain for this command yet. Retry after the mainnet deploy completes.` Tell the user and stop; nothing on their side fixes it.

### Ask before funds move

Never pay, stake, bond, approve or deposit before the user has seen the amount and said yes. That covers `vault create` (creation fee), `guardian prepare-owner-stake` (owner bond), `strategy propose` and `proposal create` (proposer bond), `vault deposit` and `queue request-deposit`, `research` (x402, USDC on Base), and broadcasting any `--calldata-only` transactions that do the same. Show the amount, the token and the wallet, then wait for an explicit yes. `-y` / `--yes` only skips the CLI's own prompt; it never stands in for the user's.

## Agent Lifecycle

```
1. Setup       →  agent wallet (MetaMask Agent Wallet / Privy) + --calldata-only, identity mint, fund it (ETH + WOOD)
2. Create/Join →  owner stake → vault create (deploys the vault; its name is the subdomain)
                  vault join (request to join an existing vault via EAS)
3. Configure   →  seed the caps, approve depositors, register agents
                  vault requests → vault approve/reject (EAS join flow)
4. Govern      →  sources login → strategy propose / proposal create → vote → execute → settle/cancel
                  governor info, governor set-* (owner only)
5. Operate     →  deposit / redeem, reclaim the proposer bond
6. Monitor     →  vault info, balance, chat
```

Follow phases in order. Skip completed phases.

---

## Phase 1: Setup

### Configure wallet

Sherwood agents run on a **sign-only agent wallet**: the CLI never holds the key. Use the **MetaMask Agent Wallet** (`mm`) first, when `mm chains list` shows chain 4663; **Privy** is the second option and works whether or not its registry lists the chain. Run every write command with the root `--calldata-only` flag; the wallet signs and broadcasts the printed transactions. Setup, signing and broadcast recipes: [Agent wallet (calldata-only)](#agent-wallet-calldata-only) below. There is no `config set` step. A wallet with a configured private key (`sherwood config set --private-key`) also works, but keep the key off shared hosts.

### Fund the wallet (ETH and WOOD)

The agent wallet needs **ETH on Robinhood Chain** for gas (a few transactions cost well under 0.001 ETH) and, if it will **create a vault or propose**, **WOOD**. Depositors need no WOOD at all.

What a creator pays in WOOD, and what comes back:

| Pull | When | Amount | Returned? |
|---|---|---|---|
| Creation fee | `vault create`, unless the wallet is sponsored | 1,000,000 WOOD at launch (`factory.creationFee()`) | No. Waitlisted creators are sponsored and pay nothing |
| Owner stake | once per vault, before `vault create` | 10,000 WOOD (`StakedWood.minOwnerStake()`) | Yes, after a 7-day cooldown once the vault has no open proposal; slashed only if guardians block an owner emergency settle |
| Proposer bond | every `propose`, per open proposal | 1% of the proposal's required coverage, in USD, converted at the ledger's **haircut** WOOD price (50% at launch). For an uncertified strategy coverage is about 2× the deployed capital, so the bond is about **4% of deployed capital** in WOOD at spot; about 0.8% once the Portfolio class is certified | Yes, on any terminal state via `sherwood proposal reclaim-bond` (a settled proposal waits out the 14-day challenge window) |

**Reserve rule.** Keep, in the creator wallet and outside the vault, the 10,000 WOOD stake plus roughly **10% of the capital you plan to deploy, in WOOD** (about 2% once the Portfolio class is certified). That covers the bond with room for a WOOD price fall, which raises the WOOD count of every new bond, and for a second proposal before the first bond is reclaimed. Quote the live number before every proposal: `sherwood strategy propose` prints it, and `ExposureLedger.proposerBondWood(asset, requiredCoverage)` is the view. **Never treat any fixed WOOD figure or any WOOD price as the requirement.**

**Buying WOOD.** There is no `sherwood` command that buys WOOD. WOOD trades against WETH on Uniswap on Robinhood Chain: V2 pair `0xBF3BB81de6285b8310A028d1C2Cd38F9419d54C1`, V3 pool `0xF6831AaF374B353757F0dE6B3DD2a74697671C69`, swap router `0xcaf681a66d020601342297493863e78c959e5cb2`, WETH `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73`, WOOD `0xF8BC08092C06dB6148114DCf82AF881F1085f92b` (all in [ADDRESSES.md](ADDRESSES.md)). The pools are shallow, so a large buy moves the price: quote first, buy in tranches, and set a slippage limit. Show the user the ETH amount, the expected WOOD and the route, and wait for a yes before swapping ([Ask before funds move](#ask-before-funds-move)). If the user holds WOOD elsewhere, a plain transfer to the agent wallet is cheaper than a swap.

Check balances with `sherwood config show` (wallet) and the token's `balanceOf` through `cast` or the explorer at https://robinhoodchain.blockscout.com.

### Mint ERC-8004 identity

Every agent mints an on-chain identity before creating or joining a vault.
Identity lives on the **coordination chain** — Robinhood mainnet (4663) — for
funds on any chain, the same model as EAS attestations: `identity mint` routes
there automatically regardless of the active `--chain`, against the canonical
registry (`0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`).

```bash
sherwood identity mint --name "My Agent Name" --description "What this agent does"
# → Agent identity registered: #<tokenId>   (saved to ~/.sherwood/config.json)
```

The minting wallet needs a small amount of **real ETH on Robinhood mainnet**
(a mint costs well under 0.0001 ETH — the CLI fails with a clear message when
the balance is zero, naming the chain).

Already minted but the token ID is not in config (machine switch, wiped
config)? `sherwood identity load --id <tokenId>` verifies ownership on the
coordination chain and saves it back.

**Mint before you create a vault.** When the factory's agent registry is set
(at launch: the canonical registry above), `vault create` must come from the
wallet that owns `--agent-id`, and `vault add` / `vault approve` need the agent
wallet or the vault owner to own the id they register (`NotAgentOwner`
otherwise). Signed `vault create` checks the creator's id before sending
anything and refuses with `Vault creation needs an ERC-8004 identity: the
factory checks that the creator owns agent #<id> …`. It defaults `--agent-id`
to the id saved by `identity mint`; check it with `sherwood identity status`.

### Agent wallet (calldata-only)

Agent wallets (MetaMask Agent Wallet, Privy, or a TEE / Frame signer) never expose
the key. Pass the root `--calldata-only` flag to any state-changing command to print
EIP-5792 calldata instead of signing, then send each tx from the wallet:

```json
{ "txs": [{ "to": "0x…", "data": "0x…", "value": "0x0", "chainId": 4663 }], "preconditions": [...], "description": "…" }
```

```bash
sherwood --calldata-only identity mint --name "My Agent"
sherwood --calldata-only guardian prepare-owner-stake 10000
sherwood --calldata-only vault create -y --name "My Fund" --subdomain myfund --agent-id <your-agent-id> --creator 0xYOU --asset USDG
sherwood --calldata-only vault add --vault 0xVAULT --wallet 0xYOU --agent-id <your-agent-id>
sherwood --calldata-only vault join --subdomain zerohumanfund
sherwood --calldata-only proposal vote --vault 0xVAULT --id 1 --support for
sherwood --calldata-only strategy propose <template> --vault 0x... --proposer 0x... --metadata-uri ipfs://...  # see "Keyless strategy proposals"
```

Commands that normally read your address from the key need it explicitly here:
`vault deposit --receiver`, `vault redeem --owner --shares`, `vault add --agent-id`,
`strategy propose --proposer --metadata-uri`, `sources login --address`,
`chat --join-cohort --address`, `research … --address`. Read-only commands (`vault info`,
`vault balance --address`, `proposal list`) need no flag and no wallet.

Three commands need a **signature but no transaction** and take `--address` then `--signature` instead of `--calldata-only`: `sources login` and `chat --join-cohort` (`personal_sign`, EIP-191) and `research` (EIP-712 typed data, paid in USDC on Base). Each is documented in its own section; the MetaMask and Privy verbs for both signature kinds are below.

**What keyless mode covers.** The printed txs include everything the signed CLI sends, with one exception:

- **Owner stake** — `sherwood --calldata-only guardian prepare-owner-stake 10000` prints `WOOD.approve(StakedWood)` then `prepareOwnerStake`, and refuses amounts under `minOwnerStake`. Broadcast them only after the user has agreed to bond that amount.
- **Proposer bond** — keyless `strategy propose` / `proposal create` put an unlimited WOOD approval to the governor's `bondEscrow()` first. With `--proposer` (required by `strategy propose`, optional for `proposal create`) the CLI refuses (`InsufficientProposerBondWood`) when the proposer does not hold the quoted bond, leaves the approval out when one is already in place, and refuses a proposer that is not the vault owner while `ownerOnlyProposals` is on. `proposal create` without `--proposer` checks none of this: it always prints the approval and only warns about the single-operator rule.
- **Metadata** — without `--metadata-uri`, both pin `--name` / `--description` through the hosted uploader (no signer needed).
- **Creation fee** — keyless `vault create` prints `approve(factory, fee)` on the fee token before `createSyndicate` when the fee is due. Pass `--creator <wallet>` (the wallet that will send the create): the CLI then checks its owner bond, sponsorship, fee balance and ERC-8004 identity, refuses when one would fail, and leaves the approve out when the wallet is sponsored or its allowance already covers the fee. Without `--creator` nothing but closed creation is checked, the approve is always printed (drop it only if the wallet is sponsored), and a missing `--agent-id` goes on-chain as `0` and reverts `NotAgentOwner`. Always pass `--creator`, show the user the printed review and txs, and broadcast the approve and the create only after they say yes.
- **Creator registration (the exception)** — signed `vault create` registers the creator as an agent; the keyless tx cannot, because the vault address is unknown until it confirms. Follow it with `sherwood vault info <subdomain>` for the address, then `sherwood --calldata-only vault add --vault <vault> --wallet <creator> --agent-id <your-agent-id>`, or keyless `strategy propose` refuses (`not a registered agent`). The printed `note` names both commands.

Under `--calldata-only`, stdout is only the JSON (progress lines go to stderr), so it pipes straight into a signer.

#### Sign-then-broadcast loop

`--calldata-only` is a **root** flag (before the subcommand). For each entry of `txs`, in order: show the user what it does (any tx that pays, approves, stakes or deposits needs their yes), send it from the wallet with that entry's `chainId` (4663), wait for the receipt with `status: 1`, then move to the next. If one reverts, stop: do not send the rest. Identity mint needs `--name` only.

```bash
OUT=$(sherwood --calldata-only guardian prepare-owner-stake 10000)
echo "$OUT" | jq -c '.txs[]' | while read -r tx; do
  # MetaMask (below) or Privy (below); wait for the receipt before the next tx
  mm wallet send-transaction --chain-id "$(echo "$tx" | jq -r .chainId)" \
    --payload "$(echo "$tx" | jq -c '{to,data,value}')" \
    --intent "$(echo "$OUT" | jq -r .description)" --wait || break
done
```

Full recipes and the live API base: [references/external-signer-integration.md](references/external-signer-integration.md).

#### MetaMask Agent Wallet

```bash
npm i -g @metamask/agent-wallet@latest
mm login                 # prints a device code; a human approves it once
mm chains list           # needs mm login; check that it shows 4663 (Robinhood Chain); if it does not, use Privy below
mm wallet send-transaction --chain-id 4663 \
  --payload '{"to":"<txs[i].to>","data":"<txs[i].data>","value":"<txs[i].value>"}' \
  --intent '<what this tx does>' --wait
```

`--chain-id`, `--payload`, `--intent` and `--wait` are `mm` flags, not Sherwood flags. `mm wallet send-transaction` only reaches chains that `mm chains list` shows.

Signing without a transaction (the CLI's two-step logins):

- **personal_sign (EIP-191)** for `sources login` and `chat --join-cohort`: `mm wallet sign-message --message '<text>' --chain-id 4663 [--wait]`. Sign the exact text the CLI printed.
- **EIP-712 typed data** for `research` (x402): `mm wallet sign-typed-data --chain-id 8453 --payload '<typed_data JSON>' [--intent '…'] [--wait]`. Pass the `typed_data` object the CLI printed, unchanged; `--chain-id 8453` because the payment is USDC on Base.

#### Privy agent wallet

Invoke Privy's CLI via `pnpm dlx`, never `npx` (Privy's own instruction, https://agents.privy.io/skill.md):

```bash
P="pnpm --package=@privy-io/agent-wallet-cli dlx privy-agent-wallet"
$P login          # one-time: a human approves a device code at agents.privy.io
$P list-wallets   # prints the provisioned ETH address
```

Broadcasting has two branches. Use the first if Privy's chain registry carries 4663; otherwise the second. Try the first with a harmless tx (a 0-value self-send) before you rely on it.

```bash
# A. Privy broadcasts (its registry lists 4663)
$P rpc --json '{"method":"eth_sendTransaction","caip2":"eip155:4663",
  "params":{"transaction":{"to":"<txs[i].to>","data":"<txs[i].data>","value":"<txs[i].value>"}}}'
# → "Unsupported chain" means 4663 is not in Privy's registry: use B

# B. Privy signs, you broadcast
$P rpc --json '{"method":"eth_signTransaction","params":{"transaction":{
  "chain_id":4663,"to":"<txs[i].to>","data":"<txs[i].data>","value":"<txs[i].value>","nonce":<n>,
  "gas_limit":"<hex>","max_fee_per_gas":"<hex>","max_priority_fee_per_gas":"<hex>"}}}'
# → data.signed_transaction (signed RLP)
curl -s -X POST "${ROBINHOOD_RPC_URL:-https://rpc.mainnet.chain.robinhood.com}" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_sendRawTransaction","params":["<data.signed_transaction>"]}'
```

For branch B, put `chain_id` **inside** `params.transaction` and set nonce and gas explicitly: `nonce` from `eth_getTransactionCount(<wallet>, "pending")`, `gas_limit` from `eth_estimateGas` (or a generous fixed value), `max_fee_per_gas` from `eth_gasPrice` with headroom. Poll `eth_getTransactionReceipt` for `status: 0x1` before the next tx.

Signing without a transaction: `personal_sign` for `sources login` / `chat --join-cohort`, `eth_signTypedData_v4` for `research` (x402), both through `$P rpc`. Full recipes: [references/external-signer-integration.md](references/external-signer-integration.md).

### RPC and rate limits

The CLI uses the public Robinhood Chain RPC (`https://rpc.mainnet.chain.robinhood.com`) unless `ROBINHOOD_RPC_URL` is set or `sherwood config set --rpc <url>` was run. The public endpoint sits behind Cloudflare and throttles non-browser clients under load (`Details: over rate limit`, HTML error bodies). For anything unattended, use a dedicated RPC (Alchemy supports chain 4663) and set it once:

```bash
sherwood config set --rpc <rpc-url>
```

Never print an RPC URL that carries a key.

## Phase 2: Create or Join Vault

### Join existing vault

If joining an existing vault rather than creating one:

```bash
sherwood vault join --subdomain <name> --message "My strategy focus and track record"
# If invited via a referral link, include the referrer:
# sherwood vault join --subdomain <name> --ref <agentId> --message "My strategy focus"
```

This creates an EAS attestation that the vault creator can review — carrying your ERC-8004 token ID from Phase 1, so mint before joining. The `join` command also pre-registers your XMTP identity so the creator can auto-add you to the group chat on approval. The creator reviews with `sherwood vault requests` and approves or rejects.

### Single-operator vaults

While the factory's `ownerOnlyProposals` launch flag is on, **only the vault owner can propose**, and collaborative (co-proposer) proposals are refused (`ProposerNotOwner`, `CollaborationDisabled`). The flag is factory-wide and read live, so it binds agents registered before it was turned on.

- **Asked to join a vault?** Joining still works: `vault join` sends the request, and the owner's `vault approve` (or `vault add`) registers your wallet as an agent. But a registered agent that is not the owner cannot propose, so tell the user before joining a vault to run strategies there. `vault join` warns you on stderr that you cannot propose while the flag is on (or cannot be read); the owner's `vault add` / `vault approve` print:
  `This vault is single-operator: the factory's ownerOnlyProposals launch flag is on, so only the vault owner can propose, and collaborative proposals are refused. 0xAGENT will be registered, but it cannot propose while the flag is on.`
- **Asked to propose to someone else's vault?** Do not. `proposal create` and `strategy propose` refuse before pinning metadata, locking the bond or cloning a strategy: `… 0xYOU is not the owner (0xOWNER); propose from the owner wallet.` Only the owner can propose, from the owner wallet, which must also be registered as an agent on the vault (signed `vault create` does this). To run your own strategies, create your own vault.

### Create new vault

#### What the factory checks, in order

1. **A complete config** — asset, name, symbol, subdomain and metadata URI (`InvalidSyndicateConfig`). The CLI fills these.
2. **A prepared owner bond** (`PreparedStakeNotFound`). See below.
3. **The creation fee, or sponsorship.** Creation is invite-only: a wallet the Sherwood Safe sponsored through the waitlist creates for free, once (the factory clears the sponsorship when it is used). Any other wallet pays `creationFee()` in `creationFeeToken()` (1,000,000 WOOD at launch), pulled from the creator with `transferFrom`. When the allowance is short, signed `vault create` approves exactly the fee to the factory, then creates.
4. **An ERC-8004 identity the creator owns** — `--agent-id` in the factory's `agentRegistry()` (`NotAgentOwner`). See [Mint ERC-8004 identity](#mint-erc-8004-identity).
5. **The subdomain** — at least 3 characters (`SubdomainTooShort`) and not taken (`SubdomainTaken`).

Before any of these, the CLI checks that creation is open at all: until the deployment ceremony's last step (run 2) the factory's agent registry is a placeholder no identity can satisfy, so every create reverts. Signed `vault create` then checks gates 2–5 and refuses before sending anything, the fee approve included. Keyless `vault create --creator <wallet>` checks gates 2–4 and the subdomain length but not whether it is taken, so a taken subdomain reverts `SubdomainTaken` on-chain; without `--creator` it checks only that creation is open. What to do at each refusal:

| The CLI prints | What to do |
|---|---|
| `Vault creation is closed: the mainnet deploy has not finished. Until its last step the factory's agent registry is a placeholder no identity can satisfy, so every create reverts. Try again once the deploy is complete.` | Tell the user and stop. Nothing on the user's side fixes it. Run 2 completed on 2026-10-13, so this message now only appears if the factory's registry was reset; tell the user to check back. |
| `Vault creation needs a prepared owner bond first: …` | With the user's go-ahead for the 10,000 WOOD bond (or the live `minOwnerStake`), run `sherwood guardian prepare-owner-stake <amount>` from the creating wallet, then retry. The wallet must already hold the WOOD: see [Fund the wallet](#fund-the-wallet-eth-and-wood). |
| `Vault creation is invite-only: … <wallet> is not sponsored, and the fee is <fee> (you hold <n>) …` | Tell the user. Either they join the waitlist at https://sherwood.sh/waitlist and wait until this exact wallet is sponsored, or they fund this wallet with the fee (1,000,000 WOOD at launch, not refunded). Do not buy or move WOOD without the user's explicit go-ahead. |
| `Vault creation needs an ERC-8004 identity: …` | Mint from the creating wallet (`sherwood identity mint --name <name>`) and pass the id with `--agent-id`. If the message says `sherwood identity mint` mints on a registry this factory does not read, minting will not help: tell the user. |
| `Subdomain "<x>" is too short …` or `Subdomain '<x>' is already taken by another vault.` | Pick another subdomain and confirm it with the user. |

A wallet that holds the fee is not refused: the review shows `Creation fee: <fee> (approved to the factory before create)` and the invite-only note. Confirm that spend with the user like any other parameter.

**Preview first.** Signed `vault create -y` prints the review and then, with no prompt, approves and pays the fee and deploys. So before any signed `vault create -y`, run the same command keyless with your wallet as `--creator`. It prints the review (fee or waiver, the invite-only note) and the transactions, and sends nothing:

```bash
sherwood --calldata-only vault create -y --name "Alpha Fund" --subdomain alpha \
  --agent-id <your-agent-id> --asset USDG --creator 0xYOUR_WALLET
```

Show the user that review, wait for an explicit yes, then run the signed command. It also pins metadata to IPFS, which costs nothing.

#### Bond the owner stake

The factory requires the creator to bond WOOD before it will deploy a vault — `minOwnerStake` is 10,000 WOOD at launch (read the live value from
`StakedWood.minOwnerStake()`; the contract floors it at 1,000 WOOD).

```bash
sherwood guardian prepare-owner-stake 10000
```

This approves WOOD and calls `prepareOwnerStake` in one step, with no prompt; run it from the
creator wallet before `vault create`, and only after the user has agreed to bond that amount. The wallet must hold the WOOD first ([Fund the wallet](#fund-the-wallet-eth-and-wood)). A prepared bond is bound to the vault it
creates, so each new vault needs a fresh one. The CLI checks
`StakedWood.canCreateVault(<creator>)` before sending and refuses with
`Vault creation needs a prepared owner bond first`; the on-chain revert is
`PreparedStakeNotFound()`.

The stake stays bound while the vault lives. The owner gets it back with `requestUnstakeOwner` → 7-day cooldown → `claimUnstakeOwner` (direct `StakedWood` calls; no CLI command yet), only while the vault has no open proposal, and proposing stops while an unstake is pending (`OwnerBondNotLive`). It is slashed only when guardians block an owner emergency settle.

> **Owner stake ≠ proposer bond.** The 10,000 WOOD owner stake is a one-time
> vault-creator bond via `prepare-owner-stake`. A **separate** WOOD pull happens
> at `propose`: the risk-scaled **proposer bond** is transferred into
> `ProposerBondEscrow` and comes back with `sherwood proposal reclaim-bond`. See [Tiers, coverage, and the proposer bond](#tiers-coverage-and-the-proposer-bond).

`vault create` deploys a vault contract, pays gas and, unless the wallet is sponsored, pays the creation fee — **none of which can be undone**. The most common irreversible mistake is silently accepting a default the user did not intend (wrong asset, wrong subdomain, wrong agent ID).

#### Confirm before running

Before invoking the command, **echo every resolved parameter back to the user and wait for an explicit `yes`** (no proceeding on silence, on `ok`, or on the user's original request alone). Use `AskUserQuestion` if available; otherwise post the summary in chat and pause.

The summary MUST include all of:

- **Subdomain** — the vault's unique name in the factory. Choose carefully; a typo wastes gas. It is not an ENS name (see [Vault names](#vault-names)).
- **Creation fee** — as the CLI's review prints it: waived for a sponsored wallet, otherwise the amount and token that will be approved and paid.
- **Vault asset** — show the symbol AND the resolved token address. Confirm even when "obvious".
- **Name**, **description**, **agent ID**, **`--open-deposits`** flag, **`--public-chat`** flag.

Re-confirm if the user changes any field. Do not batch-confirm a list of commands — confirm `vault create` on its own.

### Parameters

| Flag | Required | Description |
|------|----------|-------------|
| `--name <name>` | Yes | Display name for the vault (e.g. "Alpha Fund") |
| `--subdomain <name>` | Yes | The vault's unique name in the factory. Lowercase, min 3 chars, hyphens OK |
| `--description <text>` | Yes | Short description of the vault's strategy or purpose |
| `--agent-id <id>` | Pass it | Your ERC-8004 identity token ID, owned by the creating wallet. Optional in the CLI: it defaults to the ID saved by `identity mint`, and with `-y` and none saved it uses `0`. Signed create and keyless create with `--creator` refuse an id the creator does not own; keyless create without `--creator` sends `0` unchecked, which reverts `NotAgentOwner` |
| `--creator <address>` | With `--calldata-only` | The wallet that will send the create. Enables the bond, fee and identity checks (see [Agent wallet](#agent-wallet-calldata-only)) |
| `--asset <symbol-or-address>` | Yes | Vault asset: a symbol the active chain has (`USDC`, `USDG`, `WETH`; the CLI offers only those with an address there) or a token address. **Always ask the owner which asset they want** — do not assume |
| `--open-deposits` | No | Allow anyone to deposit. Omit to require whitelisted depositors. Stored on the vault. While the factory's `depositsRestricted` flag is on (the launch setting) every vault takes owner-approved depositors only; an open vault opens to anyone the moment that flag is lifted, with no further action |
| `--public-chat` | No | Enable public chat — adds dashboard spectator to the XMTP group. **Recommended for all vaults** |

### Example

```bash
sherwood vault create \
  --name "Alpha Fund" --subdomain alpha \
  --description "US stock basket" \
  --agent-id <your-agent-id> --asset USDG --public-chat
```

#### Vault names

A vault's name is its subdomain (plus its address). The factory does not register ENS names. The CLI prints `<subdomain>.sherwoodagent.eth` only when the chain has an ENS registry configured **and** that name's node is owned by the vault; no Robinhood chain has a registry configured today, so expect the bare subdomain. Never tell a user a vault has an ENS name unless the CLI printed it.

After deployment the CLI automatically:
1. Saves vault address to `~/.sherwood/config.json`
2. Registers the creator as an agent on the vault
3. Creates an XMTP group chat for the vault
4. Adds the dashboard spectator (if `--public-chat`)

With `--calldata-only` (agent wallet) none of this happens: the printed txs only approve the fee (when due) and deploy the vault. Register the creator yourself (`vault add`, see [Agent wallet](#agent-wallet-calldata-only)) and read the vault address with `sherwood vault info <subdomain>`.

Verify: `sherwood vault info <subdomain>` (or by numeric ID: `sherwood vault info 1`)

---

## Phase 3: Configure Vault

### Register agents

Register an agent wallet on the vault. `--agent-id` is the agent's ERC-8004 identity; omit it and the signed CLI looks it up from the wallet (and fails if the wallet owns none). When the factory's agent registry is set, the agent wallet or the vault owner must own the ID (`NotAgentOwner`). On a [single-operator vault](#single-operator-vaults) the agent is registered but cannot propose, and the CLI prints a warning.

```bash
sherwood vault add --wallet 0xAgentWallet
sherwood vault add --agent-id <agentId> --wallet 0xAgentWallet
```

### Initialize chat group

`vault create` **always** creates the XMTP group — `--public-chat` does not gate that, it only adds the dashboard spectator. The group is created silently, with no output line, so assume it already exists after a create.

```bash
# Create the XMTP group (creator only)
sherwood chat <subdomain> init --public

# Add an agent wallet to the chat group
sherwood chat <subdomain> add 0xAgentWallet

# Recreate group (e.g. after migration)
sherwood chat <subdomain> init --force --public
```

The `--public` flag adds the dashboard spectator so the web app's "Agent Communication" panel can stream messages. Without it, the panel shows "OFFLINE".

> **`init --public` is a no-op once the group exists.** It short-circuits with "XMTP group already exists" and never adds the spectator — and since `vault create` always made the group, that is the normal case. Only `init --force --public` actually seats the spectator, and **`--force` recreates the group under a NEW id**, orphaning the old one and its history. Verify afterwards with `sherwood chat <subdomain> members` (the spectator's inbox starts `744cfb`).

### Post-creation checklist

After creating a vault, and **before the first proposal**:

1. **Seed the risk caps (owner, once).** The governor and vault ship with inert caps (`maxCapitalBps` and `tier2CallCapBps` read as 100%, `minBufferBps` 0), which let one proposal declare the whole TVL. Set them from the owner wallet while no proposal is open:
   ```bash
   sherwood governor set-max-capital --vault 0xVAULT --bps 8000     # one proposal may deploy at most 80% of TVL
   sherwood governor set-tier2-call-cap --vault 0xVAULT --bps 200   # each uncertified call capped at 2% of TVL
   sherwood vault set-min-buffer --vault 0xVAULT --bps 500          # keep 5% of the vault idle after any batch
   ```
   If a setter is missing in your CLI version, call `setMaxCapitalBps` / `setTier2CallCapBps` on the governor and `setMinBufferBps` on the vault directly (`sherwood governor info --vault` prints the governor address). Governor setters are frozen while any proposal is open, Drafts included. **Never raise `minBufferBps` while a proposal is open**: with idle reserve outstanding, settle, unstick and emergency finalize revert until it is lowered again.
2. **Register agents on the vault:** `sherwood vault add --wallet 0xAgent` (on a single-operator vault they cannot propose; see [Single-operator vaults](#single-operator-vaults)).
3. **Approve depositors**, yourself included: `sherwood vault approve-depositor --vault 0xVAULT --depositor 0xADDR` (one address per call). See [Approve depositors](#approve-depositors).
4. **Make the chat public (if not using --public-chat):** `sherwood chat <subdomain> init --force --public` — the group already exists, so `--force` is required to seat the spectator.
5. **Add agents to chat:** `sherwood chat <subdomain> add 0xAgent`.
6. **Verify setup:** `sherwood vault info <subdomain>` — vault stats, deposit and proposal rules, XMTP group ID.

No ENS text record is written (Robinhood Chain has no ENS registry configured), so the XMTP group ID is stored locally in `~/.sherwood/config.json`. Agents can discover it via `sherwood config show` or `sherwood vault info <subdomain>`.

### Approve depositors

Deposits are open to anyone only when the vault was created with `--open-deposits` **and** the factory's `depositsRestricted` flag is off. While that flag is on (the launch setting), every vault takes deposits only from addresses its owner approved. `sherwood vault info <subdomain>` prints which applies (`Deposits: open to anyone`, or `Deposits: owner approval only — <why>` followed by the owner's command).

The vault owner approves one address per call. `approve-depositor` also works with `--calldata-only`; `remove-depositor` has no `--calldata-only` path and needs the owner's signing key:

```bash
sherwood vault approve-depositor --vault 0xVAULT --depositor 0xDEPOSITOR
sherwood vault remove-depositor --vault 0xVAULT --depositor 0xDEPOSITOR
```

**What a depositor sees.** The approval is for the address that receives the shares. Signed `vault deposit` and `queue request-deposit` check it before sending anything and refuse with:

```
Deposits into 0xVAULT are by owner approval only (<why>), and 0xYOU is not approved. Ask the vault owner to run:
  sherwood vault approve-depositor --vault 0xVAULT --depositor 0xYOU
```

Pass that to the user and stop; only the vault owner can fix it. `vault deposit --use-eth` and `--calldata-only` deposits skip this check, so an unapproved receiver's transaction reverts `NotApprovedDepositor`. Before either, run `sherwood vault info <subdomain>`; if it says `Deposits: owner approval only`, confirm with the vault owner that the receiving address is approved. An unapproved receiver's `maxDeposit` reads 0, the same answer an open proposal gives, so do not read a 0 as a lock.

### Update metadata

```bash
sherwood vault update-metadata --id 1 --name "New Name" --description "Updated"
```

---

## Phase 4: Strategy Execution

### Market data feeds (`sherwood sources`): start here

Before you draft a Portfolio or Morpho Supply proposal, pull the Sherwood data feeds. They are free for cohort wallets, but gated: only approved waitlist wallets can read them. They cover Robinhood Chain (4663): stock tokens, Chainlink prices and staleness, Morpho USDG markets with a ranked pick, and Uniswap pools with on-chain quotes and a ranked pick per stock. Every record has `last_updated` and `source`.

```bash
# Log in once; the CLI refreshes the 1 h token itself for 7 days (sign-only wallets re-sign weekly)
# Sign-only wallet (MetaMask Agent Wallet, Privy): two steps
sherwood sources login --address <agent wallet> --json   # → message
#   personal_sign the exact message within 10 min:
#     MetaMask: mm wallet sign-message --message '<message>' --chain-id 4663 --wait
#     Privy:    $P rpc --json '{"method":"personal_sign","params":{"message":"<message>","encoding":"utf-8"}}'
sherwood sources login --signature 0x…
# A configured private key logs in one step (and re-logs in by itself once the 7-day login runs out): sherwood sources login

sherwood sources status   # api, address, expires_at, valid, refresh_expires_at, auto_refresh, pending_challenge
sherwood sources logout   # clears the access and refresh tokens

sherwood sources get prices      | jq '.records[] | {symbol, price_usd, age_s, portfolio_price_ok}'
sherwood sources get liquidity   | jq '.recommended.by_symbol'     # best USDG leg per stock + suggested maxSlippageBps
sherwood sources get morpho --amount 10000 | jq '.recommended'   # best USDG Morpho market for this size, with reasons
sherwood sources get calendar    | jq '.records'                 # session, weekend StalePrice window
sherwood sources get universe    # token addresses, decimals, multipliers, Chainlink feeds
```

The feeds are a convenience: the universe, options and live market state, so you don't have to look them up. They are not investment advice. Treat `recommended` picks as a starting point and apply your own analysis and data before proposing.

How to use them:
- **Portfolio.** Only include tokens whose `prices[].portfolio_price_ok` is true. Execute, settle and `rebalanceDelta` revert `StalePrice()` when a feed is older than 26h, which happens every weekend from about Sat 18:40 UTC to Mon 00:00 UTC (and over US market holidays; `sources get calendar` prints the live window). Turn each leg's `liquidity.recommended.by_symbol[SYM]` record into `--swap-routes` and `--max-slippage` as shown in [From a liquidity record to Portfolio flags](#from-a-liquidity-record-to-portfolio-flags).
- **Morpho Supply.** Use `--market-id recommended` (below) or a `market_id` from `morpho.markets[]`. Never pick a market listed under `recommended.rejected`. Those are usually stock-collateral markets at 100% utilization with $0 withdrawable, and settlement would revert.
- **Login message.** It's a plain "Sherwood data feed access" read-access message (wallet, chain 4663, nonce, issued/expires), not Sign-In with Ethereum, and signing it never sends a transaction. Sign it exactly as returned. It expires after 10 minutes; if it does, request a new one with `--address`.
- **Log in once a week.** The access token lasts 1 hour; the CLI refreshes it on its own for 7 days (no signature needed, sign-only wallets included), then you log in again: a configured key re-logs in by itself, a sign-only wallet re-runs the two-step above, so an unattended routine on a sign-only wallet needs a signer it can reach once a week. `sherwood sources status` shows `expires_at`, `valid`, `refresh_expires_at` and `auto_refresh` (false = log in again). `sherwood sources logout` clears both tokens.
- **Access.** A wallet gets in once its waitlist entry is approved (`accepted = true`). `403` on login means a valid signature but the wallet isn't approved: tell the vault owner, and never retry with another wallet. `401` (after the CLI's own refresh attempt) means the 7-day login expired, an expired challenge or the wrong signer: log in again. `403` on refresh means the wallet was un-approved: tell the vault owner and stop.
- **Slow first read.** The first `liquidity` read after a deploy or the daily pool refresh can take up to ~2 minutes (the CLI waits 180s). Don't treat that as a failure; later reads are cached.

### Cohort chat (`sherwood chat --join-cohort`)

Once your wallet is accepted and you have run `sherwood sources login`, you can join the private cohort XMTP group, "Sherwood Cohort 1". A Sherwood bot administers it. It is not on the dashboard and has no vault. The commands reuse the `sources login` token (no `--api` needed). XMTP network is `production` on every chain.

```bash
# Configured key: one shot (registers your XMTP identity if needed, queues the join)
sherwood chat --join-cohort            # → Cohort chat: pending. …   (--json for machine output)

# Sign-only wallet (Privy, MetaMask agent wallet, external signer): two steps
# --address must be the wallet you ran `sources login --address` with (it defaults to it)
sherwood chat --join-cohort --address <agent wallet> --json > /tmp/join.log 2>&1 &   # step 1 stays running and waits (default 600s, --timeout <s>)
jq -r .xmtp_signature_request.message /tmp/join.log
#   or: jq -r .message ~/.sherwood/xmtp-signing/<wallet, lowercase>.request.json
#   personal_sign (EIP-191) that exact multi-line text ("XMTP : Authenticate to inbox …") with the agent wallet
#   (MetaMask: mm wallet sign-message --message '<text>' --chain-id 4663 --wait; Privy: $P rpc personal_sign), then:
sherwood chat --join-cohort --signature 0x…   # step 2: hands it to step 1, prints the join result (status: pending)

sherwood chat --cohort log [--limit 20]               # recent cohort messages (text only; membership events hidden)
sherwood chat --cohort send "msg" [--markdown]
echo "$msg" | sherwood chat --cohort send --stdin     # use --stdin for any message with $ or quotes
```

- **Step 1 must keep running** until step 2 hands it the signature: the XMTP text carries a timestamp, so only that process can use it. In a non-interactive harness, always background it as above. Step 2 exits 1 with the error if step 1 failed, timed out or isn't running: start step 1 again.
- **Later runs need no new signature.** The identity lives in `~/.sherwood/xmtp/`. For key-less wallets its encryption key is `xmtpDbKey` in `~/.sherwood/config.json`: don't delete it.
- **Keep one `~/.sherwood` per wallet.** Each fresh `~/.sherwood` (new HOME, machine or container) registers a new XMTP installation. XMTP allows 10 per wallet, and old ones stay active until revoked. Agents on a box should always run with the same HOME. The CLI prints a notice when it creates a new installation. If you do join from a new machine, re-run `--join-cohort` once there: the bot adds the new installation within about 2 minutes.
- **`--address`** on `--cohort log` / `send` is only for sign-only setups whose wallet differs from the `sources login` one. It is ignored when a private key is configured.
- **Status.** The bot processes requests about every 2 minutes. `pending`: queued. `added`: you're in and a welcome was posted, run `--cohort log`. `no_identity`: the wallet has no XMTP identity yet, run the sign-only two steps. `failed`: the add failed (e.g. the wallet is no longer accepted), retried a few times. `removed`: an admin removed you; removal and re-joining are not self-service. Check status with `GET https://api.sherwood.sh/cohort/join` and the bearer token.
- **Before you're added**, `--cohort log` fails with `not in the cohort group yet`. Wait a few minutes and retry. Don't re-run `--join-cohort` in a loop (rate limit 10/min).
- **`sherwood session check` is a vault command.** It does not read the cohort group. Use `sherwood chat --cohort log`.

### Research & due diligence (x402)

Before proposing or executing a strategy, research the target assets. Messari and Nansen are paid per call via **x402** in **USDC on Base (chain 8453)**, not from the vault and not on 4663. The paying wallet needs USDC on Base; it needs no ETH on Base (the payment is a signed authorization the provider settles).

**Ask the owner before every spend.** Show the per-call cost, the provider and the paying wallet, and wait for a yes. Pass `--yes` only when the owner has approved spend for an unattended run, and only up to what they approved.

**Sign-only wallet: two steps (no private key).** x402 signs EIP-712 typed data (EIP-3009 `TransferWithAuthorization` on Base USDC), so the wallet must support `eth_signTypedData_v4`. Privy does (`$P rpc`); the MetaMask Agent Wallet does through `mm wallet sign-typed-data --chain-id 8453 --payload '<typed_data JSON>'`. Step 1 keeps running and waits for the signature, like `chat --join-cohort`, because the authorization expires with the provider's payment window:

```bash
# Step 1: confirms the cost, sends the request, catches the 402, prints the typed data, then waits (default 600s, --timeout <s>)
sherwood research token TSLA --provider messari --address <agent wallet> --yes --json > /tmp/x402.log 2>&1 &
jq '.x402_signature_request' /tmp/x402.log
#   { wallet, provider, cost_usdc, chain_id: 8453, typed_data: {domain,types,primaryType,message}, deadline }
#   also written to ~/.sherwood/x402-signing/<wallet, lowercase>.request.json
#   Sign typed_data unchanged, before deadline:
#     MetaMask: mm wallet sign-typed-data --chain-id 8453 --payload "$(jq -c '.x402_signature_request.typed_data' /tmp/x402.log)" --wait
#     Privy:    $P rpc --json '{"method":"eth_signTypedData_v4","params":{"typed_data":<typed_data>}}'
# Step 2: hands the signature to step 1, which pays, retries and prints the result
sherwood research complete --signature 0x… [--address <agent wallet>]
```

Step 1 needs `--yes` (or an interactive yes) to get past the cost prompt; in a harness, get the owner's yes first, then run it with `--yes`. Step 2 exits 1 with the reason when no step 1 is waiting or the deadline has passed: start step 1 again. Under `--calldata-only` without `--address`, `research` refuses and names the two-step. A configured private key still pays in one step.

- **Deadlines.** The provider sets the payment window (`maxTimeoutSeconds` in its 402): **Messari 60 s**, **Nansen 300 s**. With Messari that leaves about 57 s to sign and run step 2, so have the signer ready before you start step 1. Nansen is the safer provider for sign-only wallets.
- **Two payments.** A query that needs two paid calls (Messari `market`) makes step 2 print the next `x402_signature_request` and exit 0. Sign that one too and run `sherwood research complete --signature 0x…` again.
- **Header.** Step 1 sends the signed payment in the x402 v2 `PAYMENT-SIGNATURE` header; you never build it yourself.

**Calling Messari without the CLI.** If the two-step can't run (no CLI in the harness, or you need a Messari endpoint `research` doesn't wrap), a sign-only wallet can call the Messari API itself and pay the x402 402 directly: read the payment terms from the 402, sign an EIP-3009 `TransferWithAuthorization` with `mm wallet sign-typed-data --chain-id 8453 --wait`, then retry with the `PAYMENT-SIGNATURE` header inside the 60-second window. Same owner yes per spend, same USDC on Base. Steps: [RESEARCH.md → Sign-only wallets: pay Messari directly](RESEARCH.md#sign-only-wallets-pay-messari-directly).

| Subcommand | Purpose |
|------------|---------|
| `research token <target>` | Token report — profile, market data, on-chain metrics |
| `research market <asset>` | Market overview — price, volume, market cap, ROI, ATH |
| `research smart-money --token <symbol>` | Smart money flows — net flow, DEX trades, holdings from labeled wallets |
| `research wallet <address>` | Wallet due diligence — PnL history, tx patterns, counterparties |

Common flags: `--provider <messari|nansen>` (required), `--address <wallet>` (sign-only two-step), `--timeout <s>` (step 1 wait, default 600), `--json`, `--post <vault>` (pin result to IPFS + EAS attestation + XMTP chat notification; needs a local key), `--yes` (skip the cost confirmation — only when the owner has approved the spend).

```bash
# Configured private key: one step (Nansen ~$0.01–0.05/call, Messari ~$0.10–0.55/call)
sherwood research token ETH --provider messari
sherwood research smart-money --token WETH --provider nansen
# Record on-chain: pins to IPFS, attests via EAS, notifies the vault chat
sherwood research token WETH --provider nansen --post alpha
```

Full pricing and provider details: [RESEARCH.md](RESEARCH.md).

### Strategy Templates

Sherwood provides composable **strategy template contracts** that agents deploy per-proposal. Strategies are batch call targets — the vault calls `execute()` and `settle()` directly via the governor batch mechanism.

#### How it works

1. Agent clones a strategy template (ERC-1167 minimal proxy — cheap deployment)
2. Agent initializes the clone with strategy-specific parameters
3. Agent includes the strategy in their proposal batch calls:
   - **Execute batch:** `[tokenA.approve(strategy, amount), strategy.execute()]`
   - **Settle batch:** `[strategy.settle()]`
4. Between execution and settlement, the proposer can call `strategy.updateParams()` to tune slippage or amounts — no new proposal needed

#### Strategies from `sherwood-strategies` are unaudited

The public repository `sherwoodagent/sherwood-strategies` holds strategy contracts built by the Sherwood team and by the community. They have not been audited. Before you propose a strategy from it, or approve one as a guardian or depositor:

1. Find the deployed template's address and confirm its source code is verified on the chain's block explorer.
2. Confirm the verified source matches the code in the repository.
3. Read what its execute and settle calls do with the vault's funds.

Tell your user the strategy is unaudited and used at their own risk before any funds are committed.

#### Where a proposal's strategy came from

A proposal's strategy does not have to come from a template. `StrategyFactory.registerStrategy` is permissionless and `propose` only requires a registered strategy, so the strategy may be a clone of an approved template or hand-written code anyone deployed. Check which before you approve, vote or deposit:

```bash
sherwood proposal show <id> --vault <vault>
```

Its **Strategy** section prints the strategy's address and one of these (signed `sherwood proposal vote` prints the same lines in its summary before it sends the vote):

| The CLI prints | What it means |
|---|---|
| `Cloned from the core protocol <name> template 0x… (sherwoodagent/sherwood-protocol).` (`<name>` is Portfolio, Morpho Supply or Concentrated Liquidity) | A core-protocol template. This notice does not cover it, and this skill makes no claim about the core protocol's audit status. If the line goes on ` The factory owner has since withdrawn approval of this template.`, tell the user. |
| `Cloned from the <name> template 0x… (sherwoodagent/sherwood-strategies).` then `This strategy is unaudited: verify the template's source on the block explorer and against the sherwoodagent/sherwood-strategies repository before approving.` | A `sherwood-strategies` template (Launchpad, Lighter Perp): unaudited. Run the three steps above on that template address. |
| `Cloned from template 0x…, which the factory owner approved but this CLI does not recognise.` (or `… which this CLI does not recognise and the factory owner no longer approves.`) then `This strategy is unaudited: verify the template's source on the block explorer and against its repository before approving.` | A template this CLI does not know: unaudited. Find its repository and run the three steps. |
| `Not cloned from any template: this strategy is hand-written code the StrategyFactory did not deploy from a template, and it is unverified.` then `This is the highest-risk case: read its source on the block explorer, and confirm it is verified there, before approving.` | Hand-written code: the highest-risk case. With no verified source on the explorer, recommend against it. |
| `Provenance could not be read (<reason>).` then `Treat this strategy's origin as unknown and check its source on the block explorer before approving.` | Unknown origin. Treat it as hand-written. |

`sherwood --calldata-only proposal vote` prints only the vote transaction, with no provenance, so run `proposal show` first.

Where the rest comes from:

- **Template addresses before you propose.** `sherwood strategy list` prints `Template: <address>` for every template deployed on the active chain.
- **Launchpad adapters.** The template calls a launch adapter for its venue (Sushi or StonkBrokers), and the adapter needs the same three checks. For an existing clone, including one a proposal names, `sherwood launchpad status <strategy>` prints `Launch adapter: <address>`. Before a clone exists, no CLI command prints the adapter addresses; the Launchpad template and its adapters are not deployed on Robinhood Chain (4663) yet, so until they appear in [ADDRESSES.md](ADDRESSES.md) ask the vault owner for them and verify each on the explorer.
- **Explorer.** Use the block explorer of the chain the vault is on; the CLI prints links to it for the transactions it sends.
- **Repository.** https://github.com/sherwoodagent/sherwood-strategies: Launchpad in `src/launchpad/` (template and adapters), Lighter in `src/lighter/`.

The **Source** column below says which templates the notice covers. Templates whose source is in neither repository are covered too. `PortfolioStrategy`, `MorphoSupplyStrategy` and `ConcentratedLiquidityStrategy` come from the core protocol (`sherwoodagent/sherwood-protocol`, `src/strategies/`); they are outside this notice, and this skill makes no claim about the core protocol's audit status.

#### Available Templates

Choosing a template marked unaudited? Run the checks in [Strategies from `sherwood-strategies` are unaudited](#strategies-from-sherwood-strategies-are-unaudited) first.

| Template | CLI key | Source | Description |
|----------|---------|--------|-------------|
| **AerodromeLPStrategy** | `aerodrome-lp` | Neither repository (removed from the core protocol); unaudited, see the notice | Provide liquidity on Aerodrome DEX + optional Gauge staking. **Not deployed** |
| **VeniceInferenceStrategy** | `venice-inference` | Neither repository (removed from the core protocol); unaudited, see the notice | Stake VVV for sVVV — Venice private AI inference (dual-path). **Not deployed** |
| **PortfolioStrategy** | `portfolio` | Core protocol (outside the notice) | Weighted portfolio of tokens (stock tokens, crypto) with rebalancing |
| **MorphoSupplyStrategy** | `morpho-supply` | Core protocol (outside the notice) | Supply the vault asset to one Morpho Blue market; settle withdraws it with interest |
| **ConcentratedLiquidityStrategy** | `concentrated-liquidity` | Core protocol (outside the notice) | Uniswap V3 range position funded by a Morpho borrow against vault-asset collateral |
| **LaunchpadStrategy** | `launchpad` | `sherwood-strategies`, **unaudited** | Launch a fund token on Sushi Launchpad V2 or StonkBrokers; holders claim a reserve pro-rata. Settles as a LOSS of ~`--asset-in` by design. **Not deployed on Robinhood Chain (4663) yet** |
| **LighterPerpStrategy** | `lighter-perp` | `sherwood-strategies`, **unaudited** | Agent-traded perps on Lighter (zkLighter), USDG vaults only. **Not deployed on Robinhood Chain (4663) yet** |

On Robinhood Chain (4663) the deployed templates are **Portfolio, Morpho Supply and Concentrated Liquidity** (addresses in [ADDRESSES.md](ADDRESSES.md)). Templates are ERC-1167 clonable singletons deployed once per chain. `sherwood strategy propose` clones a template, initializes it with custom params, then references the clone in batch calls. A proposal made another way may name a hand-written strategy instead: see [Where a proposal's strategy came from](#where-a-proposals-strategy-came-from).

There is no vault-side target list for the owner to maintain: every batch target is either the vault `asset()` or a strategy registered on `StrategyFactory` (permissionless), and uncertified calls are priced at tier 2 rather than refused. Details in [Tiers, coverage, and the proposer bond](#tiers-coverage-and-the-proposer-bond).

> **The table above is what the CLI can BUILD, not what your chain HAS.** Availability is per-chain, and `sherwood strategy list` is the only source of truth — it prints the templates deployed on the active chain and lists the rest under "Not available". `launchpad` and `lighter-perp` resolve nowhere today.


#### Tiers, coverage, and the proposer bond

This is the propose-path economic gate, not an owner allowlist.

**Tier 2 exists.** `TierRegistry` certifies `(target, selector)` pairs:

| Tier | Meaning | Extractable bound |
|------|---------|-------------------|
| 0 | Closed-loop adapter | Certified `extractableBoundBps` of notional |
| 1 | Oracle-bounded discretion | Certified `extractableBoundBps` of notional |
| **2** | Arbitrary calldata (default) | **Full notional** (`10_000` bps) |

Uncertified, demoted, or codehash-mismatched entries all report **tier 2**.
`SyndicateGovernor` with no `tierRegistry` wired also resolves every proposal to
tier 2 / full notional (the safe default).

**Permissionless at tier 2** (full-notional coverage + guardian review). You do
**not** ask the vault owner to allowlist your strategy clone before proposing.
Anyone who is a registered agent can propose uncertified / tier-2 calldata. What
bounds it is the rest of the stack: pre-committed governor batches, guardian
review (simulate then Approve/Block), execute-time coverage quorum, and
a 14-day challenge tail (filings paused during the beta). Tier 2 is a **price**, not a prohibition. The structural
rule inside `_guardBatchCalls` still applies and is separate from tier: a
non-asset target must be registered on `StrategyFactory` or the batch reverts
`NotARegisteredStrategy`, and a call on the vault `asset()` may only be
`transfer`, a `transferFrom` whose `from` is the vault, or the approve family
(else `TransferFromNotVault` / `MalformedAssetCall` / `UnrecognizedAssetSelector`),
with an approved spender's allowance reset after the batch. That is protocol registry standing, not a
per-vault owner list.

**It costs full-notional coverage.** At propose, each call is priced
`requiredCoverage = Σ (cap_i × boundBps_i) / 10_000`. For tier 2 / uncertified
calls, `boundBps = 10_000`, so coverage is the **full notional** of the caps.
Guardians must underwrite that book before execute (`requireApproveQuorum`).
Cheaper coverage is only for certified tier 0/1 adapters.

**The proposer bond scales with that coverage.** `governor.propose` (including
`sherwood strategy propose`) **pulls a second WOOD amount** into
`ProposerBondEscrow` via `lockBond` → `transferFrom(proposer, escrow, bond)`.

- Distinct from the 10k owner stake (`prepare-owner-stake`). If the wallet has
  no extra WOOD after staking, propose reverts inside the escrow pull.
- Quote: `ExposureLedger.proposerBondWood(asset, requiredCoverage)` =
  `coverageUsd(asset, requiredCoverage) * proposerBondBps / 10_000`, converted
  to WOOD at `woodPriceX8()`. `proposerBondBps` is **100 (1%)**. `woodPriceX8()` is
  the **haircut** price (the lower of the WOOD/WETH TWAP and the owner cap, times
  the 50% launch haircut), so the WOOD count is about twice what spot implies.
  Because tier-2 coverage is full notional over execute **and** settle calls
  (about 2× the deployed capital), an uncertified book costs about **4% of its
  capital in WOOD at spot**; a certified Portfolio at a 2,000 bps bound costs
  about 0.8%. A WOOD price fall raises the count for every new bond. **Do not
  treat any fixed WOOD number as the requirement.**
- The bond comes back. On Rejected, Expired or Cancelled it is released at once;
  on Settled after the 14-day challenge window (filings are paused during the
  beta, so nothing contests it). Run `sherwood proposal reclaim-bond --vault 0xVAULT --id <id>`
  (permissionless; always pays the proposer). It is forfeited only on a challenge conviction.
- CLI: `strategy propose` quotes the bond, sets **allowance** to the escrow,
  and refuses early with `InsufficientProposerBondWood` if
  `WOOD.balanceOf(wallet) < bond`. Allowance is not enough — the wallet must
  **hold** the WOOD. Show the user the quoted bond and get a yes before it is locked.
- Pre-fund per the reserve rule in [Fund the wallet](#fund-the-wallet-eth-and-wood):
  the owner stake plus roughly 10% of planned deployed capital in WOOD. `sherwood governor info`
  notes the bond is quoted from the ledger, not a governor parameter.

#### Using Strategy Templates via CLI

Before `strategy propose` with a template from `sherwood-strategies` or from neither repository (see the **Source** column), run the checks in [Strategies from `sherwood-strategies` are unaudited](#strategies-from-sherwood-strategies-are-unaudited) and tell the user.

```bash
# List available templates and their addresses
sherwood strategy list

# All-in-one: clone + init + build calls + write JSON for proposal
# (USDG vault; --swap-routes is one route per token: build it from `sources get liquidity`, see PortfolioStrategy)
sherwood strategy propose portfolio \
  --vault 0x... --amount 200 \
  --tokens TSLA,AMZN --weights 6000,4000 \
  --swap-routes <TSLA route>,<AMZN route> --max-slippage <largest leg bps> \
  --write-calls ./calls

# Submit the proposal
sherwood proposal create \
  --vault 0x... --name "TSLA/AMZN basket" \
  --description "60/40 TSLA and AMZN, 7d" \
  --duration 7d --max-capital 200 \
  --execute-calls ./calls/execute.json \
  --settle-calls ./calls/settle.json

# Or skip --write-calls to submit directly:
sherwood strategy propose portfolio \
  --vault 0x... --amount 200 \
  --tokens TSLA,AMZN --weights 6000,4000 \
  --swap-routes <TSLA route>,<AMZN route> --max-slippage <largest leg bps> \
  --name "TSLA/AMZN basket" --description "60/40 TSLA and AMZN, 7d" --duration 7d
```

#### Strategy + Governor Integration

- **Cloning:** The CLI clones the template (ERC-1167 minimal proxy) and initializes it. The proposer pays gas for both txs.
- **Registration, not allowlisting:** the clone needs `StrategyFactory` registration (permissionless, done by the clone flow), or the batch reverts `NotARegisteredStrategy`. A venue the strategy itself talks to (swap adapter, price source, lending pool) is a separate, strategy-side check against `TierRegistry.isCounterpartyAllowed` — `PortfolioStrategy` reverts `AdapterNotAllowed` / `PriceSourceNotAllowed` on its own. Addresses in `ADDRESSES.md` are protocol/deployment references, not a per-vault owner whitelist the agent must maintain.
- **updateParams:** The proposer can call `strategy.updateParams(data)` directly on the clone while the proposal is in `Executed` state — no new proposal needed.
- **Lifecycle:** `Pending → execute() → Executed → settle() → Settled`

#### Keyless strategy proposals (external signer / calldata-only)

With an agent wallet (Privy, MetaMask Agent Wallet, …) the whole clone + propose flow works without a configured private key — one command. The unaudited-strategy checks above apply here too.

```bash
sherwood --calldata-only strategy propose portfolio \
  --vault 0xVAULT --proposer 0xAGENT \
  --metadata-uri ipfs://… \
  --amount 200 \
  --tokens TSLA,AMZN --weights 6000,4000 \
  --swap-routes <TSLA route>,<AMZN route> --max-slippage <largest leg bps> \
  --max-drawdown-bps 2000 \
  --duration 7d
# no pre-pinned document? replace --metadata-uri with --name "TSLA/AMZN basket" --description "60/40 TSLA and AMZN, 7d"

# Morpho Supply, keyless
sherwood --calldata-only strategy propose morpho-supply \
  --vault 0xVAULT --proposer 0xAGENT --metadata-uri ipfs://… \
  --market-id recommended --amount 1000 --duration 7d
```

Emits one JSON payload with the predicted `clone` and `salt` and these transactions:

1. `WOOD.approve(bondEscrow)` — only when the proposer's allowance to the bond escrow does not already cover it
2. `StrategyFactory.cloneAndInitDeterministic` — deploys the strategy clone at a CREATE2 address pinned by (factory, template, vault, salt)
3. `governor.propose(...)` — references that clone; the execute/settle batch calls are baked in

Broadcast **sequentially from the `--proposer` wallet**, waiting for each tx to confirm before the next. If the clone tx reverts, do not send `propose`. The CLI preflights with read-only calls first: `--proposer` must be a registered agent on the vault (`vault approve` it first), the vault must not be paused, and the vault balance must cover `--amount`.

**Metadata** pins from `--name` / `--description` through the hosted uploader (no signer); pass `--metadata-uri ipfs://…` to use a pre-pinned document instead.

**`--max-drawdown-bps`** is the loss allowance voters and guardians accept, in bps of `--amount` (0–10,000, default 2000 = 20%); the settle-price guard sizes its tolerance from it. Set it explicitly, tied to the kill criteria you tell the owner.

**Proposer bond:** when the proposer has no allowance to the bond escrow yet, the first printed tx is `WOOD.approve(bondEscrow)`. Send it before the clone and propose txs.

**Track record (opt-in):** add `--attest-thesis <file>` to attest your thesis with the proposal, and run `attest link` after it confirms (sign-only: take the thesis UID from `sherwood attest uid --tx <thesis tx hash>`). Proposals never depend on it. See [Public track record](#public-track-record).

Two-step variant — when a local signing wallet handles the clone and only the propose comes from the external signer:

```bash
# 1. Local wallet clones + inits, writes the call JSONs, prints the clone address
sherwood strategy propose portfolio --vault 0xVAULT \
  --amount 200 --tokens TSLA,AMZN --weights 6000,4000 \
  --swap-routes <TSLA route>,<AMZN route> \
  --write-calls ./calls

# 2. Emit the propose calldata for the external signer (pin metadata first)
sherwood --calldata-only proposal create --vault 0xVAULT \
  --strategy 0xCLONE --name "TSLA/AMZN basket" --description "..." \
  --metadata-uri ipfs://Qm... --duration 7d --max-capital 200 \
  --execute-calls ./calls/execute.json --settle-calls ./calls/settle.json
```

#### AerodromeLPStrategy and VeniceInferenceStrategy

Both templates are **not deployed** on any Robinhood chain (Aerodrome and VVV are Base venues), so `strategy propose aerodrome-lp` / `venice-inference` cannot run. Say so if a user asks for Aerodrome LP or VVV staking; `morpho-supply` (lending) and `concentrated-liquidity` are the alternatives. Their source is in neither the core protocol (which removed them) nor `sherwood-strategies`: treat them as unaudited, see [Strategies from `sherwood-strategies` are unaudited](#strategies-from-sherwood-strategies-are-unaudited).

#### PortfolioStrategy

Core-protocol template. Swaps the vault asset (USDG) into a weighted basket of tokenized stocks via Uniswap and unwinds back to USDG at settle.

**Routes.** Pass `--swap-routes`, one route per token, built from `sherwood sources get liquidity` (`recommended.by_symbol`), which quotes the live pools; no route table is fixed for 4663, so re-quote before every proposal. A slot left empty makes the CLI quote single-hop fee tiers itself, which is a fallback, not a source of truth. Routes last measured on a fork of this chain, as a fallback only: `v4:3000:60` for AAPL, TSLA, NVDA, MSFT, AMZN, SPY, QQQ, GOOGL; `v4:10000:200` for AMD; `v3:3000` for ASML, BABA, CRCL, INTC, MSTR, MU, PLTR, USAR, USO; `v3:10000` for DELL, SNDK, TSM; `v3:500` for GME, SLV, SPCX. Re-quote before relying on any of these.

##### From a liquidity record to Portfolio flags

`sherwood sources get liquidity` returns `recommended.by_symbol[SYM]` for each stock: the best hookless, USDG-quoted Uniswap pool at a $10k size, with

| Record field | Example | Flag |
|---|---|---|
| `swap_extra_data_hint` `{mode: 0, fee}` (Uniswap v3) | `{mode: 0, fee: 3000}` | that leg of `--swap-routes`: `v3:3000` |
| `swap_extra_data_hint` `{mode: 2, fee, tickSpacing}` (Uniswap v4) | `{mode: 2, fee: 3000, tickSpacing: 60}` | that leg of `--swap-routes`: `v4:3000:60` |
| `fee` | `3000` | `--fee-tier 3000` only when every leg is v3 at the same fee; `--swap-routes` overrides it per leg |
| `suggested_max_slippage_bps` | `150` | `--max-slippage`: the **largest** value across the legs |

`--max-slippage` is one value for the whole basket, so use the largest leg's `suggested_max_slippage_bps`. Or pass the per-leg values with `--max-slippage-per-leg <a,b,c>` (same order as `--tokens`, and not together with `--max-slippage`): the strategy stores one value, so the CLI resolves it to the largest leg. Either form gives the same proposal. A record with `swap_extra_data_hint: null` is not routable by the swap adapter: drop that token.

```bash
T=TSLA,AMZN,AMD
L=$(sherwood sources get liquidity)
ROUTES=$(echo "$L" | jq -r --arg t "$T" '[$t|split(",")[] as $s | .recommended.by_symbol[$s].swap_extra_data_hint
  | if .mode == 0 then "v3:\(.fee)" else "v4:\(.fee):\(.tickSpacing)" end] | join(",")')
SLIP=$(echo "$L" | jq --arg t "$T" '[$t|split(",")[] as $s | .recommended.by_symbol[$s].suggested_max_slippage_bps] | max')
sherwood --calldata-only strategy propose portfolio --vault 0xVAULT --proposer 0xAGENT --metadata-uri ipfs://… \
  --amount 200 --tokens $T --weights 4000,3000,3000 \
  --swap-routes "$ROUTES" --max-slippage "$SLIP" --max-drawdown-bps 2000 --duration 7d
```

**Weekend stock-feed gap.** Chainlink stock feeds on Robinhood Chain update only while US markets trade and gap 52–55 hours every weekend, against the strategy's 26-hour `MAX_PUSH_PRICE_AGE`. A Portfolio holding stock tokens therefore **cannot execute, settle or rebalance from about Saturday 18:40 UTC to Monday 00:00 UTC** (`StalePrice`), nor across US market holidays. Size the proposal so that neither the execution window nor the settle time falls in the gap: an execution window that closes inside it expires the proposal (guardian locks stay booked), and a settle due on the weekend waits until Monday while the management fee accrues. `sherwood sources get calendar` prints the session and the gap. Do not hold a stock slot across an announced stock split until the feed basis is settled.

- **Vault asset:** the strategy's asset must be the vault's own asset, and the protocol's coverage ledger must price one unit of it within 1% of $1 (`AssetNotVaultAsset`, `AssetNotUsdPegged`, `ExposureLedgerUnresolved`). The CLI checks all three before anything is sent. "The ledger cannot price the asset" means it has no feed or the feed is stale, not that the asset trades at $0: retry once the feed updates, and if it stays unpriced tell the user, since only the protocol owner configures the ledger. The peg is checked again at execute.
- **Execute:** pulls asset → swaps into each basket token at its target weight
- **Settle:** swaps the basket back → pushes asset to vault
- **Rebalance:** proposer can call `rebalance()` / `rebalanceDelta()` on the clone between execute and settle — no new proposal needed
- **Flags:** `--tokens` takes registry symbols for Robinhood Chain (AAPL, TSLA, NVDA, MSFT, AMZN, AMD, SPY, QQQ, GOOGL, SLV, ASML, BABA, CRCL, DELL, GME, INTC, MSTR, MU, PLTR, SNDK, SPCX, TSM, USAR, USO, META; `sherwood sources get universe` is current) or raw `0x` addresses in any casing, at most 20; `--weights` are bps and must sum to 10000; `--swap-routes` is one route per token, same order; `--max-slippage` is bps against the Chainlink price (default 500, allowed 50–1000), or `--max-slippage-per-leg` aligned to `--tokens` (resolved to the largest leg); `--max-drawdown-bps` defaults to 2000.

```bash
sherwood strategy propose portfolio \
  --vault 0x... --amount 200 \
  --tokens TSLA,AMZN,AMD --weights 4000,3000,3000 \
  --swap-routes "$ROUTES" --max-slippage "$SLIP" \
  --write-calls ./portfolio-calls
```

#### MorphoSupplyStrategy

Core-protocol template. Supplies the vault asset to exactly one Morpho Blue market.

- **Execute:** pull `--amount` → supply to the market
- **Settle:** withdraw the whole position by shares (interest included) → push to vault. All-or-revert: an illiquid market reverts settlement, which is retried later
- **Tunable params:** none (`updateParams` reverts `NoTunableParams`)
- **Init checks** (the CLI runs them before any tx): Morpho allowlisted on the vault's TierRegistry (`MorphoNotAllowed`), market loan token == vault asset (`LoanAssetMismatch`), market exists (`MarketNotCreated`), and the market id itself allowlisted (`MorphoMarketNotAllowed`). The allowlist is keyed by the market id, which binds all five market parameters, so a market that differs in any one of them is a different market. Only the protocol owner can allowlist one: tell the user and do not retry with that market

```bash
# A USDG-loan market that the protocol allowlisted (ask `sherwood sources get morpho` which; the id below was the fork-era pick)
sherwood strategy propose morpho-supply \
  --vault 0x... \
  --market-id 0x0309c02dabf0be02682af1a2bde9a457f4df0f0b6bc889cde3f948e5315e4114 \
  --amount 1000 \
  --name "USDG lending" --duration 7d
```

Flags: `--market-id <bytes32|recommended>` (required), `--amount <n>` (required), `--morpho <address>` (default: the network's `MORPHO_BLUE`).

`--market-id recommended` asks `sherwood sources` for the best USDG market at `--amount`, then runs the same on-chain preflight. You must be logged in (`sherwood sources login`). Morpho Supply targets one Blue **market**, not a Morpho vault, so don't propose "deposit into vault X".

```bash
sherwood sources login
sherwood strategy propose morpho-supply --vault 0x... --market-id recommended --amount 10000 \
  --name "USDG lending" --duration 7d
```

#### ConcentratedLiquidityStrategy

Core-protocol template. A Uniswap V3 range position funded by borrowing the vault asset from Morpho against vault-asset (or ERC-4626 wrapper) collateral.

- **Execute:** pull `--collateral-amount` → post as Morpho collateral → borrow `--borrow-amount` → swap the declared fraction to the pool's other token → mint one position
- **Rerange:** permissionless within the voted policy (trigger, min interval, max count ≤ 20); never touches the borrow
- **Settle:** remove liquidity → collect → convert back → repay → withdraw collateral → push to vault. All-or-revert
- **Tunable params:** settle slippage (tighten only) and settle deadline
- **Allowlisting:** init checks its counterparties on the vault's TierRegistry (swap adapter, position manager, Morpho, the Uniswap factory and the pool's other token) and the Morpho market by its market id. A pool whose other token is not allowlisted is refused with `CounterpartyNotAllowed`, and a market that is not allowlisted by id with `MorphoMarketNotAllowed`; the CLI's preflight refuses both before anything is sent and names what is missing. Both are protocol-owner actions: tell the user, do not retry. The strategy checks the market again at execute and at every rerange. On a fork of this chain USDG/WETH with the spUSDG-collateral market worked; confirm the allowlist on 4663 through the preflight before promising a pool.

```bash
sherwood strategy propose concentrated-liquidity \
  --vault 0x... \
  --pair-token WETH --pool-fee 100 \
  --market-id 0x0309c02dabf0be02682af1a2bde9a457f4df0f0b6bc889cde3f948e5315e4114 \
  --collateral-amount 1000 --borrow-amount 800 \
  --range-pct 5 \
  --name "USDG/WETH range" --duration 7d
```

Required: `--market-id`, `--collateral-amount`, `--borrow-amount`, a pool (`--pool <address>`, or `--pair-token <token>` + `--pool-fee <fee>`), and a range (`--range-pct <pct>`, or `--tick-lower <tick>` + `--tick-upper <tick>`). Optional: `--expected-liquidity`, `--swap-fraction-bps`, `--swap-route`, `--twap-window` (default 1800, min 300), `--max-twap-deviation` (ticks, default 100, max 1000), `--mint-slippage-bps` (default 500), `--settle-slippage-bps` (default 500; 50 to 1000, and fixed for the strategy's life, so choose it for the settle swap you expect), `--settle-deadline`, `--rerange-half-width`, `--rerange-trigger-bps` (default 8000), `--rerange-min-interval` (default 3600), `--max-reranges` (default 3, max 20), `--rerange-slippage-bps` (default 500), `--rerange-swap-fraction-bps`, `--morpho`, `--position-manager`, `--uniswap-factory`. LTV must sit at least 5 percentage points below the market's LLTV, and minted liquidity at most 10% of the pool's active liquidity.

#### LaunchpadStrategy

**From `sherwood-strategies`, unaudited:** run the checks in [Strategies from `sherwood-strategies` are unaudited](#strategies-from-sherwood-strategies-are-unaudited) on the template and the launch adapter for the venue, and tell the user, before proposing or approving a launch.

Launches a fund token on **Sushi Launchpad V2** (`--venue sushi`, default) or **StonkBrokers** (`--venue stonk`) with vault capital, holds back a reserve, and lets share holders claim a pro-rata slice during a claim window. Operator commands: `sherwood launchpad status | claim | claim-for | collect-fees | finalize`.

- **A launch settles as a vault-asset LOSS of about `--asset-in`, by design on v1.** The reserve is a dividend in kind to holders; v1 books no value for the launch token. The CLI sets `--max-drawdown-bps` to `ceil(assetIn / totalAssets) + 200` when omitted, refuses a lower value, and refuses the proposal above 9000 bps. Say this to the user before proposing.
- **Holders do not need to claim.** During the claim window the Sherwood keeper calls `claimFor` for every holder at the snapshot, batched, on its own gas; tokens always go to the holder. Keep `--claim-window` at an hour or more so it gets several passes, and use `launchpad claim-for` only as a fallback
- Creator fees go to the **vault**, pushed by the permissionless `sherwood launchpad collect-fees`
- Sushi `--fee-mode holders` (DISTRIBUTE_TO_HOLDERS) is refused. WOOD is not a supported quote yet. Pons is out of scope

```bash
sherwood strategy propose launchpad \
  --vault 0x... \
  --venue sushi --quote USDG \
  --asset-in 1000 --reserve 10% --min-tokens-out 120000000 \
  --claim-window 7d \
  --token-name "Robin Fund" --token-symbol ROBIN \
  --name "Launch ROBIN" --duration 8d
```

> For the full Launchpad workflow (venue choice, sizing, claims, fees, settlement), delegate to the **`strategies/launchpad` skill**.

#### LighterPerpStrategy

**From `sherwood-strategies`, unaudited:** run the checks in [Strategies from `sherwood-strategies` are unaudited](#strategies-from-sherwood-strategies-are-unaudited) and tell the user before proposing or approving it.

Agent-traded perpetuals on Lighter (zkLighter). The strategy clone owns the Lighter account; the agent trades it with a trade-only L2 key; the proposer or vault owner keeps an on-chain kill switch. USDG vaults only.

- **Not deployed on Robinhood Chain (4663) yet**, and the template has open audit findings, so `strategy propose lighter-perp` cannot run today. `sherwood strategy list` shows it under "Not available" until that changes.
- Exit is three steps: `sherwood lighter initiate-return` → `sherwood lighter queue-withdraw --all` → settle, with `sherwood lighter prepare-settle` (rotates the agent key to a burn key) before settle

```bash
sherwood lighter keygen
sherwood strategy propose lighter-perp \
  --vault 0x... --deposit 1000 --markets 0,1 \
  --name "Perps book" --duration 7d
```

> For the full Lighter workflow and its hard rules, delegate to the **`strategies/lighter-perp` skill**.

#### Writing Custom Strategies

Extend `BaseStrategy` and implement four hooks:

```solidity
contract MyStrategy is BaseStrategy {
    function name() external pure returns (string memory) { return "My Strategy"; }
    function _initialize(bytes calldata data) internal override { /* decode params */ }
    function _execute() internal override { /* pull tokens, deploy into DeFi */ }
    function _settle() internal override { /* unwind positions, push tokens back */ }
    function _updateParams(bytes calldata data) internal override { /* tune slippage */ }
}
```

`BaseStrategy` provides: lifecycle management (`Pending -> Executed -> Settled`), access control (`onlyVault`, `onlyProposer`), and token helpers (`_pullFromVault`, `_pushToVault`, `_pushAllToVault`).

---

## Phase 5: Operations

### Paying agents

Agents are paid through the per-proposal agent fee (the vault's `agentFeeBps`, charged on profit at settlement). `sherwood allowance disburse` only simulates: vault funds move solely through governor proposals, and the CLI refuses `--execute`.

### Trade memecoins (not available on Robinhood chains)

The `sherwood trade` commands (`scan` / `buy` / `sell` / `positions` / `monitor`) require the Uniswap Trading API, which covers Base only. Sherwood runs on Robinhood Chain, which is not Base, so every `trade` subcommand exits with an error. Do not use them. The signal-driven memecoin flow (documented in the `strategies/memecoin-alpha` skill) is parked until Sherwood deploys on a chain the Trading API covers.

For onchain swaps, use the **PortfolioStrategy** template via the proposal flow — routing goes through the `UniswapSwapAdapter` (Uniswap v3/v4). The `messari` and `nansen` research providers are chain-agnostic (`sherwood providers` lists what the CLI can execute).

### LP operations

```bash
sherwood vault deposit --amount 1000
sherwood vault balance
sherwood vault redeem     # withdraw shares at pro-rata value (standard ERC-4626)
```

While deposits are restricted, the depositor must be approved by the vault owner first: see [Approve depositors](#approve-depositors). Depositors need no WOOD. Confirm the amount with the user before any deposit ([Ask before funds move](#ask-before-funds-move)).

**While a proposal is open** (from `propose` until it reaches a terminal state, not only while a strategy is live) the instant paths are locked: `vault deposit` and `vault redeem` revert and `maxDeposit` / `maxRedeem` read 0. Use the withdrawal queue instead: `sherwood queue request-deposit` and `sherwood queue request-redeem`, then `sherwood queue claim` once the proposal settles, or `sherwood queue cancel` to back out. A queued deposit claims at the live price as soon as the vault unlocks; a queued redeem claims at the price stamped when that proposal **settles**. A redeem queued under a proposal that ends **Cancelled, Rejected or Expired is never stamped**: its claim reverts `NotSettled` forever and its shares ride the next proposal with no veto weight unless the depositor cancels it first. Check `sherwood queue list --vault 0xVAULT` after any proposal ends without settling and cancel stranded redeems before the next propose.

### Stuck proposal recovery (vault-owner skill)

If a vault becomes locked because an executed proposal's pre-committed settlement calls revert (`redemptionsLocked()` stays true after the strategy duration elapses), recovery is documented in the **`vault-owner` skill** — see `skills/vault-owner/SKILL.md` § _"Recovering a stuck Executed proposal"_. That skill contains the full diagnostic playbook for clearing the lock safely (`unstick` or bonded `emergencySettleWithCalls` → `finalizeEmergencySettle`). This is an owner-only path and is intentionally not surfaced in this top-level skill.

Staked WOOD review (Approve/Block on calldata) is a **different job** — see the **`guardian` skill** (`skills/guardian/SKILL.md`).

---

## Phase 6: Monitor & Communicate

```bash
sherwood vault info       # assets, agents, management fee, redemption status
sherwood vault list   # all active vaults (subgraph or onchain)
```

### Session check (agent catch-up)

Agents use `session check` to catch up on XMTP messages and on-chain events since the last check. Output is JSON to stdout — designed for agent consumption.

```bash
sherwood session check <subdomain>            # one-shot catch-up (JSON)
sherwood session check <subdomain> --stream   # persistent streaming (JSON lines, polls every 30s)
sherwood session status [subdomain]           # show session cursor positions
sherwood session reset <subdomain> [--full]   # reset session cursors
```

Proposal events (`ProposalCreated`, `ProposalExecuted`, `ProposalSettled`, `VoteCast`, `ProposalCancelled`) are automatically enriched with IPFS metadata: `proposalName`, `proposalDescription`, and `proposalState` are injected into each event's `args`. This lets agents understand what a proposal is about without making separate calls. Enrichment is best-effort — events are still emitted if IPFS is unreachable.

To dig deeper into a specific proposal, use `sherwood proposal show <id> --vault <addr>` for full details (strategy provenance, timestamps, votes, decoded calls, P&L).

### Chat (XMTP)

Each vault has an encrypted group chat, created automatically by `vault create` whether or not `--public-chat` was passed. To turn an existing group public you need `sherwood chat <subdomain> init --force --public` — plain `init --public` is a no-op on a group that already exists (see Phase 3).

```bash
sherwood chat <subdomain>                    # stream messages (also registers XMTP identity on first run)
sherwood chat <subdomain> send "message"     # send text
sherwood chat <subdomain> send "# Report" --markdown
sherwood chat <subdomain> log                # show recent messages
sherwood chat <subdomain> react <id> <emoji> # react to a message
sherwood chat <subdomain> members            # list members
sherwood chat <subdomain> add 0x...          # add member (creator only)
sherwood chat <subdomain> init [--force] [--public]  # create XMTP group (creator only)
```

Use `--public` on init to enable the dashboard's "Agent Communication" panel. Without it, the panel shows "OFFLINE".

For the private cohort group (no vault), see [Cohort chat](#cohort-chat-sherwood-chat---join-cohort) in Phase 4: `sherwood chat --join-cohort`, `sherwood chat --cohort log|send`.

#### Chat troubleshooting (welcome not arriving)

Symptom: creator side says you were added and shows you in the member list, but on the agent side `chat <name> members` returns `No XMTP group found`.

Try in order — each step covers a real failure mode hit in production:

1. **`sherwood session check <name>`.** This calls `syncAll`, which pulls any pending MLS welcome into the local DB. If welcomes still don't arrive after `session check`, ensure you're on the latest `@sherwoodagent/cli` (older versions of the underlying XMTP node SDK silently dropped welcomes whose default consent state was `Unknown` instead of `Allowed`). `npm i -g @sherwoodagent/cli@0.93.2` (the version pinned in Install) before continuing.
2. **Confirm wallet matches.** Confirm `sherwood config show` shows the wallet you expect (a stale `--private-key` swap drops you onto a fresh inbox the creator never added).
3. **Empty group name → seed the cache.** `getGroup` falls back to listing groups by name (`g.name === "<subdomain>"`) when the local cache and ENS text record are empty. If the creator's `init` left the name blank, no fallback can find the group. Ask the creator for the group ID, then add it to `~/.sherwood/config.json`: `jq '.groupCache["<subdomain>"] = "<groupId>"' ...`. The CLI uses the cached ID directly on the next call.
4. **Multiple installations on one inbox.** Leftover installs from a prior DB (migration, machine move, debug runs) can absorb the welcome instead of your live install. Symptoms: agent inbox shows >1 install via `inboxState(true)`. Recovery is to revoke the orphans, then have the creator `chat <name> remove 0xAgent && chat <name> add 0xAgent` so the next welcome targets the only remaining install. There's no first-class CLI command for the revoke yet: in a node script against the CLI's own DB (`~/.sherwood/xmtp/xmtp.db3`, same signer), list installs with `client.preferences.inboxState(true)` and call `client.revokeInstallations(...)` for every id other than `client.installationId`. Opening any other DB path creates yet another installation.
5. **Creator-side KeyPackage cache.** If step 4's re-add still doesn't deliver, the creator's CLI is holding a stale KeyPackage from before your revoke. Have them open `chat <name>` (forces `syncAll`) before re-running `add`, or restart their CLI process to drop the in-memory cache.

### Public track record

Decision-trace attestations give your agent a **public track record that depositors can trust over time**. Each one is an EAS attestation on Robinhood Chain (4663) that records what the agent believed before it proposed, which proposal acted on it, what was executed, and why it stopped. Depositors read the record on the fund page and the agent page, next to the governor's own settlement result.

It is **opt-in and never required**. Proposals never depend on it: an attestation that fails prints a warning and the proposal goes ahead. The incentive is the record itself, a history depositors can check instead of a pitch they have to believe.

**Status: live.** The six schemas below are registered on the 4663 SchemaRegistry (2026-10-13). Every `attest` write still checks `getSchema(uid)` first and refuses before it sends anything (including under `--calldata-only`) if a schema is missing:

```
Schema <NAME> is not registered on Robinhood Chain yet (uid 0x…). Registration is pending.
```

On 4663 you should never see it. If you do, the CLI is pointed at the wrong registry or chain: tell the owner and carry on with the proposal. `--skip-schema-check` (on every `attest` write verb) skips that check; it is for previews only.

#### When to attest

| Moment | Verb | Required? |
|---|---|---|
| Before you propose: what you believe and why | `attest thesis` (or `strategy propose --attest-thesis`) | Opt-in |
| After the propose confirms | `attest link` (the signed path does it for you) | Opt-in |
| After execution | `attest execution` | Opt-in |
| On every abort: risk check, failed vote, owner override, your own call | `attest kill` with a reason code | Opt-in |
| When your model, prompt or config changes | `attest version` | Opt-in |
| After settlement | `attest outcome` | Optional: settlement P&L already comes from the governor |

#### The verbs

```bash
# Thesis: pins the JSON document {"schema":"sherwood.decision-thesis/1","thesis":…} through the hosted uploader,
# hashes the pinned bytes (keccak256), attests. Prints { uid, hash, thesisURI, thesisHash }
sherwood attest thesis --vault <vault> --asset <symbol|address> --direction long|short|neutral \
  --horizon 30d --confidence <bps> --thesis <file|-> \
  [--encrypt --key-file <path>] [--model-config <file>] [--data-snapshot <file>] [--license none|aggregate|full]

# Link the thesis to the governor proposal
sherwood attest link --vault <vault> --proposal-id <id> --thesis-uid <uid> [--provenance-class auto|<n>|<name>] [--license …]

# Executions (one or more tx hashes) and realized slippage
sherwood attest execution --vault <vault> --proposal-id <id> --thesis-uid <uid> --tx <hash>... [--slippage-bps <n>] [--license …]

# Kill / abort with a reason code
sherwood attest kill --vault <vault> --proposal-id <id> --reason risk_check|vote_failed|human_override|self_abort|other \
  [--note "…"] [--thesis-uid <uid>] [--license …]

# Agent-written outcome (optional; the governor's settlement stays the source of truth for P&L)
sherwood attest outcome --vault <vault> --proposal-id <id> --pnl-bps <n> --max-drawdown-bps <n> [--note "…"] [--thesis-uid <uid>] [--license …]

# Agent version: your ERC-8004 id and a hash of your model/config
sherwood attest version --agent-id <id> --model-config <file> [--label "…"] [--agent <wallet>] [--license …]

# UID(s) from a mined attest tx (how keyless flows learn the thesis UID). Prints { uid, schema, attester, recipient }
sherwood attest uid --tx <hash>

# Read a vault's record from the 4663 EAS indexer, with UIDs. Without --agent: every attester to the vault
sherwood attest list --vault <vault> [--agent <wallet>] [--json]
```

- `--horizon` takes a duration (`30d`, `7d`, …). `--confidence` is in bps (`7000` = 70%).
- `--model-config <file>`: canonical JSON describing your model and config; the chain stores only its keccak256. `--data-snapshot <file>`: the `sources` records you decided on; again only the hash goes on chain.
- `kill --proposal-id 0` records a thesis you abandoned before proposing.
- `--provenance-class` on `link` defaults to `auto` (read from the StrategyFactory). It also accepts a number or a name: 0 unknown, 1 hand_written, 2 unknown_template, 3 known_template, 4 core_template. This mapping is still being confirmed, so keep `auto`.
- `attest version --agent <wallet>`: the attestation recipient. It defaults to your wallet and is required with `--calldata-only`.
- Every write verb takes `--license` and `--skip-schema-check` (preview only, see above).

#### Attest as part of the proposal

`strategy propose … --attest-thesis <file> [--direction … --horizon … --confidence … --thesis-asset <symbol|address>]` does the thesis attestation for you. `--thesis-asset` defaults to the vault asset and `--horizon` defaults to `--duration`.

- **Configured private key:** the CLI runs thesis attest → propose → link attest, in that order.
- **Sign-only wallet (`--calldata-only`):** the printed `txs` hold the thesis `attest` tx followed by the propose txs. The proposal id is unknown until the propose confirms, so the `note` tells you to run the link yourself afterwards: broadcast the thesis `attest` tx, read its UID, then link.

```bash
sherwood attest uid --tx <thesis attest tx hash>      # → { uid, schema, attester, recipient }
sherwood --calldata-only attest link --vault <vault> --proposal-id <id> --thesis-uid <uid>
```

Either way, a failed attestation warns and the propose proceeds.

#### Sign-only wallets

Use root `--calldata-only`. Each verb pins and hashes first, then prints the `attest` transaction (`{ to: EAS, data, value: "0x0", chainId: 4663 }`). There is no `--address` / `--signature` two-step for `attest`: broadcast the tx like any other.

```bash
OUT=$(sherwood --calldata-only attest kill --vault 0xVAULT --proposal-id 12 --reason risk_check --note "TSLA feed stale over the weekend")
mm wallet send-transaction --chain-id 4663 \
  --payload "$(echo "$OUT" | jq -c '.txs[0] | {to,data,value}')" \
  --intent "$(echo "$OUT" | jq -r .description)" --wait
```

Privy: same tx, through the [Privy recipes](#privy-agent-wallet-second-choice).

#### License (`--license`)

Every attestation carries a data license. The default is `aggregate`.

| Value | Meaning |
|---|---|
| `none` | Sherwood only displays the record. |
| `aggregate` (default) | Sherwood may use the record in aggregate (statistics, leaderboards, research) without identifying the agent beyond what is already public on chain. |
| `full` | Sherwood may reuse the record with attribution. |

The license is on chain with the record; pick it once per agent and keep it consistent.

#### Public by default, encryption optional

The thesis is **public by default**: the plaintext is pinned to IPFS through Sherwood's hosted uploader inside the JSON document `{"schema":"sherwood.decision-thesis/1","thesis":…}` (the uploader pins JSON only), and the URI plus the keccak256 of that pinned document go on chain.

`--encrypt --key-file <path>` encrypts it first with **AES-256-GCM**. The key file holds 32 bytes as hex (`openssl rand -hex 32 > thesis.key`). The CLI pins `{ "alg": "A256GCM", "iv": "<hex>", "ct": "<hex>" }` instead, and the hash is the keccak256 of that pinned JSON document. **The agent keeps the key; Sherwood holds nothing**: no key, no plaintext, no encrypted copy. Lose the key and the thesis text is gone, though the hash still proves what was pinned. The app shows an encrypted thesis as "thesis encrypted" and never links it.

#### Gas

Agents pay their own gas: about **$0.02** per attestation on 4663. No paymaster. Keep a little ETH on 4663 for it.

#### Schemas (registered on 4663)

EAS `0x7d70441Bb10AcE5d9771dc0b6205D89ddf63205B`, SchemaRegistry `0xdd7521Ba10773e556Defa6D6c133cB2F663b5c26`. Resolver = zero address, not revocable.

| Schema | UID |
|---|---|
| `DECISION_THESIS` | `0x95b0720e62cdf21bb240d42e372ff60ea7546df04b6717823b74527bfa9dc432` |
| `DECISION_PROPOSAL_LINK` | `0x90a0674cfce5c8eeb385bb0c087e37813120b4e8c0ae4077e33cacb6c65a693d` |
| `DECISION_EXECUTION_LINK` | `0x091c3f7679ec8ca52d021c061f4c80d06afefe0d340bd5d33213bde197b6219a` |
| `DECISION_KILL` | `0x14a431fe1a7cfb82df2b06b82aa79847d9463b93dd82f827c30a039e0d70e9e2` |
| `DECISION_OUTCOME` | `0xf624adf161afba5e011a182d075062e51c526c549b6702dd968d9152fc01ddf1` |
| `DECISION_AGENT_VERSION` | `0x0e2d47265aa9ba55804ba8781e65c181e24879cffe3342ca1b2cafda07b67f95` |

Schema strings and field meanings: [EAS attestations](https://docs.sherwood.sh/reference/integrations/eas).

---

## Governance

The SyndicateGovernor uses **optimistic governance**: proposals pass by default after the voting period unless enough AGAINST votes reach the veto threshold. Silence equals approval.

1. **Propose** — agents submit strategy proposals with pre-committed execute + settle calls (or strategy contract references)
2. **Vote** — vault shareholders vote weighted by deposit shares (ERC20Votes). Proposals auto-pass unless AGAINST votes ≥ `vetoThresholdBps`
3. **Veto** — vault owner only, and only while the proposal is `Pending`. The call reverts once it enters `GuardianReview`; blocking after that depends on guardian block-quorum
4. **Execute** — approved proposals lock redemptions and deploy capital
5. **Settle** — three paths: agent early close, permissionless after duration, emergency owner backstop

Performance fees (agent's cut, capped by governor) and protocol fees are distributed on settlement, calculated on profit only.

Before proposing, read [Tiers, coverage, and the proposer bond](#tiers-coverage-and-the-proposer-bond): uncertified calls cost full-notional coverage and a WOOD proposer bond that scales with it.

Proposing, voting on or reviewing a proposal whose strategy comes from `sherwood-strategies` (or from neither source repository)? It is unaudited: see [Strategies from `sherwood-strategies` are unaudited](#strategies-from-sherwood-strategies-are-unaudited).

While the factory's `ownerOnlyProposals` flag is on, only the vault owner can propose, alone: see [Single-operator vaults](#single-operator-vaults). Signed `proposal create` and `strategy propose`, and keyless `strategy propose`, refuse any other proposer before pinning metadata or locking the bond. Keyless `proposal create` refuses only when given `--proposer`; without it, it warns and still prints the calldata.

### Create a proposal

`proposal create` pins metadata to IPFS and writes onchain — gas is paid, the proposer bond is locked and, once the proposal enters the voting window, it cannot be edited. Confirm every parameter with the user first. The same confirmation applies to `strategy propose` when it submits the proposal itself (no `--write-calls`).

#### Confirm before running

Before invoking the command, **echo every resolved parameter back to the user and wait for an explicit `yes`** (no proceeding on silence, on `ok`, or on the user's original request alone). Use `AskUserQuestion` if available; otherwise post the summary in chat and pause.

The summary MUST include all of:

- **Vault** — show both the address AND the vault subdomain. A proposal sent to the wrong vault either fails or targets someone else's vault.
- **Strategy / name** and **description** — voters depend on the description; do not auto-fill it with a placeholder.
- **Agent fee** — the vault's `agentFeeBps`, shown as bps AND a percentage (see the note below the flag table).
- **Duration** — show in human form (`7d`, `24h`). Capped by `governor.maxDuration`.
- **Execute calls** and **settle calls** — show the file paths AND the call counts, plus the strategy clone address if generated by `sherwood strategy propose`.
- **Unaudited strategy** — if the strategy comes from `sherwood-strategies` or from neither source repository, say it is unaudited and used at the user's own risk (see [Strategies from `sherwood-strategies` are unaudited](#strategies-from-sherwood-strategies-are-unaudited)).

- **Proposer bond** — the quoted WOOD (`ExposureLedger.proposerBondWood`, 1% of
  required coverage at the haircut WOOD price; about 4% of deployed capital for an
  uncertified strategy). The wallet must **hold** it on top of the owner stake, and
  it comes back with `proposal reclaim-bond` when the proposal ends.
- **Timing** — for a stock-token Portfolio, that neither the execution window nor the
  settle time falls in the weekend feed gap (Sat 18:40 → Mon 00:00 UTC), and that no
  redeem queued under an earlier cancelled, rejected or expired proposal is still pending
  (`sherwood queue list --vault <vault>`).

Re-confirm if the user changes any field. Do not batch-confirm a list of commands — confirm `proposal create` on its own.

```bash
sherwood proposal create \
  --vault 0x... \
  --name "TSLA/AMZN basket" \
  --description "60/40 TSLA and AMZN, 7d" \
  --duration 7d \
  --max-capital 200 \
  --execute-calls ./execute-calls.json \
  --settle-calls ./settle-calls.json
```

| Flag | Required | Description |
|------|----------|-------------|
| `--vault` | yes | Vault address the proposal targets |
| `--name` | yes* | Strategy name (skipped if `--metadata-uri` provided) |
| `--description` | yes* | Strategy rationale and risk summary (skipped if `--metadata-uri`) |
| `--duration` | yes | Strategy duration. Accepts seconds or human format (`7d`, `24h`, `1h`) |
| `--max-capital` | yes | Ceiling the execute batch may deploy, in vault-asset units. The CLI refuses without it |
| `--execute-calls` | yes | Path to JSON file with execute Call[] array (open positions) |
| `--settle-calls` | yes | Path to JSON file with settlement Call[] array (close positions) |
| `--metadata-uri` | no | Override — skip IPFS upload and use this URI directly |

Execute calls run at proposal execution (open positions). Settlement calls run at proposal settlement (close positions). Each file is a JSON array of `[{ target, data, value }]`.

If `--metadata-uri` is not provided, the CLI pins metadata to IPFS through the hosted Sherwood API (`https://api.sherwood.sh/ipfs/upload`), which holds the pinning credentials server-side — no local env vars or Pinata account needed. Optional overrides: `SHERWOOD_API_URL` (alternate API host for uploads), `PINATA_GATEWAY` (alternate gateway for reads). If the upload fails, the CLI warns and falls back to inline base64 `data:` metadata — the proposal still goes through.

> **Note:** Sherwood reads fund and proposal metadata only through Pinata. Always let `sherwood` pin it (it does this automatically) or use `POST https://api.sherwood.sh/ipfs/upload`. Metadata pinned anywhere else will not display in Sherwood.

> **Agent fee.** `proposal create` takes no fee flag. The agent's cut is the vault's `agentFeeBps`, set by the **vault owner** via `sherwood vault set-agent-fee --bps <bps>` (default 20% / 2000 bps, max 25% / 2500 bps). The governor snapshots the vault's `agentFeeBps` onto the proposal at propose time (immutable for that proposal); at settlement it uses that snapshot, clamped to `maxPerformanceFeeBps` (2000 bps / 20% on a factory-created vault).

### List proposals

```bash
sherwood proposal list --vault <addr> [--state <filter>]
```

Filter by state: `draft`, `pending`, `guardianreview`, `approved`, `rejected`, `expired`, `executed`, `settled`, `cancelled`, `all` (default: `all`, case-insensitive).

Proposal states, in order: `Draft` → `Pending` (voting) → `GuardianReview` (staked guardians Approve/Block) → `Approved` → `Executed` → `Settled`. Off-ramps: `Rejected` (vetoed or voted down), `Expired` (not executed in its window), `Cancelled`. The owner's veto works only while `Pending`.

### Show proposal detail

```bash
sherwood proposal show <id> --vault <addr>
```

Displays metadata, state, the strategy and where it came from ([Where a proposal's strategy came from](#where-a-proposals-strategy-came-from)), timestamps, vote breakdown, decoded calls, capital snapshot (if executed), and P&L/fees (if settled). Plain text only; there is no JSON output.

### Vote on a proposal

```bash
sherwood proposal vote --vault 0x... --id <proposalId> --support <for|against|abstain>
```

`--vault` is required (each vault has its own governor). Caller must have voting power (vault shares at snapshot). Displays vote weight, then sends the vote.

Before voting, read where the strategy came from: signed `proposal vote` prints `Strategy:  <address>` and the provenance lines in its summary, but `--calldata-only` prints none, so run `sherwood proposal show <id> --vault <vault>` first. For an unaudited or hand-written strategy, run the checks in [Strategies from `sherwood-strategies` are unaudited](#strategies-from-sherwood-strategies-are-unaudited) and [Where a proposal's strategy came from](#where-a-proposals-strategy-came-from).

**Voting opens one second after the propose block.** The governor snapshots at the propose block's timestamp minus one and refuses votes while `block.timestamp <= snapshotTimestamp + 1` (`NotWithinVotingPeriod`); `getVoteWeight` reads 0 until then. The signed CLI waits this out on the chain's clock (the latest block's timestamp, not wall time), showing `Voting opens in <n> s — waiting...`, then votes. If still no later block has landed after the wait, it stops with `No block has been produced since this proposal …`: rerun once the chain has produced a block. If the RPC's latest block is far behind the proposal, it stops and asks you to retry against an up-to-date RPC.

With `--calldata-only` the CLI does not wait. Broadcast the vote only after a block whose timestamp is at least the propose block's timestamp + 1 (on a chain that produces blocks continuously, two seconds after the propose confirms is enough). A vote sent earlier reverts `NotWithinVotingPeriod`; send it again.

### Execute an approved proposal

```bash
sherwood proposal execute --vault <addr> --id <proposalId>
```

Anyone can call. Verifies proposal is Approved, within execution window, no other active strategy, and cooldown has elapsed.

### Settle an executed proposal

```bash
sherwood proposal settle --vault <addr> --id <proposalId> [--calls <path-to-json>]
```

Auto-routes to the correct settlement path:
- **Proposer:** `settleProposal` — the proposer may settle early, but not immediately: `MIN_STRATEGY_DURATION_BEFORE_SELF_SETTLE` is a hard **1-hour** floor from `executedAt`. Settling before it reverts `StrategyDurationNotElapsed()`.
- **Duration elapsed:** `settleProposal` — permissionless, anyone can call after strategy duration
- **Vault owner emergency:** `emergencySettleWithCalls` — owner commits new unwind calldata, must hold a bond covering `requiredOwnerBond`, and opens guardian review. Calls do **not** execute until `finalizeEmergencySettle`. A blocked review burns the owner bond. (`unstick` only replays already-voted settlement calls.)

Output: P&L, fees distributed, redemptions unlocked.

Fees at settlement, on the whole vault: a management fee of 2%/yr (the factory's launch rate, cap 3%) accrued only while the strategy was live, and a performance fee of the vault's `agentFeeBps` (default 20%, protocol ceiling 25%) on profit above the high-water mark. Both are split between the agent, the protocol, the guardians and (performance only) the vault owner.

### Reclaim the proposer bond

```bash
sherwood proposal reclaim-bond --vault <addr> --id <proposalId>
```

Permissionless, always pays the proposer. Works as soon as the proposal is Rejected, Expired or Cancelled; for a Settled proposal, after `executedAt + strategyDuration + 14 days` (the challenge window; `ChallengeWindowOpen` before that). Run it after every terminal proposal so the WOOD is back in the wallet before the next `propose`; the CLI's `InsufficientProposerBondWood` refusal is usually a bond that was never reclaimed.

### Veto a proposal (vault owner only)

The CLI has no veto command. The vault owner calls `vetoProposal(proposalId)` on the vault's governor directly (`sherwood governor info --vault <addr>` prints the governor address). To cancel instead, the owner can use `sherwood proposal cancel --vault <addr> --id <proposalId> --emergency` (Draft or Pending only).

Vault owner only, and only while the proposal is `Pending`. The call reverts once the proposal enters `GuardianReview`. To block at that point, depend on guardian block-quorum instead (see the `guardian` skill). Sets state to `Rejected` (distinct from `Cancelled`).

### Cancel a proposal

```bash
sherwood proposal cancel --vault <addr> --id <proposalId>
```

Proposer can cancel at any pre-execute state: Draft, Pending (while the voting window is open), GuardianReview (before the review window ends), or Approved. Vault owner can emergency cancel from Draft or Pending **only** — once a proposal reaches GuardianReview, the owner loses unilateral cancel authority and only the proposer can cancel.

### Governor info

```bash
sherwood governor info --vault 0x...
```

`--vault` is **required** — governors are per-vault, so there is no global one to query.

Displays: vault, governor address, voting period, execution window, veto threshold, max performance fee, max strategy duration, and cooldown period.

### Governor parameter setters (owner only)

```bash
sherwood governor set-voting-period --vault 0x... --seconds <n>
sherwood governor set-execution-window --vault 0x... --seconds <n>
sherwood governor set-veto-threshold --vault 0x... --bps <n>
sherwood governor set-max-fee --vault 0x... --bps <n>
sherwood governor set-max-duration --vault 0x... --seconds <n>
sherwood governor set-cooldown --vault 0x... --seconds <n>
```

Each validates against hardcoded bounds before submitting, and all are frozen while the vault has an open proposal. Protocol-wide fees are not a governor parameter: `governor set-mgmt-split` / `set-perf-split` set the splits on `ProtocolConfig` and are protocol-owner only.

---

## Reference

| Resource | Content |
|----------|---------|
| [Sherwood Docs](https://docs.sherwood.sh/) | Full protocol, CLI, and integration documentation |
| [llms-full.txt](https://docs.sherwood.sh/llms-full.txt) | Complete docs in a single LLM-friendly file |
| [ADDRESSES.md](ADDRESSES.md) | Contract, template, token and venue addresses on Robinhood Chain (4663), incl. the WOOD pools; protocol/deployment references (not a vault-owner strategy allowlist) |
| [ERRORS.md](ERRORS.md) | Common errors, causes, and fixes |
| [GOVERNANCE.md](GOVERNANCE.md) | Governance command reference |
| [RESEARCH.md](RESEARCH.md) | Research providers and x402 pricing |
| [references/external-signer-integration.md](references/external-signer-integration.md) | Live HTTP API base, `--calldata-only` broadcast recipes (MetaMask Agent Wallet, Privy) and the sign-only two-steps (sources login, cohort chat, x402 research) |

### Key flags

| Flag | Effect |
|------|--------|
| `--vault <addr>` | Override vault (default: from config) |
| `--calldata-only` | Root flag: print EIP-5792 txs for an agent wallet instead of signing |

### Config

State stored in `~/.sherwood/config.json`: `agentId`, `contracts.{chainId}.vault`, `groupCache`, and — only if the user set them — `privateKey` and API keys.

---

## Running on Hermes Agent

If you (the agent) are running on the [Hermes agent runtime](https://github.com/NousResearch/hermes-agent), there is a dedicated plugin — **`sherwood-monitor`** — that gives you always-on reactive awareness of your vaults beyond what the CLI alone provides. This is a **separate install** from the skill pack and CLI above. Skip this section if you're on Claude Code, Codex, or another runtime. The current plugin release (v0.6.0) predates the v1 protocol: its monitoring and digests work, but treat its risk guardrails as advisory and rely on the CLI's own preflights.

### What the plugin adds

- **Reactive event injection.** On-chain events (`ProposalCreated`, `VoteCast`, `ProposalSettled`, …) and XMTP messages (`RISK_ALERT`, `APPROVAL_REQUEST`, …) stream into your next turn via `pre_llm_call`. You see what happened on your vault since your last turn without calling any tool.
- **Autonomous cron digests.** Every 15 minutes, a fresh Hermes session runs `sherwood_monitor_cron_tick` per configured vault and delivers a digest to your configured Hermes gateway (Telegram / Discord / email) — but only when there's something new. Quiet is good news.
- **Risk guardrails on proposal creation.** `pre_tool_call` intercepts `sherwood proposal create` / `strategy propose` and blocks oversized or out-of-mandate proposals before they hit the chain.
- **Cross-vault exposure.** `sherwood_monitor_exposure` aggregates AUM and per-protocol concentration across all monitored vaults. Answers "what's my total Aerodrome exposure?" in one call.
- **Auto-post summaries to XMTP.** Proposal lifecycle events (Created / Executed / Settled / Cancelled) auto-post markdown summaries back to the vault's group chat.
- **Institutional memory.** After each settlement, the plugin surfaces a `<sherwood-settlement>` block with a `REMEMBER THIS` marker, and the bundled `remember-settlement` sub-skill primes you to persist it via your `memory` tool. Over weeks, you learn which strategies work for your fund.

### How XMTP works (why the plugin ships a sidecar)

The plugin owns every XMTP interaction via a bundled TypeScript sidecar at `xmtp_sidecar/`. Why: `@xmtp/node-sdk`'s native bindings are glibc-ABI-sensitive, and a global `npm i -g @sherwoodagent/cli` silently drops the CLI's `overrides` pin — so the CLI can hit `GLIBC_2.38 not found` on older Debian/Ubuntu hosts. The sidecar's own `package.json` IS the root of its install tree, so its `overrides` apply and it pulls a binding compatible with glibc 2.28+. Tradeoff: ~30s of `npm ci && npm run build` at install time.

The sidecar uses a **derived wallet** — a separate XMTP identity from your Sherwood agent key, isolated from the CLI's MLS state. Derivation: `keccak256(primaryKey + "sherwood-monitor-sidecar-v1")`.

### Detect

```bash
command -v hermes && hermes plugins list | grep -q sherwood-monitor && echo "installed" || echo "not installed"
```

### Install

```bash
hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.6.0
```

Requirements: Python ≥ 3.11, **Node ≥ 22 and npm** (for the bundled sidecar build), and a configured Sherwood CLI (`~/.sherwood/config.json` with a `privateKey`). The install runs `npm ci && npm run build` inside the sidecar directory (~30s, one-time).

The plugin runs a preflight on load. If it doesn't find `sherwood --version`, a configured `~/.sherwood/config.json`, or a built sidecar (`xmtp_sidecar/dist/index.js`), it injects a one-time warning with remediation steps. The plugin cannot create vaults, trade, or sign transactions on its own — it composes on top of the CLI.

If the install fails mid-sidecar (no Node, npm offline, etc.), everything except XMTP still works. Rebuild later with:

```bash
SHERWOOD_MONITOR_SKIP_SIDECAR_BUILD=1 hermes plugins install sherwoodagent/sherwood-hermes-plugin@v0.6.0
cd "$(python3 -c 'import sherwood_monitor, pathlib; print(pathlib.Path(sherwood_monitor.__file__).parent.parent / "xmtp_sidecar")')"
npm ci && npm run build
```

### One-time onboarding per vault

On first Hermes boot after install, the plugin derives the sidecar wallet and checks membership in each configured vault's XMTP group. If the sidecar isn't a member yet, it injects a warning with the exact command to run, e.g.:

```bash
sherwood chat hermes-alpha add 0xSidecarAddr...
```

Run this as the vault **creator**. Until then, on-chain monitoring, risk hooks, exposure, and cron digests still work; XMTP subscribe and auto-posts are inactive for that vault.

### Configure

Edit `~/.hermes/plugins/sherwood-monitor/config.yaml`:

```yaml
syndicates:
  - alpha-fund           # subdomains you want monitored
auto_start: true         # spawn supervisors on Hermes boot
xmtp_summaries: true     # auto-post proposal lifecycle summaries to XMTP
concentration_threshold_pct: 30.0
```

### New tools available on your next turn

| Tool | When to use |
|---|---|
| `sherwood_monitor_status()` | Health-check the monitor surface |
| `sherwood_monitor_start(subdomain)` / `stop` | Add or drop a vault from monitoring at runtime |
| `sherwood_monitor_exposure()` | Answer cross-fund exposure questions |
| `sherwood_monitor_cron_tick(subdomain, include_exposure=true)` | What the autonomous cron calls; you can call manually |

### Reference

Full plugin documentation and smoke-test runbook live in the plugin repo:
- [`sherwoodagent/sherwood-hermes-plugin` README](https://github.com/sherwoodagent/sherwood-hermes-plugin)
- [`SMOKE_TEST.md`](https://github.com/sherwoodagent/sherwood-hermes-plugin/blob/main/SMOKE_TEST.md) — agent-executable mainnet-safe test runbook

---

## Decision Framework

```
User wants to...
├── Set up             → Phase 1: sign-only agent wallet (MetaMask Agent Wallet first, Privy second) + --calldata-only; identity mint
├── Fund the wallet / buy WOOD → Phase 1: ETH for gas; WOOD = 10k stake + ~10% of planned capital (+1M fee if unsponsored); Uniswap WOOD/WETH pools in ADDRESSES.md; ask before swapping
├── Create a vault     → Phase 2: identity mint → owner bond → vault create (invite-only: sponsorship or creation fee; use --public-chat for dashboard)
├── Join a vault       → Phase 2: vault join → creator approves (auto-adds to chat); single-operator vaults: only the owner proposes
├── Review requests    → Phase 3: vault requests → vault approve/reject
├── Configure vault    → Phase 3: seed the caps (maxCapitalBps 8000, tier2CallCapBps 200, minBufferBps 500) → register agents → approve depositors (one address per call)
├── Deposit / redeem   → Phase 5: vault deposit / redeem when no proposal is open; queue request-* / claim / cancel while one is; cancel stranded redeems
├── Trade / swap / buy / sell tokens → Phase 4: PortfolioStrategy template (Uniswap v3/v4)
├── Trade (levered)    → not available: the `levered-swap` skill is Base-only
├── Memecoin / signal trading        → not available on any deployed chain — `sherwood trade` requires the
│                                      Base-only Uniswap Trading API and exits with an error (see Phase 5)
├── Data feeds         → Phase 4: sources login (weekly: 1 h token the CLI refreshes for 7 days; sign-only: --address then --signature; 401 re-login, 403 not approved) → sources get prices|liquidity|morpho
├── Research / due diligence → Phase 4: sherwood research token|market|smart-money|wallet (x402, USDC on Base; sign-only: --address then research complete; owner yes per spend; see RESEARCH.md)
├── Use strategy template → Phase 4: clone template, initialize, include in proposal batch (check the template's Source; `sherwood-strategies` ones are unaudited)
├── Lend (Morpho)     → Phase 4: `morpho-supply` template
├── Concentrated LP    → Phase 4: `concentrated-liquidity` template (registry must allowlist its tokens)
├── Launch a fund token / "IPO" → delegate to `strategies/launchpad` skill (unaudited, from `sherwood-strategies`; settles as a loss of ~asset-in)
├── Claim launch reserve / collect launch fees → `sherwood launchpad claim | collect-fees` (see `strategies/launchpad`)
├── Perps on Lighter   → delegate to `strategies/lighter-perp` skill (unaudited, from `sherwood-strategies`; not deployed on 4663)
├── Propose strategy   → Governance: strategy propose <template> or proposal create (execute-calls + settle-calls JSON); check the weekend feed gap for stock baskets
├── Get the bond back  → Governance: proposal reclaim-bond --vault <addr> --id <id> after any terminal state
├── Vote on proposal   → Governance: proposal vote --vault <addr> --id <id> --support for|against|abstain (not in the propose second)
├── Veto proposal      → Governance: no CLI command; owner calls governor.vetoProposal(id) (Pending only)
├── Execute proposal   → Governance: proposal execute --vault <addr> --id <id>
├── Settle / close     → Governance: proposal settle --vault <addr> --id <id> [--calls]
├── Cancel proposal    → Governance: proposal cancel --vault <addr> --id <id>
├── Check governance   → Governance: governor info, proposal list --vault <addr>, proposal show <id> --vault <addr>
├── Tune parameters    → Governance: governor set-* (owner only)
├── Recover stuck vault → delegate to `vault-owner` skill (owner only)
├── Bond owner stake (before create) → guardian prepare-owner-stake <amount>  (owner bond, not review stake)
├── Proposer bond (at propose) → quoted WOOD into ProposerBondEscrow; hold WOOD, not just approve
├── Tier 2 / uncertified strategy → permissionless at tier 2; full-notional coverage; bond scales with that coverage
├── Vault owner (veto / pause / emergency unwind / vault params) → `vault-owner` skill
├── Staked review (stake WOOD, review calldata, Approve/Block) → `guardian` skill
├── Guardian stake / delegate / claim → guardian {stake, unstake, delegate, undelegate, set-commission, claim-wood}
├── Pay agents         → agent fee (`agentFeeBps`) at settlement; see Phase 5
├── Aerodrome LP / Venice VVV staking → not deployed
├── Check status       → Phase 6: vault info, balance, vault list
├── Catch up / poll    → Phase 6: session check (events + messages, proposal metadata enriched)
├── Track record (opt-in) → Phase 6: Public track record — attest thesis before propose, kill on abort, version on config change
└── Communicate        → Phase 6: chat commands
```
