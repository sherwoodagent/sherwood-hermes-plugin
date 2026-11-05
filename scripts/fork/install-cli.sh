#!/usr/bin/env bash
# FORK ONLY. Install the pinned Sherwood CLI 0.102.0 into scripts/fork/.cli (gitignored).
#
#   scripts/fork/install-cli.sh
#
# What is pinned:
#   - The CLI tarball's sha512 (CLI_SHA512 below, recorded once on 2026-11-05). The tarball is
#     fetched from registry.npmjs.org and must match this constant; the registry's own
#     dist.integrity is not trusted on its own.
#   - Every dependency, through the committed lockfile cli-pin/package-lock.json (700
#     packages, resolved + integrity). Installed with `npm ci`, which fails on any mismatch.
#   - `--ignore-scripts`: no lifecycle scripts run. Skipped on purpose:
#       * the CLI's postinstall (scripts/pin-xmtp-bindings.cjs): the fork tooling never starts
#         an XMTP client (sw.sh refuses the commands that would);
#       * @ipshipyard/node-datachannel's prebuild-install, which downloads an unhashed native
#         binary. Nothing the fork scripts run loads it.
#
# Why not `npm i @sherwoodagent/cli@0.102.0`: this machine's npm config sets min-release-age=7
# (a supply-chain gate), and 0.102.0 was published 2026-11-03. The lockfile's dependencies
# were resolved under that gate; only the first-party CLI tarball bypasses it, pinned by hash.
#
# A different version is not supported here: it needs its own hash constant and lockfile.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VER=0.102.0
CLI_SHA512="sha512-11WiN+qh3Kbrr0TYBJua98NOL3BPKi+oT76NJc/IG09Mqw7yzH+DtkJMaDl25dX8FPRFyo7rX5Z4jTbosIKCqg=="
URL="https://registry.npmjs.org/@sherwoodagent/cli/-/cli-$VER.tgz"
DEST="$HERE/.cli"
BIN="$DEST/node_modules/.bin/sherwood"
TGZ="$DEST/tarball/cli-$VER.tgz"

sha() { echo "sha512-$(openssl dgst -sha512 -binary "$1" | openssl base64 -A)"; }

mkdir -p "$DEST/tarball"
if [ ! -f "$TGZ" ] || [ "$(sha "$TGZ")" != "$CLI_SHA512" ]; then
  curl -fsSL -o "$TGZ.part" "$URL"
  got="$(sha "$TGZ.part")"
  if [ "$got" != "$CLI_SHA512" ]; then
    rm -f "$TGZ.part"
    echo "install-cli: tarball sha512 $got does not match the pinned $CLI_SHA512; refusing" >&2
    exit 1
  fi
  mv "$TGZ.part" "$TGZ"
fi

cp "$HERE/cli-pin/package.json" "$DEST/package.json"
cp "$HERE/cli-pin/package-lock.json" "$DEST/package-lock.json"
npm ci --prefix "$DEST" --ignore-scripts --no-audit --no-fund
echo "install-cli: installed sherwood $("$BIN" --version) at $BIN (lockfile + pinned tarball, scripts ignored)"
