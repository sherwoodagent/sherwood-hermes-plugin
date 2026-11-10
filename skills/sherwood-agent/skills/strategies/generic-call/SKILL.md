---
name: generic-call
model: sonnet
description: Reach a third-party protocol without writing Solidity through a GenericCallStrategy clone that runs a fixed list of calls — the plan file, balance patches by argument (raw or scaled, one per word), the floor price on a patched sell, the tier-2 full-notional posture, and what guardians check. sherwood-strategies template, unaudited, never certified; live on Robinhood Chain (4663) since 2026-11-07; needs CLI 0.104.0 or later. Triggers on generic call, generic-call, arbitrary calls, raw calldata, any protocol, no Solidity, balance patch, scaled patch, mulNum, mulDen, floor price, settlePatches, sweepTokens.
allowed-tools: Read Glob Grep Bash(sherwood *) Bash(cast call *) Bash(cast calldata *) WebFetch AskUserQuestion
license: MIT
compatibility: Node 22+ with @sherwoodagent/cli 0.104.0 or later (default chain robinhood-mainnet, 4663), network access to a Robinhood Chain RPC.
metadata:
  author: sherwood
  version: '0.1.0'
---

# Generic Call Strategy

> **Live on Robinhood Chain (4663) since 2026-11-07.** The template is `0xee67db9630fCB4F69C013928b2113C5489B39833` (block 108,600,958), approved on the StrategyFactory `0xb06788F027268a9A06c3AD41a88559530D2E54b1`, source verified on Sourcify (exact match). It is booked as `GENERIC_CALL_TEMPLATE` in `sherwood-strategies` `deployments/4663.json`; the protocol's `chains/4663.json` does not carry it. `sherwood strategy list` prints it as `Template: <address>`. Never use an address from anywhere else. On a chain whose book has no `GENERIC_CALL_TEMPLATE`, `sherwood strategy propose generic-call` stops with `The Generic Call template is not deployed on this chain`.

> **Use `@sherwoodagent/cli` 0.104.0 or later** (`@sherwoodagent/sdk` 0.16.0 or later if you build init data yourself). The live template's balance patch has five fields. CLI 0.103.0 and SDK 0.15.0 and older encode the old three-field patch, and the live template does not always refuse it: with a non-empty `sweepTokens` that init data decodes without reverting, `mulNum` and `mulDen` are read from the sweep list, and the patched settle amount comes out zero. Never propose a Generic Call plan with an older CLI. Guardians on an older CLI Block every clone that has a balance patch (`GENERIC_CALL_CONFIG_MISMATCH`).

> **Unaudited, used at the vault's own risk, and never certified.** `GenericCallStrategy` comes from `sherwoodagent/sherwood-strategies` (`src/generic-call/GenericCallStrategy.sol`; guide and design in `docs/generic-call/`). Like every template in that repository it is outside the protocol's audit scope, and it stays tier 2 for good: it must never be certified at tier 0 or 1. The template does not limit where funds go. The guardian review does. Run the three checks in [Strategies from `sherwood-strategies` are unaudited](../../../SKILL.md#strategies-from-sherwood-strategies-are-unaudited) on the template address, and tell the user all of this before you propose or approve one.

A Generic Call clone runs a list of calls the proposer fixes when the clone is created: a DEX router swap, a lending deposit, a marketplace order. Its `name()` is `"Generic Call"`. Nothing in the plan can change after the clone is created: `updateParams` always reverts `NoTunableParams`.

```
Vault (USDG)
    ↓ execute batch: USDG.approve(clone, pullAmount) → clone.execute()
clone.execute(): re-check each target's code → pull pullAmount → run the execute leg → revoke the leg's approvals
    ↓ the position lives at the venue, or as tokens on the clone
clone.settle(): run the settle leg (balance patches applied) → revoke approvals → push ALL its USDG to the vault → sweep listed tokens to the vault
Vault (USDG + PnL)
```

## The posture

- **Tier 2 at full notional.** The TierRegistry prices a clone's `execute()` and `settle()` at tier 2, 10,000 bps, so the proposal needs coverage equal to the whole pull. The proposer bond in WOOD is sized on that coverage. The CLI quotes the exact WOOD figure; never plan on a fixed number.
- **`tier2CallCapBps` must cover the pull.** The pull is the cap on `clone.execute()`, and a tier-2 call's cap must fit under `totalAssets × tier2CallCapBps / 10,000`, or `propose` reverts `Tier2CallCapExceedsCeiling`. The CLI checks this before anything is signed (`GENERIC_CALL_TIER2_CEILING`). If it refuses, lower `--amount`, or have the vault owner raise the cap with `sherwood governor set-tier2-call-cap --vault <vault> --bps <n>`. That setter reverts while any proposal is open. Arena vaults need at least 8,000 bps.
- **Settlement counts only USDG.** At settle the clone pushes its whole USDG balance to the vault. Anything returned in another token, and anything left on the clone, is a realised loss in the proposal's P&L. Sell back to USDG in the settle leg, and list any token you expect to be left over in `sweepTokens`.
- **The envelope's floor is the only template-wide one.** `settleProposal` refuses a settle below the `--max-drawdown-bps` floor, which bounds a bad fill only to the declared drawdown of the whole vault. Put venue-level minimums, such as a router's `amountOutMin`, in the calldata, and scale the minimum of a patched sell with a patch ([Floor price on a patched sell](#floor-price-on-a-patched-sell)).

## Write the plan

The plan is a JSON file passed with `--calls`. `--amount` is the vault asset the clone pulls at execute, in human units (`250` = 250 USDG; `0` pulls nothing).

```json
{
  "executeCalls": [
    { "target": "$asset", "signature": "approve(address,uint256)", "args": ["0xRouter", "$pullAmount"] },
    {
      "target": "0xRouter",
      "signature": "swap(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, address recipient)",
      "args": ["$asset", "0xToken", "$pullAmount", "247000000000000000000", "$clone"],
      "note": "buy the token with the whole pull"
    }
  ],
  "settleCalls": [
    { "target": "0xToken", "signature": "approve(address,uint256)", "args": ["0xRouter", "115792089237316195423570985008687907853269984665640564039457584007913129639935"] },
    {
      "target": "0xRouter",
      "signature": "swap(address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, address recipient)",
      "args": ["0xToken", "$asset", "0", "0", "$clone"],
      "note": "sell everything back to USDG; amountIn and minOut are patched"
    }
  ],
  "settlePatches": [
    { "callIndex": 1, "argIndex": 2, "token": "0xToken" },
    { "callIndex": 1, "argIndex": 3, "token": "0xToken", "mulNum": "9900000000", "mulDen": "10000000000000000000000" }
  ],
  "sweepTokens": ["0xToken"]
}
```

- **A call** is `{ target, signature, args }`, which the CLI encodes, or `{ target, data }` with raw calldata. A raw call may also carry `signature`, and the CLI then refuses it unless the data decodes as that function. `note` is free text the CLI prints; it is not stored on chain. `value` must be 0 or absent.
- **Arguments.** Amounts are integers in raw base units, written as decimal strings (USDG has 6 decimals, most tokens 18). Arrays and tuples are JSON arrays or objects. An argument a patch overwrites can be anything; write `0`.
- **Placeholders**, for an address or amount argument, a target, a sweep token or a patch token:

  | Placeholder | Becomes |
  |---|---|
  | `$clone` | The clone's address. The CLI predicts it before the clone exists. |
  | `$vault` | The vault. |
  | `$asset` | The vault asset (USDG). |
  | `$pullAmount` | The pull, in raw base units. |

### Balance patches: "patch argument N of settle call K with my balance of token T"

Settle calldata is fixed when the clone is created, but at settle you rarely know how much of a token the clone holds. A balance patch overwrites one 32-byte word of one settle call, just before that call runs, with the clone's live balance of a token, or with that balance scaled. It counts what the earlier settle calls returned.

On chain a patch is `BalancePatch(uint256 callIndex, uint256 wordIndex, address token, uint256 mulNum, uint256 mulDen)`. In the plan file it is `{ callIndex, argIndex | wordIndex, token, mulNum?, mulDen? }`:

- **Raw balance.** Omit `mulNum` and `mulDen` (on chain `(0, 0)`): the word becomes the clone's balance of `token`. It can never write more than the clone holds.
- **Scaled.** Set both: the word becomes `mulDiv(balance, mulNum, mulDen)`, rounded down. Both are raw uint256 integers. Write them as decimal strings: a JSON number is accepted only up to 2^53 − 1, and placeholders are not. Exactly one of them zero (or absent) is refused (`InvalidPatch`). A scaled patch can write any number, but the clone still moves only what it holds. Use it for the minimum of a patched sell (next section).
- **By argument:** `{ "callIndex": 1, "argIndex": 2, "token": "0xToken" }` means "argument 2 of settle call 1 becomes my balance of 0xToken". The CLI turns the argument position into the word the contract patches. The call needs a `signature`, and the argument must be a top-level `uint` whose position does not depend on the data: every argument before it may be anything, but the patched argument itself cannot be `bytes`, `string`, an array or a struct.
- **By word:** `{ "callIndex": 1, "wordIndex": 5, "token": "0xToken" }` patches the 32-byte word at byte offset `4 + 32 × wordIndex` directly. Use it for an amount inside a struct or array, or for a raw call with no signature: count the word in the ABI encoding yourself. The CLI warns when a signature shows the word is not an amount.
- **One patch per word, at most 32.** Two patches on one settle call are fine when they write different words: the amount and its scaled minimum in the plan above. A second patch on the same word of the same call is refused (`InvalidPatch`), and more than 32 patches in all reverts `TooManyPatches`.
- **Which token, which words.** The token must be the vault asset, a target of either leg, or a sweep token: list a receipt token the settle leg never calls (an aToken that `withdraw` burns) in `sweepTokens`. The spender word of `approve` and `setApprovalForAll`, the recipient word of `transfer`, and the `from` and `to` words of `transferFrom` and `safeTransferFrom` cannot be patched.
- **Without a patch:** use the venue's "max" sentinel (for example withdraw `type(uint256).max`), or make execute exact-output so the settle amounts are known.

### Floor price on a patched sell

A sell whose amount is patched cannot carry a sensible fixed minimum. Set low, it can be sandwiched at a block someone else picks: anyone can settle once the strategy duration has passed, and the drawdown floor bounds the damage only to the declared drawdown of the whole vault. Set at the propose-time quote, it reverts as soon as the market moves. Patch the minimum too, with a scale, so the floor follows what is actually sold:

- `mulNum / mulDen` is the floor price in output units per unit of the token sold, raw units on both sides. It is a price, not a share of the balance: `9801 / 10000` reads as "98.01% of the balance" only when both tokens have the same decimals and trade at par.
- From a quote at propose time, selling `quotedIn` of the token for `quotedOut` USDG (both raw), with a slippage bound of `slippageBps`: `mulNum = quotedOut × (10,000 − slippageBps)` and `mulDen = quotedIn × 10,000`.
- In the plan above, a quote of 1 token (`1000000000000000000`, 18 decimals) for 1 USDG (`1000000`, 6 decimals) at 100 bps gives `mulNum = 9900000000` and `mulDen = 10000000000000000000000`. Selling 248 tokens then needs at least 245.52 USDG.
- Never leave a patched sell's minimum at zero.
- Keep `--duration` short enough for the quote to hold. A floor the market has moved through reverts the sell (`CallFailed(1, index, reason)`). `settleProposal` and `unstick` replay the same leg, so the leg stays stuck, and the owner's emergency path returns the token to the vault in kind, which settlement books as a loss.

## Propose

```bash
sherwood strategy propose generic-call \
  --vault 0x... --calls plan.json --amount 250 \
  --name "Buy and sell XYZ through Router" --description "..." --duration 3d

# External signer: emits the clone and propose txs unsigned
sherwood --calldata-only strategy propose generic-call \
  --vault 0x... --calls plan.json --amount 250 \
  --proposer 0x... --metadata-uri ipfs://... --duration 3d
```

Before anything is signed, the CLI:

1. reads the vault's asset and the protocol contracts the clone may never call (the vault, its governor, the SyndicateFactory, TierRegistry, StrategyFactory, GuardianRegistry, sWOOD, the withdrawal queue, the ExposureLedger and the ChallengeGame);
2. predicts the clone's address and binds `$clone`. The CREATE2 salt is derived from the plan with `$clone` set to the zero address, so a plan may name its own clone. Pass `--salt` to deploy the same plan again;
3. checks every rule `initialize` enforces (table below) and the tier-2 ceiling, and refuses the plan, naming each problem, if any fails;
4. prints the plan: the clone, every call, what each patch overwrites (a scaled patch ends ` x <mulNum> / <mulDen>, rounded down`), and the `configHash` guardians compare;
5. deploys the clone through `StrategyFactory.cloneAndInitDeterministic`, then proposes with the standard batches:

| Leg | Calls | Caps |
|---|---|---|
| Execute | `[USDG.approve(clone, pullAmount), clone.execute()]` | `[0, pullAmount]` |
| Settle | `[clone.settle()]` | `[0]` |

`clone` deploys the clone without proposing: `sherwood strategy clone generic-call --vault 0x... --calls plan.json --amount 250`. `strategy init` does not support Generic Call.

### Rules checked before gas

| CLI code | Contract error | Rule |
|---|---|---|
| `GENERIC_CALL_VALUE_NONZERO` | `NativeValueUnsupported` | `value` must be 0 |
| `GENERIC_CALL_FORBIDDEN_TARGET` | `ForbiddenTarget` | No call to the zero address, the clone itself or a protocol contract. WOOD is allowed |
| `GENERIC_CALL_TARGET_NO_CODE` | `TargetHasNoCode` | Every target has code |
| `GENERIC_CALL_ASSET_SELECTOR` | `AssetCallNotAllowed` | On the vault asset, only `approve(address,uint256)` and `transfer(address,uint256)` |
| `GENERIC_CALL_TOO_MANY_CALLS` | `TooManyCalls` | At most 16 calls per leg |
| `GENERIC_CALL_CALLDATA_TOO_LARGE` | `CalldataTooLarge` | At most 16,384 bytes of calldata per leg |
| `GENERIC_CALL_TOO_MANY_SWEEP_TOKENS` | `TooManySweepTokens` | At most 16 sweep tokens |
| `GENERIC_CALL_SWEEP_TOKEN_INVALID` | `InvalidSweepToken` | A sweep token has code and is not a protocol contract |
| `GENERIC_CALL_TOO_MANY_PATCHES` | `TooManyPatches` | At most 32 settle patches |
| `GENERIC_CALL_PATCH_OUT_OF_RANGE`, `GENERIC_CALL_PATCH_TOKEN_UNKNOWN`, `GENERIC_CALL_PATCH_ADDRESS_WORD`, `GENERIC_CALL_PATCH_SCALE_HALF_SET`, `GENERIC_CALL_DUPLICATE_PATCH_WORD` | `InvalidPatch(patchIndex)` | The patch limits above: a settle call and a word inside its calldata, a token the plan names, not an address word, `mulNum` and `mulDen` both set or both omitted, one patch per word |
| `GENERIC_CALL_PROTOCOL_UNRESOLVED` | `ProtocolAddressUnresolved` | The vault's protocol contracts must all resolve |
| `GENERIC_CALL_TIER2_CEILING` | `Tier2CallCapExceedsCeiling` (at propose) | The pull fits under the vault's tier-2 per-call ceiling |

## Legs that work

- **Approvals are revoked after each leg** (`approve` and `setApprovalForAll` only). If the settle leg needs a venue approval, grant it in the settle leg.
- **Prefer venues that give the clone a token.** The vault owner's emergency path can recover what the clone holds. A position recorded only inside a venue (a lending balance keyed to the clone, an open order, a stake) comes home only through the settle leg; if that leg can never run, the position is lost.
- **Deadlines are fixed too.** `executeProposal` is permissionless inside the execution window and `settleProposal` once the strategy duration has passed. A venue deadline in the execute leg must cover the vote, the review and the execution window; one in the settle leg must cover the duration plus the time a settle takes to land. A stale deadline at settle wedges the leg the way a stale floor does.
- **Proxy upgrades are not caught.** Execute re-checks each target's code hash, but a proxy keeps its code hash when its implementation changes. Settle never checks code, so a change cannot wedge the return of capital.
- **ERC-721s** delivered with `safeTransferFrom` are accepted; the vault can pull one out with `rescueERC721To` in an emergency batch. ERC-1155s are refused.
- **Errors you may see:** `CallFailed(leg, index, reason)` (leg 0 is execute, 1 is settle; `reason` is the venue's revert data) and `TargetCodeChanged(target)` at execute. At clone creation, `InvalidPatch(uint256 patchIndex)` and `TooManyPatches()`, which CLI 0.104.0 decodes; the CLI checks both before it signs, so seeing one means the init data was built elsewhere, for example with an SDK older than 0.16.0.

## For guardians

The clone's calls are not batch calls, so `sherwood guardian review` (CLI 0.104.0 or later) is where a guardian reads them. `proposal show` prints `Cloned from the Generic Call template 0x… (sherwoodagent/sherwood-strategies).`, and the template must be `0xee67db9630fCB4F69C013928b2113C5489B39833` on 4663. The review reads the clone's `executeCalls()`, `settleCalls()`, `settlePatches()` (five fields per patch: `callIndex`, `wordIndex`, `token`, `mulNum`, `mulDen`), `sweepTokens()` and `grants()`, decodes every call with the target's verified ABI, names the argument each patch overwrites and prints its scale (` x <mulNum> / <mulDen> (rounded down)`), and checks every target, spender and address in the calldata like a batch call. A scaled patch's `mulNum / mulDen` is the floor price the proposer set: convert it with both decimals and compare it with the market. A CLI older than 0.104.0 reads `settlePatches()` with the three-field layout and Blocks every patched clone with `GENERIC_CALL_CONFIG_MISMATCH`. An Approve carries `STRATEGY_GENERIC_CALL`. The codes and the checklist are in the [guardian skill](../../guardian/SKILL.md#generic-call-proposals).
