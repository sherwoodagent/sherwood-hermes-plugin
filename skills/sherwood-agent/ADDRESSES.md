# Contract Addresses

Sherwood v1 is live on **Robinhood Chain mainnet (chain 4663)**. Source of truth:
`chains/4663.json` in `sherwoodagent/sherwood-protocol` (branch `v1-deploy`). The
CLI carries the same table (`sherwood strategy list` prints the templates it sees),
and the rendered docs are at <https://docs.sherwood.sh/reference/deployments>.

> **Do not reuse addresses from any other chain.** The Tenderly fork (chain 9994663)
> is closed and Robinhood testnet (46630) runs pre-v1 contracts. Both are retired.

Chain facts: ETH is the gas token, **USDG** (6 decimals) is the vault asset (there is
no USDC), official Uniswap v3 and v4, Morpho Blue, Chainlink **push** feeds (24h
heartbeat, 26h staleness bound; stock feeds pause over weekends). Explorer:
<https://robinhoodchain.blockscout.com>. Public RPC: `https://rpc.mainnet.chain.robinhood.com`
(rate-limited; set `ROBINHOOD_RPC_URL` or `sherwood config set --rpc` for anything unattended).

## Core (run 1, live since 2026-10-06)

| Contract | Address |
|----------|---------|
| SyndicateFactory | `0x619af1488F32B4797f6faDa3C3a8De72a5B693E0` |
| GovernorBeacon | `0x3CF45eFE9fBDdd6cB749384Bf5c1bA5Ec96af05d` |
| ProtocolConfig | `0x8aDb5810B5bF8B9149BE3Ca45Cb441818a9679bB` |
| SyndicateVaultImpl | `0xdaB4707c9997a84e931FE98F4688adaFc70b6B95` |
| BatchExecutorLib | `0x899FC2Cac572F64A233344115b488E71E0044f34` |
| GuardianRegistry | `0xD8F9E7446E6d6c02198AAaF674A0C5e3d5D1BF72` |
| StakedWood (sWOOD) | `0xdDf2B4C52B9575e0e6D03a3A57D75D06a4a9e203` |
| TierRegistry | `0x4614f058920941A0a9a852e62485fbDE692D75E1` |
| StrategyFactory | `0xb06788F027268a9A06c3AD41a88559530D2E54b1` |
| UniswapSwapAdapter | `0x54E6A7af53143556973493fDeC9d7837A77c67eF` |
| WOOD/USD feed (WoodPoolFeed) | `0xE031f1a5e8dBb5BAE98607BEC44c9fE91B608E09` |
| Create3Factory | `0x231972653B1e4fc772fcF064F3FD2357d9Df371D` |

There is no singleton `SyndicateGovernor`: each vault gets its own `BeaconProxy`,
resolved via `factory.governorOf(vault)` (`sherwood governor info --vault` prints it).
Each vault also gets its own `VaultWithdrawalQueue`, read from `vault.withdrawalQueue()`.

## Run 2 (economic-security stack)

Deployed in the second run of the launch ceremony on 2026-10-13 (14:31 UTC), which
also opened vault creation: the factory's `agentRegistry()` is now the canonical ERC-8004
registry. Source: `chains/4663.json` on `v1-deploy`. All four are owned by the deployer
until the Safe handoff.

| Contract | Address |
|----------|---------|
| ExposureLedger | `0x99D789E7C8E65dB5597dCa692F082B47BE30AB33` |
| ProposerBondEscrow | `0x7d25058c891d3AC5C8bc8Da6aA937DFeE648B5cF` |
| ChallengeGame | `0x7046FececcA6c0f5E66Cc998099593ae71A1bbee` (filings paused through the beta) |
| TokenCourt | `0xC30bA62727be65fd867385Ce27Ff7C78bd66A897` |

The same addresses resolve live from any vault's governor: `governor.exposureLedger()`,
`governor.bondEscrow()`, and `exposureLedger.coverageFreezer()` for the game.

## Strategy templates

| Template | Address |
|----------|---------|
| PortfolioStrategy | `0xAA5872009c527cCb80343E41C52840CEdb095eb0` |
| MorphoSupplyStrategy | `0x5B55E1Da361573CB0788e750038567D2569BE41d` |
| ConcentratedLiquidityStrategy | `0xcba9C84F2d382729D1519c5F7f4a9AAaC075f8B5` |

`LaunchpadStrategy`, its Sushi / StonkBrokers launch adapters and `LighterPerpStrategy`
come from `sherwood-strategies`, are unaudited, and are **not deployed on Robinhood
Chain**; `sherwood strategy list` shows them under "Not available". See
[Strategies from `sherwood-strategies` are unaudited](SKILL.md#strategies-from-sherwood-strategies-are-unaudited).

## WOOD and where to buy it

| Item | Address |
|------|---------|
| WOOD (ERC-20, 18 decimals) | `0xF8BC08092C06dB6148114DCf82AF881F1085f92b` |
| WETH | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` |
| WOOD/WETH Uniswap V2 pair | `0xBF3BB81de6285b8310A028d1C2Cd38F9419d54C1` |
| WOOD/WETH Uniswap V3 pool | `0xF6831AaF374B353757F0dE6B3DD2a74697671C69` |
| WOOD/WETH Uniswap V3 factory | `0xE51960f1B45f1C9FB6D166E6a884F866fC70433B` |

The CLI has no command that buys WOOD. Swap WETH for WOOD through the Uniswap
SwapRouter below (or the V2 pair directly); both pools are shallow, so quote first and
size the trade in tranches. How much WOOD a creator needs: [Fund the wallet](SKILL.md#fund-the-wallet-eth-and-wood).
The protocol prices WOOD from the lower of the two pools' 24h TWAPs (times Chainlink
ETH/USD), capped by the owner and haircut 50%; that haircut price sizes the proposer bond.

## Tokens

| Token | Address |
|-------|---------|
| USDG (vault asset, 6-dec) | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` |
| AAPL | `0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9` |
| TSLA | `0x322F0929c4625eD5bAd873c95208D54E1c003b2d` |
| NVDA | `0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC` |
| MSFT | `0xe93237C50D904957Cf27E7B1133b510C669c2e74` |
| AMZN | `0x12f190a9F9d7D37a250758b26824B97CE941bF54` |
| META | `0xc0D6457C16Cc70d6790Dd43521C899C87ce02f35` |
| GOOGL | `0x2e0847E8910a9732eB3fb1bb4b70a580ADAD4FE3` |
| AMD | `0x86923f96303D656E4aa86D9d42D1e57ad2023fdC` |
| SPY | `0x117cc2133c37B721F49dE2A7a74833232B3B4C0C` |
| QQQ | `0xD5f3879160bc7c32ebb4dC785F8a4F505888de68` |
| SLV | `0x411eFb0E7f985935DAec3D4C3ebaEa0d0AD7D89f` |
| ASML | `0x47F93d52cBeC7C6D2CfC080e154002370a60dAEA` |
| BABA | `0xad25Ac6C84D497db898fa1E8387bf6Af3532a1c4` |
| CRCL | `0xdF0992E440dD0be65BD8439b609d6D4366bf1CB5` |
| DELL | `0x941AE714EC6D8130c7B75d67160Ca08f1e7d11Dd` |
| GME | `0x1b0E319c6A659F002271B69dB8A7df2F911c153E` |
| INTC | `0xc72b96e0E48ecd4DC75E1e45396e26300BC39681` |
| MSTR | `0xec262a75e413fAfD0dF80480274532C79D42da09` |
| MU | `0xfF080c8ce2E5feadaCa0Da81314Ae59D232d4afD` |
| PLTR | `0x894E1EC2D74FFE5AEF8Dc8A9e84686acCB964F2A` |
| SNDK | `0xB90A19fF0Af67f7779afF50A882A9CfF42446400` |
| SPCX | `0x4a0E65A3EcceC6dBe60AE065F2e7bb85Fae35eEa` |
| TSM | `0x58FfE4a942d3885bAa22D7520691F611EF09e7AA` |
| USAR | `0xd917B029C761D264c6A312BBbcDA868658eF86a6` |
| USO | `0xa30FA36Db767ad9eD3f7a60fC79526fB4d56D344` |

Every stock has a Chainlink USD push feed (`CHAINLINK_<SYM>_USD_FEED` in the book);
`sherwood sources get universe` prints token, decimals, multiplier and feed per symbol,
and `sherwood strategy propose portfolio --tokens <SYMS>` resolves the feeds itself.

## External protocols

| Contract | Address |
|----------|---------|
| Uniswap V3 Factory | `0x1f7d7550b1b028f7571e69a784071f0205fd2efa` |
| Uniswap V3 PositionManager | `0x73991a25C818Bf1f1128dEAaB1492D45638DE0D3` |
| Uniswap SwapRouter | `0xcaf681a66d020601342297493863e78c959e5cb2` |
| Uniswap QuoterV2 | `0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7` |
| Uniswap V4 PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` |
| Uniswap V4 Quoter | `0x8dc178efb8111bb0973dd9d722ebeff267c98f94` |
| Morpho Blue | `0x9D53d5E3bd5E8d4Cbfa6DB1ca238AEA02E651010` |
| Chainlink ETH/USD | `0x78F3556b67E17Df817D51Ef5a990cDaF09E8d3A9` |
| Chainlink USDG/USD | `0x61B7e5650328764B076A108EFF5fa7282a1B9aD2` |
| EAS | `0x7d70441Bb10AcE5d9771dc0b6205D89ddf63205B` |
| EAS SchemaRegistry | `0xdd7521Ba10773e556Defa6D6c133cB2F663b5c26` |
| Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` |

## Batch callees

There is no callee allowlist and no adapter allowlist anywhere in the v1 stack —
`TierRegistry` prices calls, it does not gate them. The structural rule lives in
`SyndicateVault._guardBatchCalls` (mirrored at propose by the governor): every
call target is either the vault `asset()` or a strategy registered on
`StrategyFactory` (`isRegisteredStrategy`, permissionless), and anything else
reverts `NotARegisteredStrategy(target)`. On the `asset()` leg only three kinds of
call pass: `transfer`, `transferFrom` with `from == vault` (else
`TransferFromNotVault`), and the approve family (`approve`, `increaseAllowance`,
`increaseApproval`, `decreaseAllowance`, `decreaseApproval`), whose first argument
is the spender whose allowance is reset after the batch. Calldata shorter than 36
bytes reverts `MalformedAssetCall`, and any other selector reverts
`UnrecognizedAssetSelector`. Strategies check their own venues against the
TierRegistry counterparty, Morpho-market and price-source allowlists
(`CounterpartyNotAllowed`, `MorphoMarketNotAllowed`, `PriceSourceNotAllowed`).

## Identity and names

- **Identity** — every agent mints on the canonical ERC-8004 IdentityRegistry
  `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` on Robinhood Chain (ReputationRegistry
  `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`). Since run 2 it is the factory's
  `agentRegistry()`: `vault create` needs the creator to own `--agent-id`, and
  `vault add` / `vault approve` need the agent wallet or the vault owner to own the id
  (`NotAgentOwner`). See "Mint ERC-8004 identity" in [SKILL.md](SKILL.md#mint-erc-8004-identity).
- **ENS names** — the factory does not register them; a vault's name is its subdomain
  (see "Vault names" in [SKILL.md](SKILL.md#vault-names)).
