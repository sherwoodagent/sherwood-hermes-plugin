# 4663 fork runbook: the PortfolioStrategy loop on a local anvil fork

These scripts run the whole Sherwood fund loop on a local anvil fork of Robinhood
Chain mainnet (chain 4663) with Sherwood CLI 0.102.0. The loop covers ERC-8004
identities, a USDG vault with whitelisted deposits, members depositing, an agent
that cannot propose, an owner proposal that a 20% holder vetoes, an owner proposal
nobody objects to, guardian review, execute, settle and redeem. It also checks the
WETH-vault dead end. Nothing touches mainnet: every RPC call goes to
`127.0.0.1` and must answer chain id 4663, and the keys are anvil's public dev keys.

Results from the 2026-11-05 run: [RESULTS-2026-11-05.md](RESULTS-2026-11-05.md).
Unsigned propose calldata from that run: [results/2026-11-05/](results/2026-11-05/).

## Files

| File | What it does |
|---|---|
| `start-anvil.sh` | Starts anvil, forked at a pinned block, on `127.0.0.1:$FORK_PORT` (default 8545). Run it under `railway run` (see below). Detaches anvil and writes `out/anvil.pid` and `out/anvil.log`, with URLs redacted. |
| `install-cli.sh` | Installs `@sherwoodagent/cli@0.102.0` into `.cli/` (gitignored), checking the tarball's integrity. |
| `sw.sh` | Wrapper for the CLI, with an isolated HOME and production API writes blocked. It takes `RPC_URL`, `CHAIN_ID` and `SIGNER` as parameters (see [Mainnet use](#mainnet-use)). With `SIGNER=anvil` (the default) it signs with the dev key for `SW_ROLE` and allows only a local RPC. |
| `plan.sh` | External-signer driver, for mainnet or a fork. `plan.sh <step> ...` builds one loop step as unsigned `--calldata-only` JSON and appends it to an ordered `plan.md` "send these in order" list. It never signs or sends. |
| `fund-accounts.sh` | Fork only. Clears the EIP-7702 code on the dev accounts and sets their ETH, WOOD and USDG balances through storage slots. Idempotent. |
| `loop.sh` | Fork only. Runs the loop in stages: `identity create members agent p1 p2 review execute settle redeem weth`. Each finished stage is recorded in `out/state.env`, so a rerun skips it. |
| `lib.sh` | Shared addresses, parameters and helpers. `step` logs every tx a CLI call mined. `send_calldata` signs a `--calldata-only` JSON with cast (`SIGNER=anvil`) or appends it to the plan (`SIGNER=external`). Also `warp` and `expect_revert`, both fork only. |

Roles (anvil dev accounts 0 to 5): host/owner `0xf39F…2266`, agent `0x7099…79C8`,
member A `0x3C44…93BC`, member B `0x90F7…b906`, WETH-vault host `0x15d3…6A65`,
guardian `0x9965…A4dc`.

## Start the fork

You need foundry (anvil, cast), node and npm, jq, and the `railway` CLI logged in with
access to project `sherwood-arena`.

The upstream archive RPC is a secret Railway variable, `FORK_UPSTREAM_RPC_URL`, on
service `anvil` in environment `fork`. It must never land in a file, a shell history or
a transcript. `railway run` passes it to `start-anvil.sh` in the environment only.
Link from a directory outside any git checkout, so the link file stays out of the repo:

```bash
mkdir -p /tmp/fork-link && cd /tmp/fork-link
railway link -p 538c6d19-c3ee-4fa4-bf6e-94da30e51852 -e fork -s anvil
railway run -- bash /abs/path/to/scripts/fork/start-anvil.sh 106883230   # or omit the block: upstream latest - 5
```

Do not run `ps aux` or `ps -ef` while the fork is up. anvil takes the URL as an argument,
so it shows in the process list. Use `pgrep -x anvil` or `lsof -nP -iTCP:8545 -sTCP:LISTEN`.
Stop the fork with `kill "$(cat scripts/fork/out/anvil.pid)"`.

**Pick the block on a weekday during US market hours.** Chainlink stock feeds stop
publishing over the weekend. PortfolioStrategy's execute, settle and rebalance revert
`StalePrice()` once a feed is more than 26h old, and on a fork the feeds never update.
Keep the total time you warp below `min(feed updatedAt) + 26h - fork timestamp`.
`loop.sh` warps about 3.5h in total.

## Run the loop

```bash
scripts/fork/install-cli.sh
export FORK_RPC_URL=http://127.0.0.1:8545      # FORK_OUT_DIR defaults to scripts/fork/out
scripts/fork/fund-accounts.sh
scripts/fork/loop.sh                           # every stage; or e.g. `loop.sh p2 review`
```

It takes about 10 minutes. Outputs:

- `out/steps/<step>.out`: the full CLI output of each step.
- `out/steps/<step>.txs`: every tx the step mined (hash, from, to, selector, gas, status).
- `out/steps/notes.txt`: probe results and `expect_revert` checks (PASS/FAIL with the selector).
- `out/summary.tsv`: one row per tx for the whole run.
- `out/state.env`: identity ids, vault and governor addresses, proposal ids, finished stages.

To run from scratch, restart anvil and delete `out/state.env`.

## What it proves

1. ERC-8004 identities mint for the host, agent, guardian and WETH host. `vault create`
   needs no `prepareOwnerStake`, because `minOwnerStake()` is 0. The creation fee is
   1,000,000 WOOD.
2. The USDG vault starts with whitelisted deposits, a 1800 s voting period and a 5%
   agent fee. Members A (1,000) and B (250) deposit, so B holds exactly 20%.
3. The agent is registered on the vault but cannot propose: the CLI refuses in its
   preflight, and on-chain the same calldata reverts `ProposerNotOwner()`.
4. The owner proposes P1 through the external-signer path: `--calldata-only`, then
   `cast send`. Member B votes Against with 20% of the supply, and P1 ends `Rejected`. Its bond
   comes back right away.
5. After the 1h cooldown, the owner proposes P2 and nobody objects. A staked guardian opens
   the review and approves with a WOOD lock, which gives full coverage. Then
   `resolve-reviews`, `execute` (three stock legs bought), `settle` by the proposer after 1h,
   and member A redeems.
6. Execute has no path without a guardian's coverage. If no guardian approves, P2 is
   `Approved` after the review, but `executeProposal` reverts
   `InsufficientApproveCoverage()`.
7. A WETH vault can be created and funded, but its propose fails. The CLI refuses with
   `AssetNotUsdPegged`; on-chain, the ledger and the governor revert `FeedNotConfigured()`.

## Gotchas these scripts already handle

- **`~/.sherwood/config.json` takes precedence over the env vars.** The CLI reads `privateKey` and
  `rpc.<network>` from the config before it looks at `PRIVATE_KEY` or `ROBINHOOD_RPC_URL`.
  So `sw.sh` always runs the CLI with `HOME=out/home/<role>`. Never run the bare CLI
  against a fork.
- **The CLI falls back to the public RPC.** If the local RPC fails, the CLI tries
  `rpc.mainnet.chain.robinhood.com` unless both `SIM_CHAIN_ID` and `SIM_ADDRESSES_FILE`
  are set. `sw.sh` sets them (4663 and `{}`).
- **The anvil dev accounts carry EIP-7702 sweeper code on 4663 mainnet** (code
  `0xef0100 8a67b5…7e408a`). `fund-accounts.sh` clears it with `anvil_setCode`.
- **`anvil_dealERC20` cannot find the balance slot for WOOD or USDG.** WOOD balances
  are at slot 0 and USDG balances (behind a proxy) at slot 1.
- **Signed `strategy propose` always pins metadata to `api.sherwood.sh`**, even with
  `--metadata-uri`, so it fails when that API is unreachable. The fork uses
  `--calldata-only` and signs the txs with cast (`send_calldata`).
- **Signed `vault create` and `vault add` try to create or join an XMTP group on the production
  XMTP network.** `SHERWOOD_SKIP_XMTP` only affects `session check`, so `loop.sh` builds both
  with `--calldata-only` and signs them with cast. No XMTP client starts.
- **`proposal execute --dry-run` exits 1 on a fork even when execute will succeed.** The
  simulation API cannot see fork state, and the eth_call fallback reverts
  `NotActiveProposalStrategy`. Read its `Coverage` line and do not gate on its exit code.
- **The CLI guardian review refuses to Approve a 20% agent fee.** That is the vault
  default and also the governor cap, and the review reports `EXCESSIVE_PERFORMANCE_FEE`.
  `loop.sh` sets the fee to 5%.

## Mainnet use

From the first mainnet PR on, testing moves to Robinhood Chain mainnet with tiny USDG
amounts, and every spend waits for Carlos's GO. The same scripts drive mainnet through an
**external signer**: they print unsigned calldata and an ordered "send these in order" list.
They never hold or read a key.

Parameters (env), shared by `sw.sh`, `lib.sh` and `plan.sh`:

| Var | Fork | Mainnet |
|---|---|---|
| `RPC_URL` | `http://127.0.0.1:8545` (default) | an RPC you trust for chain 4663. Keep keyed URLs in the environment, never in a file |
| `CHAIN_ID` | `4663` (default) | `4663`. Every script checks that the RPC answers it, and every calldata tx carries it |
| `SIGNER` | `anvil` (default): anvil dev keys, local RPC only | `external`: no key at all; `sw.sh` runs only `--calldata-only` or read-only commands |
| `FORK_OUT_DIR` | `scripts/fork/out` | any directory; `plan.md` and `plan/NN-<step>.json` are written there |

```bash
export SIGNER=external CHAIN_ID=4663 RPC_URL=...   # mainnet RPC, from the environment
export FORK_OUT_DIR=$PWD/mainnet-run
scripts/fork/install-cli.sh
scripts/fork/plan.sh approve  <vault> <member>                 # host signs
scripts/fork/plan.sh deposit  <vault> <member> 5               # member signs: USDG approve + deposit
scripts/fork/plan.sh propose  <vault> <host> ipfs://<CID> 2 NVDA,AAPL,SPY 3400,3300,3300 v3:500,v3:500,v4:500:5 125
cat "$FORK_OUT_DIR/plan.md"                                    # the ordered list to sign
```

`plan.md` is a table of step, signer, `to`, selector, value and the JSON file and index of each tx.
Send the txs in that order from the named wallet (MetaMask agent wallet, Privy, a Safe, or anything
that takes EIP-5792 `{to, data, value, chainId}`).

- Build each step right before sending it. The CLI reads live state: the bond quote, whether the
  clone exists, and the cooldown. Steps also wait on the clock: voting 30 min, review 30 min,
  settle at least 1h after execute. Check `SIGNER=external scripts/fork/sw.sh proposal status <id> --vault <v>`
  between steps.
- Pin metadata yourself (`sherwood pin-metadata`, outside these scripts) and pass the
  `ipfs://` URI. `sw.sh` blocks the pinning API so nothing leaves by accident.
- `loop.sh` and `fund-accounts.sh` are fork only. They warp time and set balances, and they refuse
  `SIGNER=external`. The full ordered mainnet checklist, with the method, target contract and
  signer of every tx, is in [RESULTS-2026-11-05.md](RESULTS-2026-11-05.md#ordered-tx-list-for-the-loop-mainnet-checklist).
- Before the first mainnet propose: set the agent fee below 16% (`plan.sh agent-fee <vault> 500`)
  and make sure a staked guardian will Approve with enough WOOD lock, or execute reverts
  `InsufficientApproveCoverage`.
- On a fork, `plan.sh` can preview mainnet calldata without sending anything:
  `SIGNER=external RPC_URL=http://127.0.0.1:8545 scripts/fork/plan.sh ...`. It was smoke-tested
  this way for `approve`, `deposit` and `propose`. The other steps are wired the same way but
  have not run under `SIGNER=external`.
