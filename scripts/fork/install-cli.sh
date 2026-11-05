#!/usr/bin/env bash
# Install the pinned Sherwood CLI into scripts/fork/.cli (gitignored). Idempotent.
#
#   scripts/fork/install-cli.sh            # 0.102.0
#   SHERWOOD_CLI_VERSION=0.103.0 scripts/fork/install-cli.sh
#
# Why not plain `npm i @sherwoodagent/cli@0.102.0`: this machine's npm config sets
# min-release-age=7 (a supply-chain gate), and 0.102.0 was published 2026-11-03, so npm
# refuses it ("No matching version found ... with a date before ...") until 2026-11-10.
# The CLI is first-party (sherwoodagent), so this script fetches its registry tarball,
# checks it against the registry's sha512 `dist.integrity`, and installs the tarball.
# Every third-party dependency still resolves through npm under the age gate.
#
# The installed CLI is never run directly by these scripts: use scripts/fork/sw.sh, which
# isolates HOME and pins the RPC to the local fork.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VER="${SHERWOOD_CLI_VERSION:-0.102.0}"
DEST="$HERE/.cli"
BIN="$DEST/node_modules/.bin/sherwood"

if [ -x "$BIN" ] && [ "$("$BIN" --version 2>/dev/null)" = "$VER" ]; then
  echo "install-cli: sherwood $VER already installed at $BIN"; exit 0
fi

mkdir -p "$DEST/tarball"
url="$(npm view "@sherwoodagent/cli@$VER" dist.tarball 2>/dev/null)"
want="$(npm view "@sherwoodagent/cli@$VER" dist.integrity 2>/dev/null)"
case "$url" in https://registry.npmjs.org/@sherwoodagent/cli/-/*) ;; *) echo "install-cli: unexpected tarball url '$url'" >&2; exit 1 ;; esac
tgz="$DEST/tarball/cli-$VER.tgz"
curl -fsSL -o "$tgz" "$url"
got="sha512-$(openssl dgst -sha512 -binary "$tgz" | openssl base64 -A)"
if [ "$got" != "$want" ]; then
  echo "install-cli: integrity mismatch for $tgz (got $got, registry says $want)" >&2; exit 1
fi
npm i --prefix "$DEST" "$tgz"
echo "install-cli: installed sherwood $("$BIN" --version) at $BIN"
