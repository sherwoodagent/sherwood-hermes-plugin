# Error Handling

Common errors, causes, and fixes when using the Sherwood CLI.

## Setup Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `Private key not found` | The command tried to sign, and no key is configured (the normal state with an agent wallet) | Put `--calldata-only` before the subcommand and send the printed txs from the agent wallet (MetaMask Agent Wallet or Privy) |
| `Agent identity required` | No agentId saved | `sherwood identity mint --name "..."` |
| `Sherwood is not deployed on Robinhood Chain for this command yet. Retry after the mainnet deploy completes.` | A core contract this command needs is not in the CLI's 4663 address book yet | Tell the user and stop; retry after the deploy completes |
| `… not a registered agent` (keyless `strategy propose`) | The creator was never registered on the vault: keyless `vault create` cannot do it | `sherwood vault info <subdomain>` for the vault address, then `sherwood --calldata-only vault add --vault <vault> --wallet <creator> --agent-id <id>` and broadcast |

## Data feeds and research

| Error | Cause | Fix |
|-------|-------|-----|
| `401` from `sources` | No login, a login older than 7 days (the CLI refreshes the 1-hour token on its own until then), an expired challenge (10 min) or the wrong signer. A configured key re-logs in once by itself; a sign-only wallet cannot | Log in again: `sherwood sources login --address <wallet>`, sign, `--signature 0x…`. `sherwood sources status` shows `expires_at`, `valid`, `refresh_expires_at` and `auto_refresh` |
| `Sources login expired. Run sherwood sources login --address <wallet> and --signature again.` | The 7-day refresh token ran out or was rejected; the CLI cleared the login | Run the sign-only two-step again (`--address`, sign, `--signature`) |
| `403` from `sources login` or `sources get` | Valid signature, but the wallet's waitlist entry is not approved (or no longer is) | Tell the vault owner. Never retry with another wallet |
| `Wallet is no longer approved for Sherwood data feeds; tell the owner.` | `403` on refresh: the wallet was un-approved; the CLI cleared the login | Tell the vault owner and stop. Never retry with another wallet |
| `research` refuses under `--calldata-only` | x402 needs a signature and no key is configured | Use the two-step: `sherwood research … --address <wallet>` (keep it running), sign the typed data, `sherwood research complete --signature 0x…` (see [RESEARCH.md](RESEARCH.md)) |
| `research complete` exits 1 | No step 1 is waiting for this wallet, or the payment deadline passed | Start step 1 again, then sign the new typed data |
| `x402 payment window too short (deadline …)` | The provider's payment window is shorter than the time left to sign (Messari gives 60 s) | Have the signer ready before step 1, or use Nansen (300 s) |

## Attestations (public track record)

| Error | Cause | Fix |
|-------|-------|-----|
| `Schema <NAME> is not registered on Robinhood Chain yet (uid …). Registration is pending.` | The CLI could not find the decision-trace schema on the SchemaRegistry it read. On 4663 all six are registered (2026-10-13), so this means a wrong chain or registry. Every `attest` write checks first and refuses, also under `--calldata-only` | Tell the owner and carry on: the proposal does not depend on it. Check `sherwood --version` (0.93.0 or later) and the chain |
| `attest` warning during `strategy propose --attest-thesis` | The thesis or link attestation failed (unregistered schema, pin failure, gas) | Nothing to undo: the propose went ahead. Re-run `sherwood attest thesis` / `attest link` later if you want the record |

## Permission Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `NotCreator` | Wallet isn't the syndicate creator | Use the creator wallet |
| `NotARegisteredStrategy(target)` | Batch `target` is neither the vault `asset()` nor a strategy registered on `StrategyFactory` | There is no vault-side target list and no callee allowlist. Register the clone: `StrategyFactory.isRegisteredStrategy(target)` must be true (registration is permissionless). |
| `TransferFromNotVault(from)` | A `transferFrom` on the vault `asset()` whose `from` is not the vault | The asset leg may only pull from the vault itself. Rebuild the call with `from = vault`. |
| `MalformedAssetCall(selector)` | A call on the vault `asset()` with fewer than 36 bytes of calldata | The asset leg must name a first argument (spender or `from`). Encode the full call. |
| `Tier2CallCapExceedsCeiling(i)` | Call `i` resolves to tier 2 and its declared `caps[i]` exceeds `totalAssets() * tier2CallCapBps() / 10_000` | Lower that call's cap, or certify the `(target, selector)` pair on `TierRegistry` to leave tier 2. |
| `NotApprovedDepositor` | A deposit, mint or `queue request-deposit` whose share receiver is not approved. Either the vault is in whitelist mode, or the factory's `depositsRestricted` launch flag is on (then every vault is) | Only the vault owner can fix it: ask them to run `sherwood vault approve-depositor --vault <vault> --depositor <receiver>`. Signed `vault deposit` and `queue request-deposit` refuse before sending; `--use-eth` and `--calldata-only` deposits reach the chain and revert |
| `DepositorNotApproved` | The owner tried to remove a depositor who is not on the list | Nothing to remove. Check the address |
| `NotAgentOwner` | The ERC-8004 identity is not owned by the right wallet. `vault create` needs the creator to own `creatorAgentId` in the factory's `agentRegistry()`; `vault add` / `vault approve` need the agent wallet or the vault owner to own `--agent-id` | Mint one with `sherwood identity mint --name <name>`, or pass an ID you own (`sherwood identity status`). Signed `vault create` refuses before sending |

## Execution Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `CallCapExceeded(i, outflow, cap)` | Call `i` of the batch moved more of the vault asset out than its declared cap | Raise that call's cap in the proposal (re-propose) or reduce the amount the strategy pulls |
| `MaxCapitalExceedsCeiling` / `MaxCapitalCeilingRegressed` | `--max-capital` is above `totalAssets() × maxCapitalBps / 10_000` at propose, or the vault shrank before execute | Lower `--max-capital`, or (owner) raise `governor set-max-capital` while no proposal is open |
| `BufferBreached` | After the batch, the vault held less idle asset than `minBufferBps` requires | Deploy less (`--max-capital`), or (owner) lower `vault set-min-buffer` while no proposal is open; never raise it mid-proposal |
| `CoveredTvlCapExceeded` | The proposal's required coverage in USD exceeds the ledger's per-proposal cap ($1M at launch) | Split the book or deploy less |
| `CoverageHorizonExceeded` | `executeBy + strategyDuration` is more than 60 days out | Shorten `--duration` |
| `OwnerBondNotLive` | The vault owner's stake is not bound, or an owner unstake is pending | Owner: cancel the unstake (`StakedWood.cancelUnstakeOwner`) or re-bond via `rotateOwner`; nobody can propose or execute until then |
| `VaultHasOpenProposal` | One proposal per vault at a time | Wait for the open one to reach a terminal state (`sherwood proposal list --vault`) |
| `CooldownNotElapsed` | Less than `cooldownPeriod` (1h default) since the last proposal ended | Wait; checked at propose |
| `TierRegressed` | A `(target, selector)` pair was demoted on `TierRegistry` between propose and execute, so the live tier exceeds the proposal's envelope | Re-propose at the current tier; the envelope is snapshotted at propose and cannot be widened. |
| `CoverageRegressed` | Live required coverage now exceeds what the proposal snapshotted | Re-propose. Coverage is re-resolved at execute and may only shrink. |
| `Simulation failed` | Batch would revert on-chain | Check caps, `StrategyFactory.isRegisteredStrategy` on every non-asset target, the asset-leg shape, and token balances |
| `ERC721InvalidReceiver` | Vault can't receive NFTs | Vault includes ERC721Holder — redeploy if on old version |
| `Could not read decimals` | Invalid token address | Verify the address is a valid ERC-20 on Robinhood Chain (4663); see ADDRESSES.md |
| `StalePrice` | A Chainlink feed the strategy or ledger reads is older than its bound (26h for stock feeds, which pause over weekends) | Wait for the feed; for stock baskets avoid executing, settling or rebalancing from ≈ Sat 18:40 to Mon 00:00 UTC |
| `NoWoodPrice` / `WoodPriceUnset` | The ledger cannot price WOOD (feed stale or unwired, or the owner cap is 0), so proposing, approving and filing halt | Nothing on the agent side fixes it; the keeper must roll the WOOD feed. Retry later and tell the user |
| `IPFS upload failed` | Hosted Sherwood pinning API unreachable or errored | Non-fatal — CLI falls back to inline `data:` metadata; check network or set `SHERWOOD_API_URL` |

## Proposer Bond Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `InsufficientProposerBondWood` | Wallet WOOD balance is below the quoted proposer bond (`ExposureLedger.proposerBondWood`): 1% of required coverage at the haircut WOOD price, about 4% of deployed capital for an uncertified strategy. A CLI-side refusal, not an on-chain error | First reclaim any bond from an earlier proposal (`sherwood proposal reclaim-bond --vault <v> --id <id>`). Otherwise the wallet must hold more WOOD: tell the user, and do not buy or move WOOD without their go-ahead (see "Fund the wallet" in [SKILL.md](SKILL.md#fund-the-wallet-eth-and-wood)). This is **not** the 10k owner stake. The amount **scales** with coverage and WOOD price — quote it; do not assume a fixed WOOD number |
| `ProposalNotTerminal` / `NoBondToReclaim` / `ChallengeWindowOpen` | `proposal reclaim-bond` ran before the proposal ended, on a proposal with no bond, or on a Settled proposal before `executedAt + strategyDuration + 14 days` | Wait for the terminal state or the challenge window; the bond is released then |
| `ExposureLedgerUnset` / `LedgerEscrowMismatch` | The governor has no exposure ledger wired, or its bond escrow points at a different ledger | Protocol-owner wiring (`pushWiring`); tell the user |

## Governance Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `ProposalNotApproved` | Tried to execute a proposal that isn't approved | Wait for voting to end (optimistic = auto-passes) or check veto threshold |
| `ProposalNotCancellable` | Veto, cancel or emergency-cancel in a state that does not allow it (veto and owner cancel: `Pending` only; proposer cancel: Draft, Pending until voteEnd, GuardianReview, Approved) | Check the state with `sherwood proposal show <id> --vault <vault>`; after GuardianReview only guardian block-quorum stops a proposal |
| `NotVaultOwner` | Non-owner tried to veto, emergency cancel, unstick or emergency settle | Must use the vault owner wallet |
| `StrategyAlreadyActive` | Tried to execute while another strategy is live | Wait for current strategy to settle first |
| `ProposalNotApproved` on an Approved-looking proposal | The execution window closed, so the proposal is `Expired` (there is no separate window error) | Submit a new proposal; reclaim the bond with `proposal reclaim-bond` |
| `StrategyDurationNotElapsed` | The proposer tried to settle less than 1 hour after execute, or someone else before the strategy duration | Wait; the proposer may settle from 1h after execute, anyone after the duration |
| `SettlePriceBelowFloor` | Settlement would leave the share price below the declared drawdown floor | Owner path: `unstick` applies the 90% rescue floor; otherwise tell the user |
| `ParamsFrozenDuringProposal` | A governor setter ran while a proposal is open (Drafts count) | Wait for the terminal state, then retry |
| `NotWithinVotingPeriod` | The proposal is not open for votes. Voting opens one second after the propose block (the governor refuses while `block.timestamp <= snapshotTimestamp + 1`) and closes when the voting period ends | Right after a propose, resend once the chain has produced a later block (signed `proposal vote` waits for this itself). Otherwise check the state with `sherwood proposal show <id> --vault <vault>` |
| `ProposerNotOwner` | The vault is single-operator: the factory's `ownerOnlyProposals` launch flag is on, so only the vault owner can propose | Do not retry from this wallet. Tell the user; only the owner can propose, from the owner wallet |
| `CollaborationDisabled` | Collaborative proposals are off while `ownerOnlyProposals` is on: the owner proposes alone. Also raised when a co-proposer approves a Draft opened before the flag went on | Drop the co-proposers and propose from the owner wallet. For a Draft opened before the flag went on, cancel it: the proposer with `sherwood proposal cancel --vault <vault> --id <id>`, or the vault owner with `--emergency` added |
| `StrategyNotSettled(strategy)` | The settlement calls ran, but the strategy still reports `executed()`: no leg called its `settle()`, so capital would stay on the clone. `settleProposal` and `unstick` both refuse to close the proposal | Retrying replays the same pre-committed calls and reverts again. The only exit is the vault owner's bonded, guardian-reviewed `emergencySettleWithCalls` → `finalizeEmergencySettle` after the strategy duration (see the `vault-owner` skill). If you are not the owner, tell the user and the owner |

## Strategy Template Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `AlreadyInitialized` | Tried to initialize a strategy clone twice | Each clone can only be initialized once |
| `NotVault` | Non-vault address called `execute()` or `settle()` | Strategy must be called by the vault via batch calls |
| `NotProposer` | Non-proposer tried to `updateParams()` | Only the original proposer can tune parameters |
| `NotExecuted` | Tried to settle or update params before execution | Strategy must be in Executed state |
| `AlreadyExecuted` | Tried to execute an already-executed strategy | Each strategy executes once |
| `InvalidAmount` / `InvalidWeights` / `TooManyTokens` | Portfolio init with a zero amount, weights that do not sum to 10,000 bps, or more than 20 tokens | Fix the basket |
| `InvalidSlippage` | Portfolio `--max-slippage` outside 50–1000 bps, or Concentrated Liquidity `--settle-slippage-bps` outside 50–1000 (fixed for the clone's life) | Choose a value in range |
| `ProposerNoLongerAgent` | `updateParams` / rebalance from a proposer the owner has since removed from the vault | Only a registered agent can tune a live strategy; ask the owner to re-register, or wait for settlement |
| `TierRegistryUnresolved` | Portfolio, Morpho Supply or Concentrated Liquidity could not resolve the TierRegistry (vault → governor → `tierRegistry`: the vault's `governor()`, then that governor's `tierRegistry()`), so it cannot check its counterparties and fails closed | Not fixable by the vault owner or the proposer. Only the factory can set a governor's registry; the protocol owner pushes it with `SyndicateFactory.pushWiring(governor)`. Tell the user |
| `CounterpartyNotAllowed` | The Concentrated Liquidity strategy uses a counterparty that is not on the protocol's allowlist: its swap adapter, position manager, Morpho, the Uniswap factory, or the pool's other token | Use allowlisted counterparties; adding one is a protocol-owner action, so tell the user and do not retry |
| `MorphoMarketNotAllowed` | The Morpho market is not on the protocol's allowlist. The allowlist is keyed by market id, which covers all five market parameters (loan token, collateral, oracle, rate model, LLTV). Raised by Morpho Supply and by Concentrated Liquidity | Choose an allowlisted market. Only the protocol owner can add one; do not retry the same market |
| `AssetNotVaultAsset` | The Portfolio strategy was configured with an asset that is not the vault's own asset | Use the vault's asset |
| `AssetNotUsdPegged` | The Portfolio strategy needs a vault asset the protocol's coverage ledger prices within 1% of $1. This one is outside that band, or the ledger cannot price it (an unpriced asset or a stale feed reads as 0) | Use a vault whose asset is a USD stablecoin the ledger prices; if the feed is stale, wait for it rather than retrying in a loop |
| `ExposureLedgerUnresolved` | The Portfolio strategy could not resolve the coverage ledger (vault → governor → `exposureLedger`), so it cannot check the vault asset's peg and fails closed | Not fixable from the agent side: the protocol owner has to wire the ledger |
| `UnrecognizedAssetSelector` | A batch call targets the vault's own asset with a function the vault does not allow. On the asset, a batch may only call `transfer`, `transferFrom` out of the vault, and the approve family | Remove or replace that call in the proposal's execute or settlement calls |

## Coverage Errors (ExposureLedger)

| Error | Cause | Fix |
|-------|-------|-----|
| `ApproveLockBelowFloor` | A guardian Approve booked less than the floor. A slot costs `min(ceil(need/100), your whole budget)`, valued in USD at vote time. Either the lock booked to zero (your free budget, k × stake minus open exposure, is used up), your budget values to zero (no slashable stake, or WOOD prices to zero right now), or what you declared is worth less than the floor | Close an open lock or wait for one to age out, or raise `--lock-wood` on `sherwood proposal review-vote`. See the `guardian` skill |
| `CoverageInputsUnreadable` | The ledger could not read the proposal's coverage inputs (vault asset, required coverage). Usually there is no such proposal on that governor: an unknown ID reads back a zero vault | Check the proposal ID, and that the address really is this vault's governor (`factory.governorOf(vault)`) |

## Factory Errors

Vault creation is invite-only: a wallet the Sherwood Safe sponsored through the waitlist creates for free (once); any other wallet pays `creationFee()` in `creationFeeToken()`. There is no dedicated fee error: an unpaid fee surfaces as the fee token's own ERC-20 revert. Signed `vault create` checks the bond, fee, identity and subdomain length first and refuses before sending anything (see "Create new vault" in [SKILL.md](SKILL.md#create-new-vault)).

| Error | Cause | Fix |
|-------|-------|-----|
| `PreparedStakeNotFound` | No prepared owner stake for the creator, or it is already bound to a vault | With the user's go-ahead to bond that WOOD, `sherwood guardian prepare-owner-stake <amount>` (at least `minOwnerStake`, 10,000 WOOD at launch) from the creating wallet, then retry. The wallet must hold the WOOD first ("Fund the wallet" in [SKILL.md](SKILL.md#fund-the-wallet-eth-and-wood)) |
| `InsufficientStake` on `prepare-owner-stake` | Amount below `StakedWood.minOwnerStake()` | Use at least the live minimum |
| `PreparedStakeAlreadyExists` | This wallet already holds an unbound prepared stake | Use it (`vault create`) or `StakedWood.cancelPreparedStake()` to get the WOOD back |
| `ERC20InsufficientAllowance` | On `vault create`: the creator is not sponsored and did not approve the factory for the creation fee. (Also raised by any other token pull with a short allowance) | Signed `vault create` approves the fee itself. Keyless: send the printed `approve` before `createSyndicate`, or pass `--creator` so the CLI checks |
| `ERC20InsufficientBalance` | On `vault create`: the creator is not sponsored and holds less than the creation fee. (Also raised by any other token transfer) | Tell the user: join the waitlist at https://sherwood.sh for sponsorship of this wallet, or fund it with the fee |
| `NotAgentOwner` | The creator does not own `creatorAgentId` in the factory's `agentRegistry()` | Mint from the creating wallet (`sherwood identity mint --name <name>`) and pass that ID with `--agent-id` |
| `ERC721NonexistentToken` | That agent ID was never minted on the registry this contract reads | Check the ID with `sherwood identity status`, or mint one |
| `SubdomainTooShort` / `SubdomainTaken` | Subdomain under 3 characters, or already used by another vault | Pick another subdomain and confirm it with the user |
| `InvalidSyndicateConfig` | Asset, name, symbol, subdomain or metadata URI is empty | Fill every field (the CLI does this; check hand-built calldata) |

## Deposit / Withdrawal Queue Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `DepositsLocked` / `RedemptionsLocked` | An instant deposit, redeem or rescue while a proposal is open (from propose until a terminal state) | Use the queue: `sherwood queue request-deposit` / `request-redeem`, then `claim` after settlement |
| `NoOpenProposal` / `RedemptionsNotLocked` | `queue request-deposit` / `request-redeem` while no proposal is open | Use the instant path: `sherwood vault deposit` / `vault redeem` |
| `NotSettled` on `queue claim` | A queued redeem whose proposal has not settled. If that proposal ended Cancelled, Rejected or Expired it will **never** settle | Wait for settlement, or `sherwood queue cancel` the request and redeem instantly once the vault unlocks |
| `VaultLocked` on `queue claim` | The vault has another proposal open | Wait for it to end |
| `AlreadySettled` on `queue cancel` | The redeem's proposal already stamped a price; the payout is fixed | `queue claim` instead |
| `QueueReserveBreached` | A redeem or batch would dip into assets reserved for stamped queue claims | Claim or wait; the reserve clears as claims are paid |

## XMTP / Chat Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `Conversation not found` | Stale XMTP installation — group welcome targeted old KeyPackage | Revoke stale installations, get re-added (see steps below) |
| `numSynced: 0` after sync | Welcome encrypted for wrong installation (db was nuked/reinstalled) | Same as above — revoke + re-add |
| `PRAGMA key or salt has incorrect value` | DB encryption key mismatch (`~/.xmtp/.env` was overwritten) | Delete `~/.xmtp/xmtp-db*` files (keep `.env`), re-run any chat command, then get re-added |
| `Association error: Missing identity update` | Wallet never registered on XMTP production | Non-fatal — run `sherwood chat <name>` to force registration |
| `` Not logged in to sources (or token expired). Run `sherwood sources login`. `` | `chat --join-cohort` and `sources get` need the `sources login` bearer token, which lasts 1 hour and the CLI refreshes for 7 days; this means there is no login or the 7-day login ran out | `sherwood sources login` (sign-only: `--address`, sign with `mm wallet sign-message` or Privy `personal_sign`, then `--signature`), then retry |
| `` … has no XMTP identity on this machine yet. Run `sherwood chat --join-cohort --address …` `` | Sign-only wallet with no local XMTP identity | Run the sign-only two steps: `sherwood chat --join-cohort --address <wallet>` (keep it running), then `--signature 0x…` |
| `not in the cohort group yet (bot adds approved wallets within a few minutes)` | Join request not processed yet, or status is `no_identity` / `failed` | Wait a few minutes and retry `chat --cohort log`. Check status with `GET https://api.sherwood.sh/cohort/join` |

### Stale installation fix (most common XMTP issue)

The CLI keeps its XMTP identity in `~/.sherwood/xmtp/xmtp.db3` (encryption key derived from the agent key, or `xmtpDbKey` in `~/.sherwood/config.json` for key-less wallets). Follow the ordered steps in "Chat troubleshooting (welcome not arriving)" in [SKILL.md](SKILL.md#chat-troubleshooting-welcome-not-arriving): `sherwood session check <name>` first, then confirm the wallet, then seed the group cache, then revoke orphan installations and get re-added.

**Golden rule:** never delete `~/.sherwood/xmtp/` after being added to a group, and never open that DB from another script (it creates a second installation under the same inbox). Reinstalling the CLI does not touch it. After any reset you must get re-added to the group.
