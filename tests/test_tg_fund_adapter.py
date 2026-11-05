"""fund/telegram.py: record -> view mapping, CallbackFundGateway, Hermes registration."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sherwood_monitor.fund.telegram import (
    CallbackFundGateway,
    fund_to_view,
    proposal_to_view,
    register_telegram,
)
from sherwood_monitor.telegram.settings import TelegramSettings, get_secret
from sherwood_monitor.telegram.types import DraftRequested, FundActionError, FundGateway

VAULT = "0x1111111111111111111111111111111111111111"
GOV = "0x2222222222222222222222222222222222222222"


def record(**kw):
    base = dict(
        id=42, governor=GOV, proposer="atlas", thesis="Long NVDA",
        legs=[{"symbol": "NVDA", "weight_bps": 6000}, {"symbol": "AMD", "weight_bps": 4000}],
        max_capital=3000, max_drawdown_bps=1500, bond_wood=12.4, status="voting",
        backers=[11, 12, 11], objectors=[13], idea_by=10, card_message_id=None,
        voting_ends_at=1_700_001_800, against_bps=500, threshold_bps=2000,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def fund_record(**kw):
    base = dict(
        vault=VAULT, governor=GOV, name="Robin Fund", telegram_chat_id=-100, host_telegram_id=1,
        members=[{"tg_id": 10, "username": "alice", "share_bps": 5000}, 11], agents=["atlas", {"name": "nova"}],
        nav=10000, pnl_bps=820, positions=[{"symbol": "NVDA", "value": 4000, "pnl_bps": 310}],
        open_proposals=[record()],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_proposal_to_view_from_object_and_dict():
    v = proposal_to_view(record())
    assert v.id == "42" and v.backers == (11, 12) and v.objectors == (13,)
    assert [(leg.symbol, leg.weight_bps) for leg in v.legs] == [("NVDA", 6000), ("AMD", 4000)]
    d = proposal_to_view({"id": "7", "status": "draft", "proposer_agent": "nova", "thesis": "x"})
    assert d.proposer == "nova" and d.threshold_bps == 2000 and d.legs == ()


def test_fund_to_view():
    f = fund_to_view(fund_record())
    assert f.host_tg_id == 1 and f.telegram_chat_id == -100
    assert f.agents == ("atlas", "nova")
    assert [(m.tg_id, m.number, m.username) for m in f.members] == [(10, 1, "alice"), (11, 2, None)]
    assert f.open_proposals[0].id == "42" and f.positions[0].pnl_bps == 310
    assert fund_to_view(fund_record(open_proposals=["42"])).open_proposals == ()


async def test_callback_gateway_maps_records_and_actions():
    fr = fund_record()
    seen = {}

    async def get_proposal(fund, pid):
        return record(id=pid) if pid == "42" else None

    def toggle(fund, pid, tg):
        seen["toggle"] = (pid, tg)
        return record(backers=[tg])

    gw = CallbackFundGateway(
        get_fund_for_chat=lambda chat: fr if chat == -100 else None,
        get_proposal=get_proposal,
        toggle_backer_fn=toggle,
        ship_draft_fn=lambda fund, draft, by: f"shipped {draft}",
        leaderboard_fn=lambda fund: [{"tg_id": 10, "pnl_bps": 1240, "ideas": 3}],
        people_fn=lambda fund: {"12": "carol", 13: {"first_name": "Dave", "last_name": "Doe"}},
        on_draft_requested_fn=lambda ev: seen.setdefault("draft", ev),
        wood_usd_fn=lambda: "0.25",
    )
    assert isinstance(gw, FundGateway)
    assert (await gw.fund_for_chat(-100)).name == "Robin Fund"
    assert await gw.fund_for_chat(-1) is None
    assert (await gw.proposal(-100, "42")).id == "42"
    assert await gw.proposal(-100, "nope") is None
    assert (await gw.toggle_backer(-100, "42", 11)).backers == (11,) and seen["toggle"] == ("42", 11)
    assert await gw.ship_draft(-100, "d7", 1) == "shipped d7"
    with pytest.raises(FundActionError):
        await gw.approve_member(-100, 5, 1)  # not wired
    with pytest.raises(FundActionError):
        await gw.ship_draft(-1, "d7", 1)  # no fund
    rows = await gw.leaderboard(-100)
    assert rows[0].tg_id == 10 and rows[0].pnl_bps == 1240
    people = await gw.people(-100)
    assert people[10].handle == "@alice" and people[12].handle == "@carol" and people[13].handle == "DD"
    assert await gw.wood_usd() == 0.25
    assert await gw.host_signing_url(-100, "d7", 1) is None
    ev = DraftRequested(idea_by=10, text="long", source_message_id=100, chat_id=-100)
    await gw.on_draft_requested(ev)
    assert seen["draft"] is ev


def test_register_telegram_registers_factory_and_hook():
    pytest.importorskip("telegram")
    from telegram.ext import ApplicationBuilder

    ctx = MagicMock()
    gw = CallbackFundGateway(get_fund_for_chat=lambda chat: None)
    runtime = register_telegram(ctx, gw, settings=TelegramSettings())
    platform, factory = ctx.register_platform_handler.call_args.args
    assert platform == "telegram"
    assert factory.__qualname__.endswith("_wire_sherwood_fund_telegram")
    hook_name, hook = ctx.register_hook.call_args.args
    assert hook_name == "pre_llm_call" and hook(platform="telegram", user_message="hi") is None
    assert not runtime.connected
    app = ApplicationBuilder().token("123:TEST").build()
    factory(app, MagicMock())
    assert runtime.connected and sum(len(h) for h in app.handlers.values()) == 8
    factory(None, MagicMock())  # other platforms hand None: no crash


def test_register_telegram_without_platform_handlers():
    ctx = SimpleNamespace(register_hook=MagicMock())
    assert register_telegram(ctx, CallbackFundGateway(get_fund_for_chat=lambda c: None)) is None


def test_fund_package_imports_without_ptb(monkeypatch):
    """register() must work when python-telegram-bot is absent."""
    import importlib

    for name in list(sys.modules):
        if name == "telegram" or name.startswith("telegram.") or name.startswith("sherwood_monitor.telegram") \
                or name == "sherwood_monitor.fund.telegram":
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "telegram", None)  # makes `import telegram` fail
    mod = importlib.import_module("sherwood_monitor.fund.telegram")
    ctx = MagicMock()
    runtime = mod.register_telegram(ctx, mod.CallbackFundGateway(get_fund_for_chat=lambda c: None),
                                    settings=TelegramSettings())
    assert runtime is not None and ctx.register_platform_handler.called


def test_settings_from_env_and_secret_lookup(monkeypatch):
    env = {
        "APP_BASE_URL": "https://staging.sherwood.sh/",
        "TELEGRAM_GROUP_ALLOWED_CHATS": "-1001, -1002, junk",
        "SHERWOOD_TG_PERSONAL_LINKS": "dm",
        "TELEGRAM_BOT_USERNAME": "@sherwoodagentbot",
    }
    s = TelegramSettings.from_env(env)
    assert s.app_base_url == "https://staging.sherwood.sh"
    assert s.allowed_chat_ids == frozenset({-1001, -1002})
    assert s.personal_link_mode == "dm" and s.bot_username == "sherwoodagentbot"
    assert s.chat_allowed(-1001) and not s.chat_allowed(-3)
    assert TelegramSettings.from_env({}).app_base_url == "https://app.sherwood.sh"
    assert TelegramSettings().chat_allowed(-3)

    # No Hermes: env fallback
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setenv("SHERWOOD_TG_LINK_SECRET", "from-env")
    assert get_secret("SHERWOOD_TG_LINK_SECRET") == "from-env"

    # Hermes present: profile-scoped store wins, env is not consulted
    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default="": {"SHERWOOD_TG_LINK_SECRET": "from-hermes"}.get(name, default)
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    assert get_secret("SHERWOOD_TG_LINK_SECRET") == "from-hermes"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    assert get_secret("TELEGRAM_BOT_TOKEN") is None
