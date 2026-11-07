"""Telegram handlers under Hermes' `multiplex_profiles`: secret reads run inside this profile's scope.

With multiplexing on, `agent.secret_scope.get_secret` raises `UnscopedSecretError` when no scope is
installed, and the plugin's own PTB handlers run outside any Hermes turn. Live symptom (0.7.0 and
0.8.0 before this fix): "secret lookup for SHERWOOD_TG_LINK_SECRET failed: UnscopedSecretError",
then "Links are not configured yet. Ask the host." on the Deposit button.

Hermes is not installed in the test environment, so `agent.secret_scope` and `hermes_constants` are
stand-ins that copy the real semantics the plugin relies on (hermes-agent `agent/secret_scope.py`):
a context-local scope, a context-local multiplex flag, a `get_secret` that reads the scope and raises
with multiplexing on and no scope, and `build_profile_secret_scope(home)` reading `<home>/.env`.
"""
from __future__ import annotations

import sys
import types
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("telegram")

from sherwood_monitor.fund import secrets as fund_secrets  # noqa: E402
from sherwood_monitor.telegram import settings as tg_settings  # noqa: E402
from sherwood_monitor.telegram.handler import FundHandlers, wire  # noqa: E402
from sherwood_monitor.telegram.links import fund_link_message, verify_link_signature  # noqa: E402
from sherwood_monitor.telegram.runtime import TelegramRuntime  # noqa: E402
from sherwood_monitor.telegram.scope import SecretScope, capture_secret_scope  # noqa: E402
from sherwood_monitor.telegram.settings import TelegramSettings  # noqa: E402
from tests.test_tg_handler import make_query  # noqa: E402
from tests.tg_fakes import BOB, VAULT, Clock, FakeGateway, FakeJobQueue, fake_bot  # noqa: E402

LINK_SECRET = "profile-scoped-link-secret"
API_TOKEN = "profile-scoped-api-token"


class UnscopedSecretError(RuntimeError):
    pass


def fake_hermes(monkeypatch, home: Path) -> types.ModuleType:
    """Install stand-ins for `agent.secret_scope` and `hermes_constants` (see the module docstring)."""
    ss = types.ModuleType("agent.secret_scope")
    scope: ContextVar = ContextVar("scope", default=None)
    multiplex: ContextVar = ContextVar("multiplex", default=False)
    ss.UnscopedSecretError = UnscopedSecretError
    ss.built = []

    def get_secret(name, default=None):
        bound = scope.get()
        if bound is not None:
            value = bound[0].get(name)
            return value if value is not None else default
        if multiplex.get():
            raise UnscopedSecretError(name)
        return default  # the test process env holds none of these

    def build_profile_secret_scope(hermes_home):
        ss.built.append(Path(hermes_home))
        out = {}
        for line in (Path(hermes_home) / ".env").read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip():
                out[key.strip()] = value.strip()
        return out

    ss.get_secret = get_secret
    ss.build_profile_secret_scope = build_profile_secret_scope
    ss.set_secret_scope = lambda mapping, *, profile_home=None: scope.set(None if mapping is None else (mapping, profile_home))
    ss.reset_secret_scope = lambda token: scope.reset(token)
    ss.current_secret_scope = lambda: (scope.get() or (None,))[0]
    ss.current_secret_scope_home = lambda: (scope.get() or (None, None))[1]
    ss.set_multiplex_context = lambda active: multiplex.set(bool(active))
    ss.reset_multiplex_context = lambda token: multiplex.reset(token)
    agent = types.ModuleType("agent")
    agent.secret_scope = ss
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", ss)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    # tui_gateway.launch_profile_policy.owner_secret_scope: for the launch home, the process env
    # (minus globals) under the profile's own files; `ss.process_env` stands in for that env.
    ss.process_env = {}
    ss.owned = []

    def owner_secret_scope(h):
        ss.owned.append(Path(h))
        return {**ss.process_env, **build_profile_secret_scope(h)}

    policy = types.ModuleType("tui_gateway.launch_profile_policy")
    policy.owner_secret_scope = owner_secret_scope
    gateway_pkg = types.ModuleType("tui_gateway")
    gateway_pkg.launch_profile_policy = policy
    monkeypatch.setitem(sys.modules, "tui_gateway", gateway_pkg)
    monkeypatch.setitem(sys.modules, "tui_gateway.launch_profile_policy", policy)
    return ss


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    home.mkdir()
    (home / ".env").write_text(f"SHERWOOD_TG_LINK_SECRET={LINK_SECRET}\nSHERWOOD_TG_API_TOKEN={API_TOKEN}\n")
    for key in ("SHERWOOD_TG_LINK_SECRET", "SHERWOOD_TG_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return fake_hermes(monkeypatch, home)


def wired_runtime():
    """A runtime with the plugin's real secret lookup (no test secret injected), wired like Hermes does."""
    rt = TelegramRuntime(FakeGateway(), TelegramSettings(bot_username="sherwoodagentbot"), clock=Clock())
    bot = fake_bot()
    wire(SimpleNamespace(bot=bot, job_queue=FakeJobQueue(), add_handler=lambda handler, group=0: None), rt)
    return rt, bot


async def tap(rt, bot, data, user_id):
    q = make_query(data, user_id)
    await FundHandlers(rt).on_callback(SimpleNamespace(callback_query=q), SimpleNamespace(bot=bot))
    return q


async def test_multiplex_without_a_scope_is_the_bug(hermes):
    """Reproduces the live failure: a bare secret read in a PTB task, multiplexing on, no scope."""
    token = hermes.set_multiplex_context(True)
    try:
        assert tg_settings.link_secret() is None  # "secret lookup ... failed: UnscopedSecretError"
        assert fund_secrets.get_secret("SHERWOOD_TG_API_TOKEN") is None
    finally:
        hermes.reset_multiplex_context(token)


async def test_a_button_builds_a_signed_link_under_multiplexing_with_no_scope_installed(hermes):
    rt, bot = wired_runtime()
    assert isinstance(rt.secret_scope, SecretScope) and len(hermes.owned) == 1  # Hermes' own mapping for this home
    assert Path(rt.secret_scope.profile_home) == hermes.owned[0]
    assert LINK_SECRET not in repr(rt.secret_scope) and API_TOKEN not in repr(rt.secret_scope)  # repr hides the mapping
    token = hermes.set_multiplex_context(True)
    try:
        assert hermes.current_secret_scope() is None  # PTB's task: nothing installed
        q = await tap(rt, bot, f"shfund:deposit:{VAULT}", BOB)
    finally:
        hermes.reset_multiplex_context(token)
    assert "not configured" not in str(q.answer.await_args)
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    url = markup.to_dict()["inline_keyboard"][0][0]["url"]
    q_ = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert urlsplit(url).path == "/tg/deposit" and q_["tg"] == str(BOB)
    message = fund_link_message("deposit", q_["fund"], q_["tg"], int(q_["ts"]))
    assert verify_link_signature(LINK_SECRET, message, q_["sig"], ts=int(q_["ts"]), now=int(q_["ts"]))
    assert LINK_SECRET not in url


async def test_the_installed_scope_is_reset_after_the_handler_returns_or_fails(hermes):
    rt, bot = wired_runtime()
    seen = []

    def deposit_secret():
        seen.append(hermes.current_secret_scope())
        return tg_settings.link_secret()

    rt._secret = deposit_secret
    await tap(rt, bot, f"shfund:deposit:{VAULT}", BOB)
    assert seen and seen[0] is rt.secret_scope.mapping  # installed during the call
    assert hermes.current_secret_scope() is None  # and reset after it

    def boom(chat_id):
        raise RuntimeError("gateway down")

    rt.gateway.fund_for_chat = boom
    await tap(rt, bot, f"shfund:deposit:{VAULT}", BOB)  # _safe logs and swallows it
    assert hermes.current_secret_scope() is None


async def test_capture_prefers_the_scope_hermes_has_bound(hermes):
    mapping = {"SHERWOOD_TG_LINK_SECRET": "bound-by-hermes"}
    token = hermes.set_secret_scope(mapping, profile_home="/profiles/alpha")
    try:
        captured = capture_secret_scope()
    finally:
        hermes.reset_secret_scope(token)
    assert captured.mapping is mapping and captured.profile_home == "/profiles/alpha" and hermes.built == []


async def test_the_app_token_reads_inside_the_scope(hermes):
    scope = capture_secret_scope()
    token = hermes.set_multiplex_context(True)
    try:
        with scope.installed():
            assert fund_secrets.get_secret("SHERWOOD_TG_API_TOKEN") == API_TOKEN
        assert fund_secrets.get_secret("SHERWOOD_TG_API_TOKEN") is None  # outside it, refused (never os.environ)
    finally:
        hermes.reset_multiplex_context(token)


async def test_a_secret_only_in_the_process_env_is_kept(hermes, monkeypatch):
    """launchd / systemd / `op run`: the link secret is in the launch env, not the profile's .env."""
    (sys.modules["hermes_constants"].get_hermes_home() / ".env").write_text("")  # nothing in the profile's file
    hermes.process_env = {"SHERWOOD_TG_LINK_SECRET": "from-the-launch-env"}
    scope = capture_secret_scope()
    assert scope.mapping["SHERWOOD_TG_LINK_SECRET"] == "from-the-launch-env"
    monkeypatch.setitem(sys.modules, "tui_gateway.launch_profile_policy", None)  # older Hermes: no owner_secret_scope
    assert "SHERWOOD_TG_LINK_SECRET" not in capture_secret_scope().mapping  # files only


def test_the_repr_never_shows_a_secret():
    assert "s3cret" not in repr(SecretScope({"SHERWOOD_TG_LINK_SECRET": "s3cret"}, "/profiles/alpha"))


def test_outside_hermes_there_is_nothing_to_capture(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # import fails, as outside Hermes
    assert capture_secret_scope() is None
    with SecretScope({"X": "y"}).installed():  # a no-op without Hermes
        pass
