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
