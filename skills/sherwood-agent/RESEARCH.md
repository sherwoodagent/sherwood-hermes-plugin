# Research Reference

Before proposing or executing a strategy, agents should research the target assets. Research queries are paid per call via x402 in **USDC on Base (chain 8453)** from the agent's wallet — no API keys, no vault funds, nothing on 4663. Fund the paying wallet with USDC on Base; it needs no ETH there.

**Owner approval before each spend.** Show the cost, provider and paying wallet, and wait for the owner's yes before every call. `--yes` skips only the CLI's prompt, and only for spend the owner already approved.

## Sign-only wallets: the two-step

x402 signs EIP-712 typed data (EIP-3009 `TransferWithAuthorization`), so the wallet must support `eth_signTypedData_v4`: Privy does (`$P rpc`), and the MetaMask Agent Wallet does through `mm wallet sign-typed-data --chain-id 8453 --payload '<typed_data JSON>'` (USDC on Base, so `--chain-id 8453`). No private key is needed.

```bash
# Step 1: confirms cost, catches the 402, prints the typed data, then waits (default 600s, --timeout <s>)
sherwood research token TSLA --provider messari --address <wallet> --yes --json > /tmp/x402.log 2>&1 &
jq '.x402_signature_request' /tmp/x402.log
#   { wallet, provider, cost_usdc, chain_id: 8453, typed_data: {domain,types,primaryType,message}, deadline }
#   also at ~/.sherwood/x402-signing/<wallet, lowercase>.request.json
#   sign typed_data unchanged: MetaMask mm wallet sign-typed-data --chain-id 8453 --payload '<typed_data JSON>' --wait; Privy eth_signTypedData_v4
# Step 2: hands the signature to step 1, which pays, retries and prints the result
sherwood research complete --signature 0x… [--address <wallet>]
```

Step 2 exits 1 with the reason when no step 1 is waiting or the deadline has passed. Under `--calldata-only` without `--address`, `research` refuses and names this two-step. A configured private key pays in one step (the commands below).

**Deadlines.** The provider sets the payment window (`maxTimeoutSeconds` in its 402): Messari 60 s, Nansen 300 s. With Messari that leaves about 57 s to sign and run step 2. Nansen is the safer provider for sign-only wallets.

**Two payments.** A query that needs two paid calls (Messari `market`) makes step 2 print the next `x402_signature_request` and exit 0: sign it and run `research complete` again. Step 1 sends the payment in the x402 v2 `PAYMENT-SIGNATURE` header.

## Commands

```bash
# Token due diligence
sherwood research token ETH --provider messari
sherwood research token 0xABC... --provider nansen

# Smart money analysis
sherwood research smart-money --token WETH --provider nansen

# Market overview
sherwood research market ETH --provider messari

# Wallet due diligence (e.g. before approving an agent)
sherwood research wallet 0xDEF... --provider nansen
```

Add `--post <syndicate>` to record research on-chain: pins the full result to IPFS, creates an EAS attestation (provider, query, cost, IPFS URI), and posts a notification to the syndicate XMTP chat.

```bash
sherwood research token WETH --provider nansen --post alpha
```

Add `--yes` to skip the cost confirmation prompt, only for spend the owner has already approved.

## Sign-only wallets: pay Messari directly

Use the CLI two-step above when you can. If it can't run (no CLI in the harness, or a Messari endpoint `research` doesn't wrap), a sign-only agent (MetaMask Agent Wallet, Privy, or another external signer) can call the Messari API itself and pay each call with a signed EIP-3009 authorization. The wallet only signs; it sends no transaction. The facilitator settles the USDC transfer. Verified end to end with the MetaMask agent wallet (`mm`), with Messari as the provider.

Keep a little USDC on Base (chain 8453) in the agent wallet. Each call costs real money: show the owner the cost and the paying wallet and wait for their yes, as with the CLI.

1. **Call** the Messari endpoint with no payment, e.g. `GET https://api.messari.io/metrics/v2/assets/details?assetIDs=bitcoin`. Expect HTTP `402` with an x402 v2 body (also base64 in the `payment-required` header). From `accepts`, pick the entry with `scheme: "exact"` and `network: "eip155:8453"` (Base USDC). Read `amount`, `payTo`, `asset`, `maxTimeoutSeconds` and the EIP-712 domain (`extra.name`, `extra.version`) from that entry. **Never hardcode a price or an address**; they come from the 402. Asset search (`/metrics/v2/assets?search=…`) is free and returns 200.
2. **Build** the EIP-712 typed data: `primaryType: "TransferWithAuthorization"`, domain `{name: extra.name, version: extra.version, chainId: 8453, verifyingContract: asset}`, and message `{from: <agent wallet>, to: payTo, value: amount, validAfter: now - 600, validBefore: now + maxTimeoutSeconds, nonce: <random 32 bytes, 0x-hex>}` (unix seconds, numbers as decimal strings). Include `EIP712Domain` (name/string, version/string, chainId/uint256, verifyingContract/address) and `TransferWithAuthorization` (from/address, to/address, value/uint256, validAfter/uint256, validBefore/uint256, nonce/bytes32) in `types`.
3. **Sign**: `mm wallet sign-typed-data --chain-id 8453 --payload '<typed data JSON>' --wait --json`. In `--json` mode, mm can print `{"_notice": …}` lines before the result, so take the `signature` from the final line. If a notice has `kind: "AWAITING_MFA"`, forward its `message` (and any approval link or request ID) to the owner right away. The signature is only useful if approval lands inside the window. In guard mode with no MFA, signing takes a few seconds.
4. **Retry immediately** with the same request plus the payment header: base64 of the JSON `{x402Version: 2, resource: <from the 402>, accepted: <the chosen accepts entry>, payload: {signature, authorization: <the message from step 2>}}`, sent as `PAYMENT-SIGNATURE` (v2; also send it as `X-PAYMENT` for v1 servers). Expect `200`. Steps 2–4 must finish inside `maxTimeoutSeconds` (60s today). If the window passes, the authorization has expired and nothing was paid: build a fresh one (new nonce, new timestamps) and retry once.

A second `402` after you pay means the payment was rejected. Its body or `payment-required` header says why (e.g. insufficient USDC or an expired authorization). Fix that before trying again, and don't loop.

## Signal-Based Trading (not available on Robinhood chains)

The `sherwood trade` commands (`scan` / `buy` / `sell` / `positions` / `monitor`) compose research providers with Venice inference for signal-driven memecoin trading via the Uniswap Trading API, which covers Base only. Sherwood runs on Robinhood Chain (4663), which is not Base, so every `trade` subcommand exits with an error. Do not use them. The full workflow (documented in the `strategies/memecoin-alpha` skill) is parked until Sherwood deploys on a chain the Trading API covers.

## Providers & x402 pricing

`sherwood providers` lists the providers the CLI can execute, including the two research providers below (chain-agnostic). Vault swaps on 4663 go through the PortfolioStrategy's Uniswap adapter, not a provider.

**Messari** — market metrics, asset profiles, on-chain analytics (34,000+ assets)
- Asset details / ROI / ATH: **$0.10**
- Timeseries (1d): **$0.15** | Timeseries (1h): **$0.18**
- Market / exchange metrics: **$0.35**
- News / signals: **$0.55**
- Full pricing: https://docs.messari.io/api-reference/x402-payments

**Nansen** — token screener, smart money flows, wallet profiler (18+ chains)
- Basic (token screener, balances, PnL, DEX trades, flows): **$0.01**
- Premium (counterparties, holders, leaderboards): **$0.05**
- Smart money (netflow, holdings, SM DEX trades): **$0.05** (+$0.01 if resolving symbol → address)
- Full pricing: https://docs.nansen.ai/getting-started/x402-payments
