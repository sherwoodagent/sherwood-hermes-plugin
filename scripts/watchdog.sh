#!/usr/bin/env bash
# Generic wrapper: `scripts/watchdog.sh <subcommand>` invokes the
# corresponding `sherwood_monitor.cli_watchdog` subcommand. Hermes
# no_agent crons (`hermes cron create --no-agent --script ... <sub>`)
# call this with one of digest|aum|gas|stream.
#
# Output contract (preserved verbatim from cli_watchdog.main):
#   stdout non-empty  → Hermes delivers as alert
#   stdout empty      → Hermes treats as silent tick
#   exit non-zero     → Hermes delivers as error alert
#
# The wrapper adds NO logic — it only picks a Python interpreter. The
# entire cron contract lives in the Python module and is exercised by
# tests/test_cli_watchdog.py.
set -euo pipefail

SUB="${1:-}"
if [[ -z "${SUB}" ]]; then
  echo "usage: $0 <digest|aum|gas|stream>" >&2
  exit 64
fi

# Interpreter resolution (first hit wins):
#   1. SHERWOOD_MONITOR_PYTHON — explicit override (used by tests).
#   2. The `sherwood-monitor-watchdog` console script if it's on PATH
#      (`pip install` of this package creates one — picks up the same
#      Python interpreter the plugin was installed into, even if PATH
#      points at a different `python3`).
#   3. $PYTHON environment variable.
#   4. python3 on PATH.
export PYTHONUNBUFFERED=1

if [[ -n "${SHERWOOD_MONITOR_PYTHON:-}" ]]; then
  exec "${SHERWOOD_MONITOR_PYTHON}" -m sherwood_monitor.cli_watchdog "${SUB}"
fi

if command -v sherwood-monitor-watchdog >/dev/null 2>&1; then
  exec sherwood-monitor-watchdog "${SUB}"
fi

PY="${PYTHON:-python3}"
exec "${PY}" -m sherwood_monitor.cli_watchdog "${SUB}"
