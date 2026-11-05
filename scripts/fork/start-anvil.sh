#!/usr/bin/env bash
# Start a local anvil fork of Robinhood Chain mainnet (chain 4663) on 127.0.0.1.
#
# The upstream archive RPC (Alchemy) is a secret. It is read ONLY from the environment
# variable FORK_UPSTREAM_RPC_URL, which `railway run` injects from the Railway variable on
# project sherwood-arena / environment fork / service anvil. It is never written to a file,
# and every line anvil prints goes through a URL redactor before it reaches the log.
#
# Usage (from a directory that is NOT inside a git repo, so the railway link file stays out
# of the tree):
#   railway link -p 538c6d19-c3ee-4fa4-bf6e-94da30e51852 -e fork -s anvil
#   railway run -- bash /abs/path/scripts/fork/start-anvil.sh [BLOCK]
#
#   BLOCK          fork block number (default: upstream latest - 5, recorded in out/fork-block.txt)
#   FORK_PORT      default 8545
#   FORK_OUT_DIR   default scripts/fork/out (log: anvil.log, pid: anvil.pid)
#
# The script daemonizes anvil (nohup) and returns once the fork answers chain id 4663.
# Stop it with: kill "$(cat scripts/fork/out/anvil.pid)"
#
# Do NOT run `ps aux` / `ps -ef` while the fork is up: anvil takes the URL in argv.
# Use `pgrep -x anvil` or `lsof -nP -iTCP:8545 -sTCP:LISTEN` instead.
set -euo pipefail
export FOUNDRY_DISABLE_NIGHTLY_WARNING=1
export PATH="$PATH:/usr/sbin:/sbin:$HOME/.foundry/bin"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${FORK_OUT_DIR:-$HERE/out}"
PORT="${FORK_PORT:-8545}"
ANVIL="${ANVIL:-$(command -v anvil || echo "$HOME/.foundry/bin/anvil")}"
CAST="${CAST:-$(command -v cast || echo "$HOME/.foundry/bin/cast")}"
BLOCK="${1:-${FORK_BLOCK:-}}"
mkdir -p "$OUT"
LOG="$OUT/anvil.log"

redact() { sed -u -E 's#[A-Za-z][A-Za-z0-9+.-]*://[^[:space:]"'"'"']+#<redacted-url>#g'; }

if [ -z "${FORK_UPSTREAM_RPC_URL:-}" ]; then
  echo "start-anvil: FORK_UPSTREAM_RPC_URL is not set. Run this under 'railway run' (see header)." >&2
  exit 2
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "start-anvil: something already listens on 127.0.0.1:$PORT; not starting a second fork." >&2
  exit 1
fi

up_cid="$("$CAST" chain-id --rpc-url "$FORK_UPSTREAM_RPC_URL" 2>&1 | redact)"
if [ "$up_cid" != "4663" ]; then
  echo "start-anvil: upstream answered chain id '$up_cid', want 4663" >&2
  exit 2
fi
if [ -z "$BLOCK" ]; then
  latest="$("$CAST" block-number --rpc-url "$FORK_UPSTREAM_RPC_URL" 2>&1 | redact)"
  BLOCK=$((latest - 5))
fi
ts="$("$CAST" block "$BLOCK" --field timestamp --rpc-url "$FORK_UPSTREAM_RPC_URL" 2>&1 | redact)"
echo "$BLOCK" > "$OUT/fork-block.txt"
echo "start-anvil: forking 4663 at block $BLOCK (timestamp $ts, $(date -u -r "$ts" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || true))"

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

pid=""
for _ in $(seq 1 90); do
  if cid="$("$CAST" chain-id --rpc-url "http://127.0.0.1:$PORT" 2>/dev/null)" && [ "$cid" = "4663" ]; then
    pid="$(lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN | head -1)"
    echo "$pid" > "$OUT/anvil.pid"
    echo "start-anvil: up. pid=$pid rpc=http://127.0.0.1:$PORT log=$LOG block=$("$CAST" block-number --rpc-url "http://127.0.0.1:$PORT")"
    exit 0
  fi
  sleep 1
done
echo "start-anvil: fork did not answer within 90s; see $LOG" >&2
exit 1
