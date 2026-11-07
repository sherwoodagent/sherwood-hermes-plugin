# Sherwood Monitor — Boot

For each fund in `~/.hermes/plugins/sherwood-monitor/config.yaml`:

1. Call `sherwood_monitor_status()` and report each fund's state
   (`pid`, `uptime_seconds`, `events_seen`, `last_event_at`).
2. Streams run for the life of this Hermes process. With `auto_start: true`
   they start at the first turn by themselves; if a fund still has no live
   supervisor, call `sherwood_monitor_start(subdomain)`. Ending a turn or
   `/new` does not stop them; Hermes shutdown does. Events between turns
   arrive as `<sherwood-event>` blocks on your next turn.
3. If `on_session_start` injected any `<sherwood-catchup>` blocks,
   summarize them briefly for the user (new proposals, settlements,
   risk alerts) so they know the state of their funds at session start.

If `sherwood_monitor_status()` returns an empty list, note that no
funds are configured and remind the user how to add one:
`edit ~/.hermes/plugins/sherwood-monitor/config.yaml`.

On-chain events reach you through `sherwood session check --stream
--no-xmtp`, for every fund. XMTP is optional and works only for funds whose
XMTP group id is in their ENS record on Base; Robinhood Chain funds have
none, so never tell a user to add the sidecar to a Robinhood Chain fund's
group: it cannot help.

The gas watchdog needs only `SHERWOOD_AGENT_ADDRESS` (the monitor wallet's
address) in the Hermes profile `.env`. `SHERWOOD_PRIVATE_KEY` (secret) is
needed only for XMTP; the address is derived from it when unset. Without
either, the gas watchdog is off and everything else works; the plugin says
so once in a `<sherwood-monitor-warning>` when funds are configured. Since
0.7.0 the plugin never reads the Sherwood CLI's `~/.sherwood/config.json`.
If a user upgraded from 0.6.0, tell them to set `SHERWOOD_AGENT_ADDRESS`
(and the key only if they use XMTP, a dedicated monitor key, never a
fund-managing key). Never ask for a key in chat.

## Group-chat fund (one-time check per session)

If the fund settings (the `fund:` section of the config, or the plugin's
settings in Hermes) name a `default_fund`, call
`fund_status()` and tell the user, in one or two lines: members, agents,
open proposals and claims, whether deposits are open (`deposits_open`), and
any `notes` it returns (agent fee unknown or over 1500 bps, a WETH fund that
cannot propose, a cooldown). If no fund is configured, say nothing about
funds.

With `proposal_signer: agent` (the open protocol) the host is your own
wallet, the one the `sherwood` CLI holds; the CLI signs, the plugin never
does. Never run pending commands on boot, and not just because a session
has a terminal. Run them only in a `hermes -z` one-shot the operator
started for that purpose, or a cron the operator configured with
`approvals.cron_mode: approve`. There: call `fund_pending_commands`, run each
listed `command` verbatim, then record what landed at once as its `then`
says (`fund_set_proposal_status ... proposed` with `onchain_id` and
`tx_hash`, or `fund_approve_agent` with `tx_hash`). A cron in deny mode must
not run them: it cannot record the result, and an unrecorded propose comes
back. Respect `held_back`, `chain_hold`, `expired` and `cli_warning`. In the
group chat you have no terminal: never try; say the proposing agent runs its
command outside the chat.

What the plugin registers (v0.8.1): 17 tools, 5 hooks, 5 slash commands,
4 skills, 6 `hermes sherwood` subcommands and one Telegram platform handler.

- **Monitor tools** (toolset `sherwood-monitor`): `sherwood_monitor_start`,
  `sherwood_monitor_stop`, `sherwood_monitor_status`,
  `sherwood_monitor_exposure`, `sherwood_monitor_cron_tick`.
- **Fund tools** (toolset `sherwood-fund`): `fund_status`, `fund_setup`,
  `fund_claim_topic`, `fund_release_claim`, `fund_draft_proposal`,
  `fund_set_proposal_status`, `fund_record_vote`, `fund_record_settle`,
  `fund_approve_member`, `fund_approve_agent`, `fund_remove_agent`,
  `fund_pending_commands` (the CLI commands still to run under
  `proposal_signer: agent`; it stamps the propose commands it hands out).
  The host-authority ones (`fund_setup`, `fund_approve_*`,
  `fund_remove_agent`, `fund_record_settle`, `fund_set_proposal_status`,
  and `fund_record_vote` for someone else's vote) refuse anyone but the
  fund's host and ask a human to approve each call.
- **Hooks:** `on_session_start` (catch-up, then make sure the streams run),
  `on_session_finalize` (stops the streams and the XMTP sidecar only when
  Hermes shuts down, `reason="shutdown"`; `atexit` is the fallback), `pre_tool_call` (risk checks on
  `sherwood proposal create` / `strategy propose`, the fund rules and the
  host check), `post_tool_call` (settlement memory), `pre_llm_call` (one
  hook: queued events plus the Telegram pitch context).
- **Slash commands:** `/fund`, `/join-fund <fund>` (owner only: brings this
  Hermes into a fund as a co-manager), `/fund-approve`, `/fund-agents`,
  `/leaderboard`. Telegram's menu shows them with `_` for `-`.
- **Skills:** `sherwood-agent` (the Sherwood CLI skill pack), `fund-pitch`,
  `fund-propose`, `fund-settle`.
- **CLI** (`hermes sherwood <command>`, from the operator's shell): `start`,
  `stop`, `status`, `tail`, `install-cron`, `fund-bind`. `fund-bind`
  changes a fund's vault, governor, chat or host; it refuses inside a chat
  turn. Never suggest it in a group chat.
- **Telegram:** a platform handler that Hermes wires only when its Telegram
  adapter connects (it needs `TELEGRAM_BOT_TOKEN`). It posts fund,
  proposal, draft and receipt cards, counts 🔥 on pitches, and answers
  buttons with personal links to `app.sherwood.sh/tg/...`, where wallets
  sign. Without a token or python-telegram-bot the cards stay off and
  everything else works.

The plugin never signs or sends a transaction. It builds unsigned ones
through the prepare API (`tx_builder_url`, default
`https://api.sherwood.sh/api/v1`); with `proposal_signer: host` the host
signs owner actions on `/tg/sign`, members sign from their own wallets, and
the agent signs only from its own scoped wallet (its WOOD bond, its
settles). With `proposal_signer: agent` the host is the agent's own wallet:
the plugin hands it the exact `sherwood` CLI commands (propose, register a
guest agent) and the agent signs them with the CLI, outside the plugin. On a
fund with open deposits there is no member to approve: members deposit
straight from the card.

Before you run a fund in a Telegram group, the operator must have set the
deployment boundary in the README ("Group-chat fund"): no terminal, file or
code tools in the group's toolset, `plugins.isolation: in_process`,
`busy_input_mode: interrupt` in forum topics, and host approvals in a
private chat, never "Always". If the group toolset still has `terminal`,
say so to the operator. With `proposal_signer: agent` the operator also
needs the Sherwood CLI 0.103.0 or later on this machine (older releases
target another network or lack the bond escrow on the propose path);
`fund_pending_commands` says so in `cli_warning` when it is older. The
fund features work with the gateway's `multiplex_profiles` on or off; if a
member reports "Links are not configured yet" while `SHERWOOD_TG_LINK_SECRET`
is set in this profile's `.env`, tell the operator to check the gateway log
for a secret-scope warning from the plugin. After changing any
`SHERWOOD_TG_*` value the operator restarts the gateway.

## Cron setup (one-time)

Run `hermes sherwood install-cron` from the shell. It writes one wrapper
per watchdog to `$HERMES_HOME/scripts/sherwood-monitor-<mode>.py` (Hermes
runs cron scripts only from there, with no arguments) and registers, or
updates, five crons:

- Four **no_agent** watchdogs (zero LLM tokens — Hermes runs the script and
  delivers any stdout verbatim):
  - `sherwood-monitor-digest` (every 15m) — bullet-formats new
    proposals/settlements/risk alerts
  - `sherwood-aum-watchdog` (every 15m) — alerts when TVL Δ exceeds threshold
  - `sherwood-gas-watchdog` (every 30m) — alerts when agent wallet ETH is low
  - `sherwood-stream-watchdog` (every 5m) — alerts when a running fund
    stream goes stale, its `sherwood session check` process dies, or Hermes
    is gone (it reads `stream_status.json`, written by the supervisor)
- One **agent** cron (`sherwood-proposal-reasoning`, every 6h) — the only
  cron that costs LLM tokens. It lists open proposals and returns a vote
  recommendation per proposal; silent when no proposals are open.

If the user hasn't run `install-cron` yet, or upgraded from 0.6.0 (whose
`install-cron` registered nothing on Hermes 0.22), suggest it. The
chat-fallback path (registering crons via the built-in `cronjob` tool) is no
longer practical now that we register five entries; tell the user to run the
shell command instead.

`install-cron` is idempotent: output JSON lists which entries were
`installed`, `updated` (already registered, brought back to this
definition), `skipped` (disabled in config), or returned an `error`. Re-run
it if the plugin moves; the `.py` wrappers run on Hermes' current dependency
environment, so Hermes updates need nothing.
