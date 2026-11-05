# Sherwood Monitor — Boot

For each fund in `~/.hermes/plugins/sherwood-monitor/config.yaml`:

1. Call `sherwood_monitor_status()` and report each fund's state
   (`pid`, `uptime_seconds`, `events_seen`, `last_event_at`).
2. If `auto_start` is true and a fund has no live supervisor,
   call `sherwood_monitor_start(subdomain)`.
3. If `on_session_start` injected any `<sherwood-catchup>` blocks,
   summarize them briefly for the user (new proposals, settlements,
   risk alerts) so they know the state of their funds at session start.

If `sherwood_monitor_status()` returns an empty list, note that no
funds are configured and remind the user how to add one:
`edit ~/.hermes/plugins/sherwood-monitor/config.yaml`.

## Group-chat fund (one-time check per session)

If the fund settings (the `fund:` section of the config, or the plugin's
settings in Hermes) name a `default_fund`, call
`fund_status()` and tell the user, in one or two lines: members, agents,
open proposals and claims, and any `notes` it returns (agent fee unknown or
over 1500 bps, a WETH fund that cannot propose, a cooldown). If no fund is
configured, say nothing about funds.

What the plugin registers (v0.7.0): 16 tools, 5 hooks, 5 slash commands,
4 skills, 6 `hermes sherwood` subcommands and one Telegram platform handler.

- **Monitor tools** (toolset `sherwood-monitor`): `sherwood_monitor_start`,
  `sherwood_monitor_stop`, `sherwood_monitor_status`,
  `sherwood_monitor_exposure`, `sherwood_monitor_cron_tick`.
- **Fund tools** (toolset `sherwood-fund`): `fund_status`, `fund_setup`,
  `fund_claim_topic`, `fund_release_claim`, `fund_draft_proposal`,
  `fund_set_proposal_status`, `fund_record_vote`, `fund_record_settle`,
  `fund_approve_member`, `fund_approve_agent`, `fund_remove_agent`.
  The host-authority ones (`fund_setup`, `fund_approve_*`,
  `fund_remove_agent`, `fund_record_settle`, `fund_set_proposal_status`,
  and `fund_record_vote` for someone else's vote) refuse anyone but the
  fund's host and ask a human to approve each call.
- **Hooks:** `on_session_start` (catch-up), `on_session_end` (stop
  supervisors and the XMTP sidecar), `pre_tool_call` (risk checks on
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
`https://api.sherwood.sh/api/v1`); the host signs owner actions on
`/tg/sign`, members sign from their own wallets, and the agent signs only
from its own scoped wallet (its WOOD bond, its settles, and its proposals
when `proposal_signer: agent`).

Before you run a fund in a Telegram group, the operator must have set the
deployment boundary in the README ("Group-chat fund"): no terminal, file or
code tools in the group's toolset, `plugins.isolation: in_process`,
`busy_input_mode: interrupt` in forum topics, and host approvals in a
private chat, never "Always". If the group toolset still has `terminal`,
say so to the operator.

## Cron setup (one-time)

Run `hermes sherwood install-cron` from the shell. It idempotently registers
five crons:

- Four **no_agent** watchdogs (zero LLM tokens — Hermes runs the script and
  delivers any stdout verbatim):
  - `sherwood-monitor-digest` (every 15m) — bullet-formats new
    proposals/settlements/risk alerts
  - `sherwood-aum-watchdog` (every 15m) — alerts when TVL Δ exceeds threshold
  - `sherwood-gas-watchdog` (every 30m) — alerts when agent wallet ETH is low
  - `sherwood-stream-watchdog` (every 5m) — alerts when a fund's
    supervisor stream goes stale or its supervisor PID dies
- One **agent** cron (`sherwood-proposal-reasoning`, every 6h) — the only
  cron that costs LLM tokens. It lists open proposals and returns a vote
  recommendation per proposal; silent when no proposals are open.

If the user hasn't run `install-cron` yet, suggest it. The chat-fallback
path (registering crons via the built-in `cronjob` tool) is no longer
practical now that we register five entries; tell the user to run the
shell command instead.

`install-cron` is idempotent — output JSON lists which entries were
`installed`, `skipped` (already registered or disabled in config), or
returned an `error`.
