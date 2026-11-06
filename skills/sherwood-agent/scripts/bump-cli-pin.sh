#!/usr/bin/env bash
# Move every `@sherwoodagent/cli@X.Y.Z` pin in the skill to a new CLI release.
#
#   scripts/bump-cli-pin.sh 0.101.0
#
# Then bump the skill version (.claude-plugin/plugin.json and SKILL.md
# metadata.version), open a PR and merge it: the release workflow tags the
# merge and publishes SKILL.md with its sha256 (see CLAUDE.md → Releases).
set -euo pipefail
new=${1:?usage: scripts/bump-cli-pin.sh <cli version, e.g. 0.101.0>}
[[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "not a release version: $new" >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"

published=$(npm view "@sherwoodagent/cli@$new" version 2>/dev/null || true)
[ "$published" = "$new" ] || { echo "@sherwoodagent/cli@$new is not published on npm" >&2; exit 1; }

files=()
while IFS= read -r x; do files+=("$x"); done < <(git grep -l -E '@sherwoodagent/cli@[0-9]' -- '*.md' ':!:CLAUDE.md')
[ "${#files[@]}" -gt 0 ] || { echo "no @sherwoodagent/cli pins found" >&2; exit 1; }
perl -pi -e "s{\@sherwoodagent/cli\@[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*}{\@sherwoodagent/cli\@$new}g" "${files[@]}"
git --no-pager diff --stat
CHECK_NPM=0 scripts/check-skill.sh >/dev/null && echo "Pinned @sherwoodagent/cli@$new. Next: bump the skill version, PR, merge."
