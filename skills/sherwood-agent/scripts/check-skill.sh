#!/usr/bin/env bash
# shellcheck disable=SC2016  # backticks in single-quoted patterns are literal on purpose
# Release invariants for the skill pack. CI runs it on every PR and before
# every tag (.github/workflows/ci.yml, release.yml); run it locally too.
#
#   1. .claude-plugin/plugin.json `version` == SKILL.md `metadata.version`.
#   2. No `@latest` in any file an agent reads (everything but CLAUDE.md).
#   3. Every `@sherwoodagent/cli@X.Y.Z` pin is the same version, and (unless
#      CHECK_NPM=0) that version is published on npm.
#   4. Install commands in fenced code blocks name an exact version:
#      `npm i -g` / `npm install -g`, `--package=` and `pip install`.
#   5. No blanket pre-approval in any `allowed-tools`: Bash(<tool>:*) or
#      Bash(<tool> *) for a tool that can fetch, read arbitrary files,
#      install, run code or sign (curl, cat, npm, npx, pnpm, pip, forge, git,
#      mm, cast, jq, sh, bash, node, python). `sherwood *` and read-only
#      `cast call *`-style subcommands are fine.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

status=0
err() { echo "::error file=${2:-}::$1"; status=1; }

frontmatter() { awk 'NR==1 && /^---$/ {f=1; next} f && /^---$/ {exit} f' "$1"; }

# Files an agent reads: every tracked Markdown file except the repo guide.
content=()
while IFS= read -r x; do content+=("$x"); done < <(git ls-files '*.md' ':!:CLAUDE.md')

# 1. versions agree
plugin_version=$(jq -r .version .claude-plugin/plugin.json)
skill_version=$(frontmatter SKILL.md | sed -n "s/^  version: *['\"]\{0,1\}\([^'\"]*\)['\"]\{0,1\} *$/\1/p")
[ "$plugin_version" = "$skill_version" ] \
  || err "plugin.json version $plugin_version != SKILL.md metadata.version $skill_version" SKILL.md
echo "skill version: $plugin_version"

# 2. no @latest
if hits=$(grep -n '@latest' "${content[@]}"); then
  while IFS= read -r h; do err "@latest is not allowed; pin an exact version: $h" "${h%%:*}"; done <<<"$hits"
fi

# 3. one CLI pin, published
pins=()
while IFS= read -r x; do pins+=("$x"); done < <(grep -h -o -E '@sherwoodagent/cli@[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*' "${content[@]}" | sort -u)
if [ "${#pins[@]}" -ne 1 ]; then
  err "expected exactly one @sherwoodagent/cli pin, found ${#pins[@]}: ${pins[*]:-none} (scripts/bump-cli-pin.sh sets them all)" SKILL.md
else
  cli_version=${pins[0]##*@}
  echo "CLI pin: $cli_version"
  if [ "${CHECK_NPM:-1}" != 0 ]; then
    published=$(npm view "@sherwoodagent/cli@$cli_version" version 2>/dev/null || true)
    [ "$published" = "$cli_version" ] || err "@sherwoodagent/cli@$cli_version is not published on npm" SKILL.md
  fi
fi

# 4. install commands in fenced code blocks carry exact versions
codelines() { awk '/^[[:space:]]*```/ {inb = !inb; next} inb {print FILENAME ":" FNR ": " $0}' "$@"; }
code=$(codelines "${content[@]}")
while IFS= read -r hit; do
  [ -n "$hit" ] || continue
  loc=${hit%%|*}; pkg=${hit##*|}
  [[ "$pkg" =~ ^(@[^/@]+/)?[^@]+@[0-9]+\.[0-9]+\.[0-9]+ ]] || err "unpinned install: $pkg ($loc)" "${loc%%:*}"
done < <(
  while IFS= read -r line; do
    loc=$(cut -d: -f1,2 <<<"$line")
    grep -o -E 'npm (i|install) +-g +[^ `)"]+|--package=[^ `)"]+' <<<"$line" \
      | sed -E 's/^npm (i|install) +-g +//; s/^--package=//' | sed "s|^|$loc\||" || true
  done <<<"$code"
)
while IFS= read -r hit; do
  [ -n "$hit" ] || continue
  loc=${hit%%|*}; pkg=${hit##*|}
  [[ "$pkg" == *==* ]] || err "unpinned pip install: $pkg ($loc)" "${loc%%:*}"
done < <(
  while IFS= read -r line; do
    loc=$(cut -d: -f1,2 <<<"$line")
    grep -o -E 'pip3? install +[^ `)"#]+' <<<"$line" | sed -E 's/^pip3? install +//' | sed "s|^|$loc\||" || true
  done <<<"$code"
)

# 5. no blanket allowed-tools
blanket='Bash\((curl|wget|cat|npm|npx|pnpm|yarn|bun|pip|pip3|uv|forge|git|mm|cast|jq|sh|bash|zsh|node|python|python3)(:\*| \*)\)|Bash\(\*\)|(^| )Bash( |$)'
while IFS= read -r f; do
  tools=$(frontmatter "$f" | sed -n 's/^allowed-tools: *//p')
  if bad=$(grep -o -E "$blanket" <<<"$tools"); then
    err "blanket allowed-tools in $f: $(tr '\n' ' ' <<<"$bad")" "$f"
  fi
done < <(git ls-files 'SKILL.md' '*/SKILL.md')

[ "$status" -eq 0 ] && echo "skill checks passed"
exit "$status"
