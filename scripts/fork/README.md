# 4663 fork runbook: the PortfolioStrategy loop on a local anvil fork (FORK ONLY)

Everything in `scripts/fork/` is **fork-only tooling**. It runs the Sherwood fund loop on a
local anvil fork of Robinhood Chain mainnet (chain 4663) with Sherwood CLI 0.102.0. The loop
covers ERC-8004 identities, a USDG vault with whitelisted deposits, members depositing, an
agent that cannot propose, an owner proposal that a 20% holder vetoes, an owner proposal
nobody objects to, guardian review, execute, settle and redeem. It also checks the WETH-vault
dead end.

Every script refuses an RPC that is not exactly `http://127.0.0.1:<port>` or
`http://localhost:<port>`. It also refuses one whose `eth_chainId` is not 4663, and signing
requires `web3_clientVersion` = `anvil/*`. The only keys are anvil's public dev keys.

Results from the 2026-11-05 runs: [RESULTS-2026-11-05.md](RESULTS-2026-11-05.md). The propose
calldata captured on the fork is in [results/2026-11-05/](results/2026-11-05/). It is FORK-ONLY:
never sign it on mainnet.

## Mainnet

These scripts do not drive mainnet. Mainnet signing goes through the Sherwood **prepare API**
and the app's **`/tg/sign`** page. `plan.sh` is fork-only: it previews what each loop step sends
on the fork. The ordered tx list in RESULTS (method, target, signer) is a reference for what the
loop sends, not something to sign from here.

## Files

| File | What it does |
|---|---|
| `start-anvil.sh` | Starts anvil, forked at a pinned block, on `127.0.0.1:$FORK_PORT` (default 8545). Run it under `railway run` (see below). It detaches anvil and writes `anvil.pid` and `anvil.log` with URLs redacted. cast gets the upstream URL through `ETH_RPC_URL`. |
| `install-cli.sh` | Installs `@sherwoodagent/cli@0.102.0` into `.cli/` (gitignored). The tarball sha512 is pinned in the script, every dependency is pinned by `cli-pin/package-lock.json`, and the install is `npm ci --ignore-scripts`. |
| `guard.sh` | Shared hard guards: exact loopback host match, `eth_chainId` 4663, anvil-only signing, refusal of foreign output dirs, and an output redactor. |
| `sw.sh` | Fork-only CLI wrapper, `SW_ROLE=host scripts/fork/sw.sh vault info ...`. It runs the CLI under `env -i` with an explicit minimal environment, from a fresh empty working directory per call, with HOME set to `out/home/<role>`. Every outbound URL the CLI reads is forced to a dead port, and `netguard.cjs` blocks non-loopback connections. `--calldata-only` runs with no key and only for the subcommands that honour it. |
| `netguard.cjs` | Node preload: refuses every non-loopback TCP connection and DNS lookup from the CLI process, and logs each attempt to `$FORK_OUT_DIR/netlog.tsv`. |
| `fund-accounts.sh` | Clears the EIP-7702 code on the dev accounts and sets their ETH, WOOD and USDG balances through storage slots. Idempotent. |
| `loop.sh` | Runs the loop in stages: `identity create members agent p1 p2 review execute settle redeem weth`. Each finished stage is recorded in `state.env` (shell-quoted), so a rerun skips it. |
| `plan.sh` | Fork-only preview. `plan.sh <step> ... [+ <step> ...]` builds unsigned calldata against the fork into a new `plans/<UTC>-b<block>/plan.md`, stamped with block, hash, chain, RPC host and CLI version. It never signs. |
| `lib.sh` | Shared addresses and helpers: `step` logs every tx a CLI call mined, `send_calldata` signs a `--calldata-only` JSON with cast and fails closed on any RPC error, plus `warp` and `has_code`. |

Roles (anvil dev accounts 0 to 5): host/owner `0xf39F…2266`, agent `0x7099…79C8`,
member A `0x3C44…93BC`, member B `0x90F7…b906`, WETH-vault host `0x15d3…6A65`,
guardian `0x9965…A4dc`.

## Start the fork

You need foundry (anvil, cast), node and npm, jq, perl, and the `railway` CLI logged in with
access to the project named in the SHE-375 context notes.

The upstream archive RPC is a secret Railway variable, `FORK_UPSTREAM_RPC_URL`. The project,
environment and service are in the context notes, not in this repo. The URL must never land in
a file, a shell history or a transcript; `railway run` passes it to `start-anvil.sh` in the
environment only. Link from a directory outside any git checkout, so the link file stays out of
the repo:

```bash
mkdir -p /tmp/fork-link && cd /tmp/fork-link
railway link                                     # pick the project/environment/service from the context notes
railway run -- bash /abs/path/to/scripts/fork/start-anvil.sh 106883230   # or omit the block: upstream latest - 5
```

anvil has no environment form of `--fork-url`, so the URL is in anvil's argv. Do not run
`ps aux` or `ps -ef` while the fork is up; use `pgrep -x anvil` or
`lsof -nP -iTCP:8545 -sTCP:LISTEN`. Stop it with `kill "$(cat <FORK_OUT_DIR>/anvil.pid)"`.

**Pick the block on a weekday during US market hours.** Chainlink stock feeds stop publishing
over the weekend. PortfolioStrategy's execute, settle and rebalance revert `StalePrice()` once a
feed is more than 26h old, and on a fork the feeds never update. `loop.sh` warps about 3.5h in total.

## Run the loop

```bash
scripts/fork/install-cli.sh
export RPC_URL=http://127.0.0.1:8545
export FORK_OUT_DIR=/tmp/fork-run-$(date +%s)     # a fresh dir; scripts/fork/out/ and scripts/fork/*-run/ are gitignored
scripts/fork/fund-accounts.sh
scripts/fork/loop.sh                              # every stage; or e.g. `loop.sh p2 review`
```

It takes about 2.5 minutes. Outputs in `$FORK_OUT_DIR`:

- `steps/<step>.out`: the full CLI output of each step.
- `steps/<step>.txs`: every tx the step mined (hash, from, to, selector, gas, status).
- `steps/notes.txt`: probe results and `expect_revert` checks (PASS/FAIL with the selector).
- `summary.tsv`: one row per tx for the whole run.
- `netlog.tsv`: every connection the CLI attempted, with ALLOW for loopback and BLOCK otherwise.
- `state.env`: identity ids, vault and governor addresses, proposal ids, finished stages.

To run from scratch, restart anvil and use a new `FORK_OUT_DIR`. The scripts refuse an output
dir that holds a `sim-addresses.json`: older versions wrote one there, and a populated one would
redirect the CLI's contract addresses.

## What it proves

1. ERC-8004 identities mint for the host, agent, guardian and WETH host. `vault create` needs
   no `prepareOwnerStake`, because `minOwnerStake()` is 0. The creation fee is 1,000,000 WOOD.
2. The USDG vault starts with whitelisted deposits, a 1800 s voting period and a 5% agent fee.
   Members A (1,000) and B (250) deposit, so B holds exactly 20%.
3. The agent is registered but cannot propose: the CLI refuses in its preflight, and on-chain
   the same calldata reverts `ProposerNotOwner()`.
4. P1 goes through `--calldata-only` and cast signing. Member B votes Against with 20%, so P1
   ends `Rejected`, and its bond comes back at once.
5. After the 1h cooldown, P2 is proposed and nobody objects. A staked guardian opens the review
   and approves with a WOOD lock, which gives full coverage. Then `resolve-reviews`,
   `execute` (three stock legs bought), `settle` by the proposer after 1h, and member A redeems.
6. Execute has no path without a guardian's coverage. If no guardian approves, `executeProposal`
   reverts `InsufficientApproveCoverage()`.
7. A WETH vault can be created and funded, but its propose fails. The CLI refuses with
   `AssetNotUsdPegged`; on-chain, the ledger and the governor revert `FeedNotConfigured()`.

## Gotchas these scripts handle

- **`~/.sherwood/config.json` takes precedence over the env vars.** Its `privateKey` and `rpc`
  win over `PRIVATE_KEY` and `ROBINHOOD_RPC_URL`, so `sw.sh` always runs the CLI with an isolated
  HOME. Never run the bare CLI against a fork.
- **The CLI calls `dotenv.config()` on its working directory.** A `.env` holding `PRIVATE_KEY`
  would be loaded even after the variable is unset. `sw.sh` uses `env -i` and a fresh empty
  working directory per call.
- **Three commands ignore `--calldata-only` and sign directly** in CLI 0.102.0:
  `vault set-agent-fee`, `proposal open-reviews` and `proposal resolve-reviews`. `sw.sh` refuses
  them under `--calldata-only`.
- **The CLI falls back to the public RPC** unless `SIM_CHAIN_ID` and `SIM_ADDRESSES_FILE` are both
  set. `sw.sh` sets them, with a `{}` address book written fresh into the per-call directory.
- **Signed commands phone home.** `proposal review-vote` POSTs an EIP-712 verdict to the
  production backoffice indexer, `identity mint` posts to the metadata mirror, and signed
  `vault create`/`add` start an XMTP client. `sw.sh` forces every URL variable to
  `http://127.0.0.1:9` and preloads `netguard.cjs`. It also refuses signed
  `vault create|add|join|approve` and `chat`; `loop.sh` builds those with `--calldata-only`.
- **The anvil dev accounts carry EIP-7702 sweeper code on 4663** (`0xef0100 8a67b5…7e408a`).
  `fund-accounts.sh` clears it.
- **`anvil_dealERC20` cannot find the balance slot for WOOD or USDG.** WOOD balances are at
  slot 0 and USDG balances (behind a proxy) at slot 1.
- **Signed `strategy propose` always pins metadata to `api.sherwood.sh`**, even with
  `--metadata-uri`. The loop proposes with `--calldata-only` plus cast.
- **`proposal execute --dry-run` exits 1 on a fork even when execute will succeed**
  (`NotActiveProposalStrategy` in the eth_call fallback). Read its `Coverage` line instead.
- **The CLI guardian review refuses to Approve a 20% agent fee** (`EXCESSIVE_PERFORMANCE_FEE`).
  `loop.sh` sets the fee to 5%. On a fork `review-vote` also needs `--accept-warnings`, because
  no simulation API can see fork state.
