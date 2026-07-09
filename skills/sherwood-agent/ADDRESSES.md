# Contract Addresses

Sherwood currently deploys on **Robinhood testnet (chain 46630)** — the CLI targets
it by default. These addresses are also available in `cli/src/lib/addresses.ts`
(resolved at runtime). This is the initial deployment target; the protocol will
expand to more chains over time.

> On-chain contracts keep the `Syndicate*` naming for audit continuity; the product concept is a fund.

> See also: [Deployments reference](https://docs.sherwood.sh/reference/deployments)

## Robinhood testnet (chain 46630)

V2 deployment — full stack: core contracts + guardian layer (registry + sWOOD) +
live-NAV (PriceRouter + Uniswap-compatible adapter backed by Synthra) +
StrategyFactory keyless deploy. Source of truth: `contracts/chains/46630.json`.

| Contract | Address |
|----------|---------|
| SyndicateFactory | `0x81F785E31B9f31BC437978B5E7dEe8006F43dd8b` |
| SyndicateGovernor | `0xdAB22196ac2c6C5C33445f3125C28aF98E0cA2d0` |
| SyndicateVaultImpl | `0xB21b3af1E9f99222d136517363d0d185AF2449cd` |
| BatchExecutorLib | `0xB869dF634679C6a36D6C82D47D1793486F4cba3a` |
| GuardianRegistry | `0x2fe810666E673Fbd87cA00f95EBf82a19A5b543F` |
| StakedWood (sWOOD) | `0x320911DEF7Ec4c0C435B9cCEE3fAfE9EEad171C9` |
| WOOD token (fixture) | `0x4435Aae199907f60588902Bcd7c4363a13Bb2951` |
| PriceRouter | `0x7Cc36bcc6a1f0F2607BacC8692fe2aD52eB14fd7` |
| PortfolioStrategy (template) | `0x3a6121371D51B59De3A14Bc401EfBd3fb726E109` |
| StrategyFactory | `0xBBb2884dEAD8235d75a0405ecC3c9F5713cBE904` |
| UniswapSwapAdapter (Synthra-backed) | `0x2e8aB422Db9127F1a70d8C38A15903f22B51DA97` |

### Tokens

| Token | Address |
|-------|---------|
| WETH (default vault asset) | `0x7943e237c7F95DA44E0301572D358911207852Fa` |
| TSLA | `0xC9f9c86933092BbbfFF3CCb4b105A4A94bf3Bd4E` |
| AMZN | `0x5884aD2f920c162CFBbACc88C9C51AA75eC09E02` |
| PLTR | `0x1FBE1a0e43594b3455993B5dE5Fd0A7A266298d0` |
| NFLX | `0x3b8262A63d25f0477c4DDE23F83cfe22Cb768C93` |
| AMD | `0x71178BAc73cBeb415514eB542a8995b82669778d` |

There is no USDC on this chain — WETH is the default vault asset.

### External Protocols

Synthra is Uniswap-V3-compatible; the deployed `UniswapSwapAdapter` is backed by the
Synthra router plus a QuoterV2 shim. Prices come from Chainlink Data Streams via the
verifier proxy.

| Contract | Address |
|----------|---------|
| Synthra Router | `0x3Ce954107b1A675826B33bF23060Dd655e3758fE` |
| Synthra Quoter | `0x231606c321A99DE81e28fE48B07a93F1ba49e713` |
| Synthra V3 Factory | `0x911b4000D3422F482F4062a913885f7b035382Df` |
| Synthra QuoterV2 shim | `0xC703139b5C7EA26D906fcd6d335798d2EC9A1262` |
| Chainlink Verifier Proxy | `0x72790f9eB82db492a7DDb6d2af22A270Dcc3Db64` |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` |
| Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` |

## Not yet active on Robinhood testnet

The following are not deployed on the current target chain and come online as
Sherwood expands to more chains:

- **ERC-8004 agent identity** — no IdentityRegistry on this chain; `fund create`
  and `fund add` skip identity verification (registries are `address(0)`), and
  `agentId=0` is used when `--agent-id` is omitted.
- **EAS coordination attestations** (join requests / approvals) — no EAS predeploy.
- **ENS subnames (Durin)** — no registrar; `fund create` skips ENS registration.
- **Strategies other than Portfolio** — Moonwell (supply / wstETH), Aerodrome LP,
  Leveraged Aerodrome CL, Venice inference, Mamo yield, Hyperliquid perp/grid.

## Strategy Templates

The live strategy on Robinhood testnet is **Portfolio** — a weighted basket of
tokenized stocks/crypto with on-chain rebalancing through Synthra. Use
`sherwood strategy list` to see current deployed template addresses.

| Template | Address |
|----------|---------|
| PortfolioStrategy | `0x3a6121371D51B59De3A14Bc401EfBd3fb726E109` |
| UniswapSwapAdapter (Synthra-backed) | `0x2e8aB422Db9127F1a70d8C38A15903f22B51DA97` |

Under the V2 live-NAV model the strategy is never trusted for value: it reports its
on-venue holdings via `IStrategy.positions()` and the vault prices them through the
governance-owned `PriceRouter`. Portfolio reports no priceable positions and routes
through the async-redeem queue (Lane B), settling at one frozen per-proposal price.

## Allowlist Targets — Portfolio Strategy

```bash
sherwood vault add-target --target 0x7943e237c7F95DA44E0301572D358911207852Fa  # WETH (vault asset)
sherwood vault add-target --target 0x2e8aB422Db9127F1a70d8C38A15903f22B51DA97  # UniswapSwapAdapter (Synthra)
sherwood vault add-target --target 0x3Ce954107b1A675826B33bF23060Dd655e3758fE  # Synthra Router
sherwood vault add-target --target <stock-token-addresses>                       # e.g. TSLA / AMZN / AMD
sherwood vault add-target --target <strategy-clone-address>                       # Your strategy contract
```
