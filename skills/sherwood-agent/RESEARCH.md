# Research Reference

Before proposing or executing a strategy, agents should research the target assets. Messari and Nansen queries (`sherwood research`) are paid per call via x402 in **USDC on Base (chain 8453)** from the agent's wallet — no API keys, no vault funds, nothing on 4663. Fund the paying wallet with USDC on Base; it needs no ETH there. News from FLASH (`sherwood news`) works the other way round: free during the pilot, no x402 step, but it needs your own FLASH API key. See [News (FLASH)](#news-flash).

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

## News (FLASH)

`sherwood news` (CLI 0.100.0 or later) returns news items from [FLASH](https://flash.im) for a Robinhood stock token, a crypto asset or a free-text query. FLASH covers equities, macro and crypto. Each item carries an impact tier from 2 to 5, a headline, the publisher and a link to the original article, ticker tags with sentiment, and FLASH's own summary and "why it matters".

**Free during the pilot, no x402 step.** FLASH takes no payment: nothing is signed, no USDC is spent, and there is no cost prompt (the CLI prints `Est. cost: free (pilot)`). The query runs without a wallet; only `--post` needs one.

**Key.** Get your own key at https://app.flash.im/terminal (self-serve, free during the pilot). Set it one of two ways:

```bash
sherwood config set --flash-api-key <key>    # saved in ~/.sherwood/config.json; wins over the env var
export FLASH_API_KEY=<key>                   # used when the config holds no key
```

Without a key, `news` exits 1 with:

```
No FLASH API key. Get one (free during the beta) at https://app.flash.im/terminal, then set FLASH_API_KEY or run `sherwood config set --flash-api-key <key>`.
```

A rejected key (HTTP 401 or 403) names the same two settings and the sign-up URL. Never post the key in chat or pin it.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--asset <symbol\|name>` | none | A Robinhood stock token by symbol or name (`NFLX`, `Netflix`): asks FLASH for that token's keywords, category `economy`. Anything not in the stock-token universe is treated as a crypto asset: the input is the keyword, category `crypto`. Pass exactly one of `--asset` and `--query` |
| `--query <text>` | none | Free text. With `--match keyword`, comma-separated keywords, at most 30. FLASH matches the entities it tags items with (tickers, company and institution names such as `Netflix`, `NFLX`, `Federal Reserve`), not article text: `Treasury` or `rate cut` return nothing even when headlines mention them |
| `--match <keyword\|sentence>` | `keyword` | How `--query` is matched. `sentence` sends the text whole and is logged by FLASH (below). Ignored with `--asset` |
| `--days <n>` | `7` | Look back this many days |
| `--impact <n>` | `2` | Lowest impact tier, 2 to 5. `3` means tiers 3, 4 and 5 |
| `--limit <n>` | `20` | Most items to return, 1 to 100. With `--post`, at most 20: a higher value is refused |
| `--category <economy\|crypto\|auto>` | `auto` | FLASH category. `auto`: `economy` for a stock token, `crypto` for any other `--asset`, no category for `--query` |
| `--provider <name>` | `flash` | News provider. FLASH is the only one |
| `--json` | off | Print the `sherwood/research/v1` document that `--post` pins (its `data` is the FLASH record) instead of the list. Under a keyless `--post` it goes to stderr, because stdout carries the attest tx |
| `--post <vault>` | none | Pin the record to IPFS, attest `X402_RESEARCH` to the vault the name resolves to, and post the top 3 headlines to its chat. Needs a configured key, or root `--calldata-only`, which prints the attest tx and skips the chat |
| `--yes` | off | Skip the `--match sentence` confirmation |
| `--address <wallet>` | none | Refused: FLASH takes no payment. A sign-only wallet posts with root `--calldata-only` |
| `--timeout <s>` | `600` | Unused by FLASH (it is the x402 two-step's wait for the paid providers) |

**Output.** Items print newest first: UTC time, impact pips (`●●●●○` is impact 4), headline, source name and article URL; ticker tags with sentiment on the next line; `whyItMatters` indented under impact 4 and 5; the FLASH credit last, once. When FLASH has nothing, the CLI prints `FLASH has no items for <target> in the last <n> days` and exits 0 (with `--json` the record still prints, `matchStatus: "no_match"` and empty `items`), so a script can tell no news from a failure.

Sample output, taken live on 2026-10-28 (items change with the news; ticker sentiment and `whyItMatters` print on the items that carry them):

```
$ sherwood news --asset NFLX --limit 3

Research Query
────────────────────────────────────────
  Provider:  flash
  Type:      news
  Target:    NFLX
  Est. cost: free (pilot)

✔ news data received from flash

2026-10-28 13:01Z  ●●○○○  Bessent trade adviser Eric Ettorre leaves Treasury for Netflix — Axios  https://www.axios.com/2026/10/28/bessent-treasury-trade-adviser-netflix
    NFLX

2026-10-28 10:45Z  ●●○○○  Polymarket bettors give YouTube a 19% chance of topping $12 billion in quarterly ads — Benzinga  https://www.benzinga.com/markets/tech/26/10/62647323/youtube-ad-revenue-netflix-earnings-alphabet
    NFLX

2026-10-27 12:30Z  ●●○○○  Opinion: Companies should treat departing employees with greater dignity — Fortune  https://fortune.com/2026/10/27/netflix-layoffs-corporate-america-getting-wrong-dignity/
    NFLX

Powered by FLASH  https://flash.im
```

### `--match sentence` is logged

FLASH logs sentence queries together with your IP address and user agent. Before sending one, the CLI prints `FLASH logs sentence queries with your IP and user agent; use --match keyword to avoid sending the thesis text.` and asks `Send this sentence to FLASH?` (default no). `--yes` skips the question. Do not send thesis text, exact levels or the owner's management plan as a sentence. Ask with keywords, and pass `--yes` only when the owner has accepted the logging.

### Attribution

Wherever you show FLASH items (vault chat, a thesis, a proposal description, a report, a UI):

- show each item's source name (`source.name`) and link the original article (`sourceUrl`); the headline is the link text;
- add one "Powered by FLASH" credit linking https://flash.im per list, not per item.

`summary` and `whyItMatters` are FLASH's own text and may be quoted in full. The CLI's list, its vault-chat post and the Sources section it appends to a default thesis already follow these rules; anything you write from a record must too.

### Record news on chain

`--post <vault>` pins the record and attests it, which makes it citable. With a configured key:

```bash
sherwood news --asset NFLX --limit 10 --post <vault-name>
#   ✔ Pinned to IPFS: ipfs://…
#   ✔ Attested: https://api.sherwood.sh/attestation/<uid>
#   ✔ Posted to vault chat
```

The permalink page shows the headlines with their sources and the "Powered by FLASH" credit. With a sign-only wallet, the CLI pins the record and prints the `X402_RESEARCH` attest tx instead of posting to chat:

```bash
sherwood --calldata-only news --asset NFLX --limit 10 --post <vault-name>
# broadcast the printed tx (sign-then-broadcast loop in SKILL.md), then read the research UID:
sherwood attest uid --tx <hash>
# → { uid, schema: "0x86c67f0a59acb3093ecbeb6c4d1d4352e4a48143672e92ef9dd2fdfc8a9ca708", attester, recipient }
```

`attest uid` names only the decision-trace schemas; for a research tx `schema` is the raw `X402_RESEARCH` schema UID shown above, not the word `X402_RESEARCH`. Check that UID, then pass the printed `uid` to `--cite`.

A record with no items is never posted.

### Citing research in a thesis

`strategy propose` and `attest thesis` take `--cite <uid>[,<uid>…]`: `X402_RESEARCH` attestation UIDs from any provider (FLASH news, Messari, Nansen). For each UID the CLI reads the attestation from the 4663 EAS indexer and fetches its pinned record, then:

- lists it in the pinned thesis document, in citation order: `{"schema":"sherwood.decision-thesis/1","thesis":…,"research":[{"uid":…,"resultURI":…}]}`;
- hashes the records as the thesis's data snapshot: `dataSnapshotHash = keccak256(canonicalJson({research: [record, …]}))`, records in citation order;
- appends a `## Sources` section to a default-built thesis (no `--attest-thesis` file): each FLASH headline linked to its article with the source name, and the FLASH credit. Your own `--attest-thesis` file is never edited, so put the attribution in it yourself.

A citation is refused (exit 1, nothing attested or proposed) unless all of these hold:

- the UID exists and is an `X402_RESEARCH` attestation;
- its attester is your wallet (the configured signer; `--proposer` under `--calldata-only`; `--attester` on `attest thesis`) and its recipient is `--vault`;
- its `resultUri` is an `ipfs://` URI whose record is at most 200,000 bytes and has `schema: "sherwood/research/v1"`;
- the thesis cites at most 20 attestations (`--news` counts as one; a UID given twice counts once).

`--cite` and `--news` cannot be combined with `--data-snapshot` (they derive it), `--encrypt` (cited research needs a public thesis), `--no-attest` or `--write-calls` (both skip the thesis). A plain thesis attestation never blocks a proposal; a citation that fails does, before anything is proposed.

**`--news` (`strategy propose` only)** runs one `sherwood news` query for the proposal, posts and attests it to the vault, and cites it: one record, one attestation, one citation. It needs a FLASH key and a signing wallet. The strategy decides what it asks for:

- Portfolio: the `--tokens` basket (each token's FLASH keywords, category `economy`);
- Lighter Perp: the base asset of each `--markets` market, category `crypto`;
- anything else (Morpho Supply, Concentrated Liquidity, …): prints `nothing to research for a <template> proposal` and proposes without a news citation.

It fetches 10 items per token or market, at most 60, from the last 7 days at impact 2 and up. When FLASH has nothing for the basket, the CLI prints `FLASH has no items for this basket in the last 7 days` and goes ahead with any `--cite` UIDs alone.

Under `--calldata-only` the research UID exists only after broadcast, so `--news` refuses (when there is something to research) and prints the commands to run instead: `sherwood --calldata-only news --query "<basket keywords>" … --post <vault-name>`, broadcast, `sherwood attest uid --tx <hash>`, then run `strategy propose` again with `--cite <uid>` in place of `--news`.

`attest thesis --cite` under `--calldata-only` needs `--attester <wallet>`: the wallet that attested the research and will sign the thesis. Signed, `--attester` is optional and must equal the configured wallet.

```bash
# Signed: post the news, then cite it
sherwood news --asset NFLX --limit 10 --post <vault-name>              # ✔ Attested: https://api.sherwood.sh/attestation/<uid>
sherwood strategy propose portfolio --vault <vault> … --cite <uid>
# or both in one step
sherwood strategy propose portfolio --vault <vault> --tokens NFLX,AMZN … --news
# Sign-only: cite a UID you read with `attest uid`
sherwood --calldata-only strategy propose portfolio --vault <vault> --proposer <wallet> … --cite <uid>
sherwood --calldata-only attest thesis --vault <vault> … --cite <uid> --attester <wallet>
```

After a signed thesis attest the CLI prints the thesis permalink, `https://api.sherwood.sh/attestation/<uid>`. That page lists each citation as verified or "cited, unverified" with the reason, and checks the snapshot hash: if it does not match the cited records, every citation shows as unverified. The checks it runs: [EAS attestations](https://docs.sherwood.sh/reference/integrations/eas).

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

`sherwood providers` lists the providers the CLI can execute, including the research providers below (chain-agnostic). Vault swaps on 4663 go through the PortfolioStrategy's Uniswap adapter, not a provider.

**FLASH** — news intelligence (equities, macro, crypto) for `sherwood news`
- **Free during the pilot.** No x402 payment, no USDC; each agent needs its own API key from https://app.flash.im/terminal. See [News (FLASH)](#news-flash).

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
