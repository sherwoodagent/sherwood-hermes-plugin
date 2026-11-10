# Submitting sherwood-monitor to the Hermes plugin catalog

The entry to submit is [`catalog/sherwood-monitor.yaml`](../catalog/sherwood-monitor.yaml).
It goes to **NousResearch/hermes-agent** as `plugin-catalog/sherwood-monitor.yaml`,
one file per PR. The catalog's rules and schema are in that repo's
`plugin-catalog/README.md`; the submission guide is
`website/docs/developer-guide/plugins/catalog-submission.md`.

Opening the catalog PR is part of the hackathon entry: it is held for the
maintainer's go-ahead, and the repo owner (or a major contributor) must be the
one who submits it (catalog rule 5).

## 1. Wait for the v0.9.0 tag

Merging the release PR into `main` runs `.github/workflows/release.yml`. When
`pyproject.toml` carries a version with no matching tag, it creates the
`v0.9.0` tag on exactly the commit that triggered the run (the merge commit:
`target_commitish: ${{ github.sha }}`) and a GitHub release. Nobody tags by
hand.

```bash
gh release view v0.9.0 -R sherwoodagent/sherwood-hermes-plugin
```

## 2. Resolve the commit to pin

The catalog pins a full 40-character commit, never a tag name.

```bash
SHA=$(gh api repos/sherwoodagent/sherwood-hermes-plugin/commits/v0.9.0 --jq .sha)
echo "$SHA"    # 40 lowercase hex chars
```

From a local clone, `git fetch --tags origin && git rev-parse 'v0.9.0^{commit}'`
gives the same value (`^{commit}` matters if the tag is ever annotated: a bare
`rev-parse` would return the tag object).

### Publish the install command with the sha

Hermes has no `@tag` install syntax: `hermes plugins install owner/repo@v0.9.0`
fails (Hermes turns it into a repository URL), and `--ref` accepts only a full
40-character commit sha. The README and the bundled skill
at the pinned commit therefore say
`hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref <40-hex sha of the v0.9.0 release commit>`;
that placeholder stays in the tree by design, because a commit cannot contain
its own sha. Fill it in where it can live:

```bash
gh release edit v0.9.0 -R sherwoodagent/sherwood-hermes-plugin --notes "$(cat <<EOF
Install: hermes plugins install sherwoodagent/sherwood-hermes-plugin --ref $SHA
(or, once listed in the Hermes catalog: hermes plugins install sherwood-monitor)
EOF
)"
```

and in the consumer-repo PRs (`docs/consumer-updates.md`), which pin `$SHA`
in place of the placeholder. Editing the release notes is part of the
release, held for the maintainer's go-ahead like the catalog PR.

## 3. Check the plugin at that commit

```bash
git clone https://github.com/sherwoodagent/sherwood-hermes-plugin /tmp/shm-pin
git -C /tmp/shm-pin checkout --detach "$SHA"
hermes plugins validate /tmp/shm-pin
hermes plugins doctor /tmp/shm-pin --ci
```

Catalog CI runs `hermes plugins validate --install-deps` on a fresh clone at the
pin. `--install-deps` installs the plugin's Python dependencies into the Hermes
environment, so only run it locally on a throwaway Hermes install.

## 4. Fill in the entry

The copy of `catalog/sherwood-monitor.yaml` at the pinned commit has the
placeholder, because a commit cannot carry its own sha. (After the tag, a
follow-up PR fills the sha in on `main`, as #39 did for v0.8.4.) Submit the
pinned copy, filled in:

```bash
sed -e "s/^sha: \"<sha of the v0.9.0 tag commit>\"/sha: \"$SHA\"/" \
    -e '/^# REPLACE before submitting/,/^# catalog CI reject anything else/d' \
    /tmp/shm-pin/catalog/sherwood-monitor.yaml > /tmp/sherwood-monitor.yaml
grep -n '^sha:' /tmp/sherwood-monitor.yaml   # must show the 40-hex commit
```

Check, against `plugin.yaml` at the pin:

- `version` is `"0.9.0"` and `requires_hermes` matches `plugin.yaml`.
- `capabilities` lists the same 17 tools, 5 hooks and 5 `requires_env` names
  (`tests/test_catalog_entry.py` enforces this in this repo).
- `tier: community`. Only Nous maintainers set `official` or `verified`.

## 5. Open the PR

```bash
gh repo fork NousResearch/hermes-agent --clone --remote
cd hermes-agent
git checkout -b plugin-catalog/sherwood-monitor
cp /tmp/sherwood-monitor.yaml plugin-catalog/sherwood-monitor.yaml
pip install ruamel.yaml==0.18.17
python3 scripts/validate_plugin_catalog.py plugin-catalog/sherwood-monitor.yaml
git add plugin-catalog/sherwood-monitor.yaml
git commit -S -m "plugin-catalog: add sherwood-monitor 0.9.0"
git log --format='%h %G?' -1      # must print G
git push -u origin plugin-catalog/sherwood-monitor
gh pr create -R NousResearch/hermes-agent \
  --title "plugin-catalog: add sherwood-monitor 0.9.0" \
  --body-file /tmp/sherwood-monitor-pr.md
```

The PR body (`/tmp/sherwood-monitor-pr.md`) says what the plugin does, which
Hermes surfaces it uses, and everything rule 13 asks to disclose. Use:

- **What it does:** the catalog `description` first sentence, plus a link to
  the README at the pinned commit.
- **Surfaces:** `register_tool` (17 tools in toolsets `sherwood-monitor` and
  `sherwood-fund`), `register_hook` (`on_session_start`, `on_session_finalize`,
  `pre_tool_call`, `post_tool_call`, `pre_llm_call`), `register_command`
  (`/fund`, `/join-fund`, `/fund-approve`, `/fund-agents`, `/leaderboard`),
  `register_skill` (4 skills), `register_cli_command` (`hermes sherwood`),
  `register_platform_handler("telegram", ...)`.
- **Disclosure:** the README section "What the plugin touches (disclosure)" and
  [docs/hackathon-disclosure.md](hackathon-disclosure.md), linked at the pinned
  commit (`https://github.com/sherwoodagent/sherwood-hermes-plugin/blob/<SHA>/docs/hackathon-disclosure.md`).
- The validator output from step 3.

Then wait for the catalog CI job (`plugin-catalog-ci.yml`) to go green.

## Expect in review

- **Cron wrappers.** `hermes sherwood install-cron` writes four small
  `.py` wrapper scripts to `$HERMES_HOME/scripts/` (Hermes runs cron scripts only
  from there) and registers them with `hermes cron create`; it is disclosed.
  Its test runs the real `hermes` binary in a scratch `HERMES_HOME`.
- **The rule-9 warning (private core import).** `hermes plugins validate`
  passes, with one warning: `private core imports (catalog rule 9 …):
  sherwood_monitor/telegram/scope.py:89
  tui_gateway.launch_profile_policy.owner_secret_scope`. The reason:
  - The Telegram fund handlers run on Hermes' Telegram adapter loop, outside
    any agent turn. So when the adapter connects, the plugin captures the
    profile's secret mapping and installs it around each handler call. That
    way `get_secret` reads this profile's secrets when `multiplex_profiles`
    is on.
  - Inside a turn the plugin uses the documented
    `agent.secret_scope.current_secret_scope()`.
  - Outside one it imports `owner_secret_scope(home)`, the mapping Hermes'
    own gateway, cron and delegate code bind for a profile. For the launch
    profile (the process's own home) that is the process env plus `.env`, so
    a secret injected by launchd, systemd or `op run` is kept. For another
    profile's home it is built from that profile's files, as
    `build_profile_secret_scope` does.
  - Hermes has no public equivalent today. The import is guarded: without it
    the plugin falls back to `agent.secret_scope.build_profile_secret_scope(home)`.
  - Nothing is rebound or patched; validate's `no core override` check
    passes.
  If the reviewers want the import gone, the plugin can use the fallback
  alone or ask for a public seam.
- **What `show` and `validate` say about isolation.** `hermes plugins show
  sherwood-monitor` prints `Runs in: Hermes process — runs in the plugin host
  (plugins.isolation: host)`, and validate's isolation report says `verdict:
  host`. The entry's `known_issues` say to keep `plugins.isolation:
  in_process`. These do not contradict each other:
  - **The line has two parts.** The first part is where the plugin runs now:
    `in_process` is Hermes' default. The dimmed part is the static audit
    (`hermes_cli/plugin_isolation_audit.py`).
  - **The audit cannot see the Telegram handler.** It reads only direct
    `<obj>.register_*(...)` calls. The plugin looks up
    `register_platform_handler` with `getattr(ctx,
    "register_platform_handler", None)`, so that it still loads on a Hermes
    without the method, and the audit does not see it. Hermes lists that
    method as unsupported in the plugin host ("the handler factory receives
    the platform's native SDK client"). Validate's own note says
    `register_cli_command` is skipped there.
  - **Under `isolation: host`** the Telegram handler and `hermes sherwood`
    are missing, as `known_issues` says.
  - **`Listens: (none)`** is the manifest's `listens` list of Hermes events,
    and the plugin subscribes to none. The `subscribe` in the audit's
    `ctx_methods` is the plugin's own `FundService.subscribe`, not a Hermes
    subscription.

- **Rule 11 (credentials come through Hermes): resolved in 0.7.0.** Up to
  0.6.0 the plugin read the Sherwood CLI's `~/.sherwood/config.json`
  (including its `privateKey`). 0.7.0 deletes those reads: the monitor wallet comes
  only from the declared `requires_env` entries `SHERWOOD_PRIVATE_KEY`
  (secret, optional) and `SHERWOOD_AGENT_ADDRESS` (optional), read with
  `agent.secret_scope.get_secret`; the gas watchdog's RPC is the plugin's own
  `gas_rpc_url` config key. Preflight no longer checks for the CLI's config
  file either. `tests/test_register.py` and `tests/test_cli_watchdog.py`
  assert the file is never opened, even when it exists. If a reviewer asks,
  say so and point at those tests.
- **Declared env.** `requires_env` (prompted at install, all optional except
  for the Telegram features): `TELEGRAM_BOT_TOKEN`,
  `SHERWOOD_TG_LINK_SECRET`, `SHERWOOD_TG_API_TOKEN`,
  `SHERWOOD_PRIVATE_KEY`, `SHERWOOD_AGENT_ADDRESS`. `TX_BUILDER_URL` and
  `APP_BASE_URL` are under `optional_env` (URL overrides with defaults in
  `config_schema`, not prompted). The catalog schema has no optional-env
  list, so the entry's `capabilities.requires_env` mirrors `requires_env`
  only.
- **Rule 17 (no unscannable payloads).** Nothing binary is committed, and
  the install builds nothing: the plugin is pure Python and downloads only
  its declared Python dependencies.
- **Uninstall deletes state on a default install.** The plugin keeps its
  config and state under `~/.hermes/plugins/sherwood-monitor/` in the user's
  home (`_plugin_root()` in `sherwood_monitor/__init__.py`; `funds/` from
  `fund/store.py`). With the default `HERMES_HOME` that is the directory
  Hermes clones the plugin into, so `hermes plugins remove sherwood-monitor`
  deletes `config.yaml` and `funds/` with it; the README ("Upgrade and
  uninstall") and the entry's `known_issues` say to back them up first. An
  in-place `hermes plugins install ... --force --ref <sha>` keeps them
  (Hermes keeps untracked files).
- **Rule 12 (approvals).** The fund guard returns Hermes' approval shape for
  host actions with a per-action `rule_key`; it never auto-approves.

## Later releases

A new pin is a new PR (rule 4): bump `sha` and `version` together in
`plugin-catalog/sherwood-monitor.yaml`, after the new tag exists, and say in
the PR which commit range it adopts.
