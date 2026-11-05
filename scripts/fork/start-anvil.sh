#!/usr/bin/env bash
# FORK ONLY. Start a local anvil fork of Robinhood Chain mainnet (chain 4663) on 127.0.0.1.
#
# The upstream archive RPC is a secret. It is read ONLY from the environment variable
# FORK_UPSTREAM_RPC_URL, which `railway run` injects from the Railway variable of the same
# name (project / environment / service: see the SHE-375 context notes, not this repo).
# It is never written to a file, cast gets it through ETH_RPC_URL (environment, not argv),
# and every line anvil prints goes through a URL redactor before it reaches the log.
#
# Usage (from a directory that is NOT inside a git repo, so the railway link file stays out
# of the tree; link it to the fork service named in the context notes):
#   railway link            # project / environment / service from the SHE-375 context notes
#   railway run -- bash /abs/path/scripts/fork/start-anvil.sh [BLOCK]
#
#   BLOCK          fork block number (default: upstream latest - 5, recorded in fork-block.txt)
#   FORK_PORT      default 8545
#   FORK_OUT_DIR   default scripts/fork/out (gitignored; log: anvil.log, pid: anvil.pid)
#
# The script daemonizes anvil and returns once the fork answers chain id 4663.
# Stop it with: kill "$(cat scripts/fork/out/anvil.pid)"
#
# anvil has no environment form of --fork-url, so the URL IS in anvil's argv. Do NOT run
# `ps aux` / `ps -ef` while the fork is up; use `pgrep -x anvil` or
# `lsof -nP -iTCP:8545 -sTCP:LISTEN` instead.
set -euo pipefail
export FOUNDRY_DISABLE_NIGHTLY_WARNING=1
export PATH="$PATH:/usr/sbin:/sbin:$HOME/.foundry/bin"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/guard.sh"
OUT="${FORK_OUT_DIR:-$HERE/out}"
PORT="${FORK_PORT:-8545}"
ANVIL="${ANVIL:-$(command -v anvil || echo "$HOME/.foundry/bin/anvil")}"
CAST="${CAST:-$(command -v cast || echo "$HOME/.foundry/bin/cast")}"
BLOCK="${1:-${FORK_BLOCK:-}}"
mkdir -p "$OUT"
LOG="$OUT/anvil.log"

upstream() { ETH_RPC_URL="$FORK_UPSTREAM_RPC_URL" "$CAST" "$@" 2>&1 | redact; }
utc() { date -u -r "$1" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d "@$1" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || true; }

if [ -z "${FORK_UPSTREAM_RPC_URL:-}" ]; then
  echo "start-anvil: FORK_UPSTREAM_RPC_URL is not set. Run this under 'railway run' (see header)." >&2
  exit 2
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "start-anvil: something already listens on 127.0.0.1:$PORT; not starting a second fork." >&2
  exit 1
fi

up_cid="$(upstream chain-id)"
if [ "$up_cid" != "4663" ]; then
  echo "start-anvil: upstream answered chain id '$up_cid', want 4663" >&2
  exit 2
fi
if [ -z "$BLOCK" ]; then
  latest="$(upstream block-number)"
  BLOCK=$((latest - 5))
fi
ts="$(upstream block "$BLOCK" --field timestamp)"
echo "$BLOCK" > "$OUT/fork-block.txt"
echo "start-anvil: forking 4663 at block $BLOCK (timestamp $ts, $(utc "$ts"))"

: > "$LOG"
# Detach fully (new session via POSIX setsid, stdio closed) so the fork outlives this shell,
# `railway run` and any agent harness that started it. The URL reaches anvil's argv from the
# inherited environment inside the child shell; it is never expanded on this command line.
export ANVIL PORT BLOCK LOG
export REDACT_RE='[A-Za-z][A-Za-z0-9+.-]*://[^[:space:]"]+'
nohup perl -MPOSIX -e 'POSIX::setsid() or die "setsid: $!"; exec @ARGV or die "exec: $!"' \
  bash -c '"$ANVIL" --host 127.0.0.1 --port "$PORT" --chain-id 4663 \
      --fork-url "$FORK_UPSTREAM_RPC_URL" --fork-block-number "$BLOCK" \
      --fork-retry-backoff 1000 --retries 10 --timeout 60000 2>&1 \
    | sed -u -E "s#${REDACT_RE}#<redacted-url>#g" >> "$LOG"' \
  > /dev/null 2>&1 < /dev/null &

LOCAL="http://127.0.0.1:$PORT"
for _ in $(seq 1 90); do
  if cid="$(ETH_RPC_URL="$LOCAL" "$CAST" chain-id 2>/dev/null)" && [ "$cid" = "4663" ]; then
    pid="$(lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN | head -1)"
    echo "$pid" > "$OUT/anvil.pid"
    echo "start-anvil: up. pid=$pid rpc=$LOCAL log=$LOG block=$(ETH_RPC_URL="$LOCAL" "$CAST" block-number)"
    exit 0
  fi
  sleep 1
done
echo "start-anvil: fork did not answer within 90s; see $LOG" >&2
exit 1
