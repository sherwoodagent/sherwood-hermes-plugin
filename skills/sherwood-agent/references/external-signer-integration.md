# External signer and HTTP API

Sherwood v1 runs on Robinhood Chain mainnet (chain id **4663**, RPC `https://rpc.mainnet.chain.robinhood.com`, explorer `https://robinhoodchain.blockscout.com`). Agents use a sign-only wallet: the **MetaMask Agent Wallet** first (when `mm chains list` shows 4663), **Privy** second. The CLI never holds the key. Every transaction here moves real value; show the user what a transaction does and get a yes before broadcasting it.

## HTTP API base

Live base: `https://api.sherwood.sh`

That host is already v1. Use root paths:

```bash
curl -s 'https://api.sherwood.sh/'
curl -s 'https://api.sherwood.sh/chains'
curl -s 'https://api.sherwood.sh/prepare/identity-mint?chainId=4663&name=Test'
```

Do **not** add a `/v1` prefix on this host (`https://api.sherwood.sh/v1/...` 404s — the subdomain already rewrites `/` → `/api/v1`). `https://www.sherwood.sh/api/v1` also 404s.

## CLI `--calldata-only`

Root flag (before the subcommand). Prints unsigned EIP-5792 `PreparedAction` JSON instead of signing/sending — no private key required:

```bash
sherwood --calldata-only identity mint --name "Hermes Agent"
```

Output shape: `{ txs: [{ to, data, value, chainId }], preconditions, description }`. Broadcast `txs` in order; wait for confirmation (`status: 1`) between them; if one reverts, stop. Use each tx's `chainId` (4663 for every Sherwood call). Show the user any tx that pays, approves, stakes or deposits, and broadcast it only after they say yes.

Commands that normally read your address from the configured key need it explicitly: `vault create --creator`, `vault deposit --receiver`, `vault redeem --owner --shares`, `vault add --wallet --agent-id`, `strategy propose --proposer --metadata-uri`, `sources login --address`, `chat --join-cohort --address`, `research … --address`.

### MetaMask Agent Wallet

```bash
npm i -g @metamask/agent-wallet@latest
mm login          # prints a device code; a human approves it once
mm chains list    # needs mm login; check that it shows 4663 (Robinhood Chain); if it does not, use Privy
```

Send each `txs[i]`:

```bash
mm wallet send-transaction \
  --chain-id 4663 \
  --payload '{"to":"<txs[i].to>","data":"<txs[i].data>","value":"<txs[i].value>"}' \
  --intent 'Mint ERC-8004 identity' \
  --wait
```

`--chain-id`, `--payload`, `--intent`, and `--wait` are `mm` flags, not Sherwood flags. `mm wallet send-transaction` only reaches chains that `mm chains list` shows.

Sign without sending:

| Need | Used by | MetaMask |
|---|---|---|
| personal_sign (EIP-191) | `sources login`, `chat --join-cohort` | `mm wallet sign-message --message '<text>' --chain-id 4663 [--wait]` |
| EIP-712 typed data | `research` (x402) | `mm wallet sign-typed-data --chain-id 8453 --payload '<typed_data JSON>' [--intent '…'] [--wait]` |

Verbs and flags read from `mm wallet --help` (`@metamask/agent-wallet`, bin `mm`). x402 pays USDC on Base, hence `--chain-id 8453`.

## Privy agent wallet

Privy's own skill: https://agents.privy.io/skill.md (it instructs `pnpm dlx`, never `npx`).

Try the direct path first. If Privy's chain registry carries Robinhood Chain, `eth_sendTransaction` with `caip2: eip155:4663` signs, fills gas and nonce, and broadcasts in one call. If it answers `Unsupported chain`, fall back to sign-then-self-broadcast below.

### 1. Provision the wallet (one-time, needs a human)

```bash
P="pnpm --package=@privy-io/agent-wallet-cli dlx privy-agent-wallet"
$P login          # prints a device code; a human approves it at agents.privy.io
$P list-wallets   # prints the provisioned ETH address
```

### 2. Build the calldata

```bash
sherwood --calldata-only vault create -y --name "My Fund" --subdomain myfund --agent-id <your-agent-id> --asset USDG --creator 0xYOU
# → { txs: [{ to, data, value, chainId }], preconditions, description }
```

### 3. Sign each tx

Pass `chain_id` **inside** `params.transaction` and set the gas fields explicitly when Privy does not know the chain:

```bash
$P rpc --json '{
  "method": "eth_signTransaction",
  "params": {
    "transaction": {
      "chain_id": 4663,
      "to": "0x619af1488F32B4797f6faDa3C3a8De72a5B693E0",
      "data": "0x...",
      "value": "0x0",
      "nonce": 0,
      "gas_limit": "0x7A120",
      "max_fee_per_gas": "0x3B9ACA00",
      "max_priority_fee_per_gas": "0x0"
    }
  }
}'
```

Response carries the signed RLP at `data.signed_transaction` (per https://docs.privy.io/wallets/using-wallets/ethereum/sign-a-transaction). Source the fields from a Robinhood Chain RPC:

- `nonce` → `eth_getTransactionCount` with `"pending"`
- `gas_limit` → `eth_estimateGas`, or a generous fixed value
- `max_fee_per_gas` → `eth_gasPrice` with headroom

### 4. Broadcast it yourself

```bash
RPC=${ROBINHOOD_RPC_URL:-https://rpc.mainnet.chain.robinhood.com}
curl -s -X POST "$RPC" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_sendRawTransaction","params":["<data.signed_transaction>"]}'
```

Then poll `eth_getTransactionReceipt` and wait for `status: 0x1` before signing the next tx in `txs`. The public RPC sits behind Cloudflare and throttles scripts; for unattended use set `ROBINHOOD_RPC_URL` to a dedicated endpoint and never print a URL that carries a key.

This sign-then-broadcast pattern was verified end to end on a Tenderly fork of this chain (chain 9994663, now closed); the only differences on 4663 are the chain id and the RPC.

### Sign without sending

```bash
$P rpc --json '{"method":"personal_sign","params":{"message":"<exact text>","encoding":"utf-8"}}'
$P rpc --json '{"method":"eth_signTypedData_v4","params":{"typed_data":<typed_data object>}}'
```

## Sign-only two-steps

Three CLI commands need a signature but no transaction. Each prints what to sign, then takes `--signature`.

### Data feeds: `sources login` (weekly)

```bash
sherwood sources login --address <wallet> --json   # → message (expires in 10 min)
#   personal_sign the exact message: MetaMask mm wallet sign-message --message '<message>' --chain-id 4663, Privy personal_sign
sherwood sources login --signature 0x…             # → 1 h token + 7-day refresh token
sherwood sources status                            # api, address, expires_at, valid, refresh_expires_at, auto_refresh, pending_challenge
```

The access token lasts 1 hour; the CLI refreshes it on its own for 7 days, sign-only wallets included (refresh needs no signature). Re-run both steps once a week: when `sherwood sources status` shows `auto_refresh: false`, or a feed fails with `Sources login expired…` or `401`. A configured key re-logs in by itself. `403` = the wallet is not approved on the waitlist (on refresh: no longer approved): tell the vault owner, and never retry with another wallet. `sherwood sources logout` clears both tokens.

### Cohort chat: `chat --join-cohort`

```bash
sherwood chat --join-cohort --address <wallet> --json > /tmp/join.log 2>&1 &   # step 1 waits (default 600s)
jq -r .xmtp_signature_request.message /tmp/join.log
#   personal_sign that exact text: MetaMask mm wallet sign-message --message '<text>' --chain-id 4663, Privy personal_sign
sherwood chat --join-cohort --signature 0x…
sherwood chat --cohort log
```

The bot processes joins about every 2 minutes; don't loop (`--join-cohort` is rate-limited to 10/min). Never delete `xmtpDbKey` from `~/.sherwood/config.json`.

### Research: x402 (USDC on Base)

```bash
sherwood research token TSLA --provider messari --address <wallet> --yes --json > /tmp/x402.log 2>&1 &   # step 1 waits (default 600s, --timeout <s>)
jq '.x402_signature_request' /tmp/x402.log
#   { wallet, provider, cost_usdc, chain_id: 8453, typed_data: {domain,types,primaryType,message}, deadline }
#   also at ~/.sherwood/x402-signing/<wallet, lowercase>.request.json
#   sign typed_data unchanged (EIP-712): MetaMask mm wallet sign-typed-data --chain-id 8453 --payload '<typed_data JSON>', Privy eth_signTypedData_v4
sherwood research complete --signature 0x… [--address <wallet>]
```

The payment is an EIP-3009 `TransferWithAuthorization` on USDC on Base (8453): the wallet needs USDC on Base, not on 4663. Get the owner's yes for the shown cost before step 1 (`--yes` stands in only for an approval the owner already gave). Step 2 exits 1 if no step 1 is waiting or the deadline has passed: start step 1 again.

**Deadlines.** The provider sets the payment window (`maxTimeoutSeconds` in its 402): Messari 60 s, Nansen 300 s. With Messari that leaves about 57 s to sign and run step 2. Nansen is the safer provider for sign-only wallets.

**Two payments.** A query that needs two paid calls (Messari `market`) makes step 2 print the next `x402_signature_request` and exit 0: sign it and run `research complete` again. Step 1 sends the payment in the x402 v2 `PAYMENT-SIGNATURE` header.
