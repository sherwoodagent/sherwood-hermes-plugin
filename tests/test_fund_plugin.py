"""The plugin as Hermes sees it: registrations, manifest, commands, and the draft guard."""
from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from sherwood_monitor.fund.settings import FundSettings, load_fund_settings
from tests.fake_ctx import FakeCtx
from tests.fund_helpers import GOVERNOR, GUEST_ADDR, HOST_ADDR, MEMBER_ADDR, TARGET, VAULT

ROOT = Path(__file__).resolve().parent.parent
MONITOR_TOOLS = {
    "sherwood_monitor_start", "sherwood_monitor_stop", "sherwood_monitor_status",
    "sherwood_monitor_exposure", "sherwood_monitor_cron_tick",
}
FUND_TOOLS = {
    "fund_status", "fund_setup", "fund_claim_topic", "fund_release_claim", "fund_draft_proposal",
    "fund_set_proposal_status", "fund_record_vote", "fund_record_settle", "fund_approve_member",
    "fund_approve_agent", "fund_remove_agent",
}


def boot(tmp_path: Path, monkeypatch, fund_cfg: dict | None = None) -> FakeCtx:
    monkeypatch.delenv("SHERWOOD_FUND_STATE_DIR", raising=False)
    monkeypatch.delenv("TX_BUILDER_URL", raising=False)
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    cfg_dir = tmp_path / ".hermes" / "plugins" / "sherwood-monitor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"funds": [], "fund": fund_cfg or {}}))
    ctx = FakeCtx()
    with patch("pathlib.Path.home", return_value=tmp_path), \
         patch("sherwood_monitor.preflight.subprocess.run", MagicMock(side_effect=FileNotFoundError)):
        from sherwood_monitor import register
        register(ctx)
    return ctx


HOST_CFG = {
    "agent_name": "alpha", "agent_identity_id": "101", "agent_address": HOST_ADDR,
    "default_fund": "alpha-fund", "app_base_url": "https://app.example",
}


@pytest.fixture
def host(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, HOST_CFG)
    out = ctx.call_tool("fund_setup", {
        "fund": "alpha-fund", "vault": VAULT, "governor": GOVERNOR, "telegram_chat_id": "-100500",
        "host_telegram_id": "900", "max_capital": "1000", "max_drawdown_bps": 2000, "voting_period": 1800,
    })
    assert out["ok"] and out["created"], out
    return ctx


def draft_args(**kw):
    args = {
        "topic": "TSLA momentum", "thesis": "TSLA runs into deliveries",
        "legs": [{"summary": "buy TSLA", "target": TARGET, "data": "0xabcd", "cap": "400"},
                 {"summary": "sell TSLA", "phase": "settle", "target": TARGET, "data": "0xdcba"}],
        "max_capital": "500", "max_drawdown_bps": 1000, "strategy_duration_seconds": 3600,
        "bond_quote_wood": "1000", "wood_balance": "2000", "idea_by": "77",
        "strategy": TARGET, "metadata_uri": "ipfs://bafyproposal",
    }
    args.update(kw)
    return args


# -- registration ----------------------------------------------------------------------
def test_registers_against_the_real_hermes_signatures(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch)
    assert set(ctx.tools) == MONITOR_TOOLS | FUND_TOOLS
    assert all(ctx.tools[t]["is_async"] for t in MONITOR_TOOLS)
    assert not any(ctx.tools[t]["is_async"] for t in FUND_TOOLS)
    assert {ctx.tools[t]["toolset"] for t in FUND_TOOLS} == {"sherwood-fund"}
    assert set(ctx.commands) == {"fund", "join-fund", "fund-approve", "fund-agents", "leaderboard"}
    assert set(ctx.skills) == {"sherwood-agent", "fund-pitch", "fund-propose", "fund-settle"}
    assert all(p.name == "SKILL.md" for p in ctx.skills.values())
    assert set(ctx.hooks) == {"on_session_start", "on_session_end", "pre_tool_call", "post_tool_call", "pre_llm_call"}


def test_fund_skills_stay_short():
    for sub in ("pitch", "propose", "settle"):
        text = (ROOT / "skills" / "fund" / sub / "SKILL.md").read_text()
        assert text.startswith("---\nname: fund-") and len(text.splitlines()) < 150


def test_hooks_accept_hermes_kwargs(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch)
    kw = {"session_id": "s1", "platform": "telegram", "task_id": "t1"}
    asyncio.run(ctx.fire("on_session_start", **kw))
    asyncio.run(ctx.fire("on_session_end", **kw))
    asyncio.run(ctx.fire("pre_llm_call", **kw))
    asyncio.run(ctx.fire("post_tool_call", tool_name="terminal", args={"command": "ls"}, result="", **kw))


# -- manifest -----------------------------------------------------------------------------
def test_manifest_v2_matches_registrations(tmp_path, monkeypatch):
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    ctx = boot(tmp_path, monkeypatch)
    assert manifest["name"] == "sherwood-monitor"
    assert manifest["manifest_version"] == 2
    assert manifest["requires_hermes"] == ">=0.21.5"
    assert set(manifest["provides_tools"]) == set(ctx.tools)
    assert set(manifest["provides_hooks"]) == set(ctx.hooks)
    assert manifest.get("capabilities", []) == []
    env = {e["name"]: e for e in manifest["requires_env"]}
    assert set(env) == {"TELEGRAM_BOT_TOKEN", "PRIVY_APP_ID", "PRIVY_APP_SECRET", "SHERWOOD_TG_LINK_SECRET", "TX_BUILDER_URL", "APP_BASE_URL"}
    assert {n for n, e in env.items() if e["secret"]} == {"TELEGRAM_BOT_TOKEN", "PRIVY_APP_SECRET", "SHERWOOD_TG_LINK_SECRET"}
    assert all(e.get("description") and e.get("url") for e in env.values())
    settings_keys = {f.name for f in fields(FundSettings)}
    assert set(manifest["config_schema"]) <= settings_keys
    for key, spec in manifest["config_schema"].items():
        assert spec["type"] in {"str", "int", "float", "bool", "list", "dict", "secret"}, key
    # The release workflow reads this exact line.
    assert any(line.startswith("version: ") for line in (ROOT / "plugin.yaml").read_text().splitlines())


def test_settings_resolution_order(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"fund": {"agent_name": "from-yaml", "tx_builder_url": "https://yaml.example"}}))
    monkeypatch.setenv("APP_BASE_URL", "https://env.example")
    ctx = FakeCtx({"agent_name": "from-hermes", "default_fund": {"not": "scalar"}})
    s = load_fund_settings(cfg, ctx)
    assert s.agent_name == "from-hermes"
    assert s.tx_builder_url == "https://yaml.example"
    assert s.app_base_url == "https://env.example"
    assert s.default_fund == ""


# -- tools + guard ----------------------------------------------------------------------------
def test_draft_guard_blocks_before_calldata(host):
    hook = host.hooks["pre_tool_call"][0]
    host.call_tool("fund_claim_topic", {"topic": "TSLA momentum"})
    ok = asyncio.run(hook(tool_name="fund_draft_proposal", args=draft_args(), task_id="t"))
    assert ok is None
    too_big = asyncio.run(hook(tool_name="fund_draft_proposal", args=draft_args(max_capital="1000.01")))
    assert too_big["action"] == "block" and "exceeds_max_capital" in too_big["message"]
    deep = asyncio.run(hook(tool_name="fund_draft_proposal", args=draft_args(max_drawdown_bps=2500)))
    assert deep["action"] == "block" and "exceeds_max_drawdown" in deep["message"]
    no_claim = asyncio.run(hook(tool_name="fund_draft_proposal", args=draft_args(topic="NVDA")))
    assert "claim_mismatch" in no_claim["message"]
    garbage = asyncio.run(hook(tool_name="fund_draft_proposal", args=draft_args(max_capital="lots")))
    assert garbage["action"] == "block" and "cannot verify" in garbage["message"]
    # Other tools pass straight through the fund guard.
    assert asyncio.run(hook(tool_name="fund_status", args={})) is None


def test_tool_flow_host_side(host):
    status = host.call_tool("fund_status")
    assert status["ok"] and status["you"]["role"] == "host"
    assert "alpha-fund · USDG on Robinhood Chain (4663)" in status["card"]
    assert status["buttons"][0]["url"] == "https://app.example/fund/alpha-fund"

    assert host.call_tool("fund_claim_topic", {"topic": "TSLA momentum"})["claim"]["agent"] == "alpha"
    d = host.call_tool("fund_draft_proposal", draft_args())
    assert d["ok"] and d["proposal"]["max_capital"] == "500000000"
    assert d["proposal"]["legs"][0]["cap"] == "400000000"
    assert d["prepare"]["ready"] and d["bond"]["swap_first"] is False

    again = host.call_tool("fund_claim_topic", {"topic": "NVDA"})
    assert again["blocked"] and again["code"] == "agent_has_open_proposal"

    p = host.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": "proposed", "onchain_id": 5})
    assert p["proposal"]["onchain_id"] == 5
    for status_ in ("passed", "executed"):
        host.call_tool("fund_set_proposal_status", {"proposal_id": "p-1", "status": status_})
    s = host.call_tool("fund_record_settle", {"proposal_id": "5", "pnl": "-12.5", "pnl_bps": -250, "tx_hash": "0x" + "ab" * 32})
    assert s["receipt"]["pnl"] == "-12500000"
    assert s["post"].startswith("Settled #5 -12.5 USDG (-250 bps) by alpha")


def test_member_and_agent_approval_tools(host, tmp_path):
    m = host.call_tool("fund_approve_member", {"member": MEMBER_ADDR})
    assert m["member"]["member_no"] == 1 and m["prepare"]["body"]["depositor"] == MEMBER_ADDR
    # A guest on the same machine asks to join through the shared state file.
    from sherwood_monitor.fund.models import Agent
    from sherwood_monitor.fund.service import FundService
    from sherwood_monitor.fund.store import LocalJsonBackend

    FundService(LocalJsonBackend(tmp_path / ".hermes/plugins/sherwood-monitor/funds")).request_agent(
        "alpha-fund", Agent(name="beta", identity_id="202", address=GUEST_ADDR))
    a = host.call_tool("fund_approve_agent", {"agent": "beta"})
    assert a["prepare"]["route"] == "register-agent" and a["prepare"]["body"]["agentId"] == "202"
    a = host.call_tool("fund_approve_agent", {"agent": "beta", "tx_hash": "0x" + "cd" * 32})
    assert a["agent"]["registered"] and "prepare" not in a
    r = host.call_tool("fund_remove_agent", {"agent": "beta"})
    assert r["agent"]["removed"]
    bad = host.call_tool("fund_remove_agent", {"agent": "alpha"})
    assert bad["ok"] is False and bad["code"] == "cannot_remove_host"


def test_tools_without_agent_name_explain_setup(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch)
    out = ctx.call_tool("fund_claim_topic", {"fund": "alpha-fund", "topic": "x"})
    assert out["ok"] is False and "agent_name" in out["error"]


# -- slash commands ----------------------------------------------------------------------------
def test_commands(host):
    tg = SimpleNamespace(user_id="900", chat_id="-100500")
    card = host.run_command("fund", "", source=tg)
    assert "alpha-fund" in card and "Buttons: [Deposit] https://app.example/fund/alpha-fund" in card
    assert "/join-fund alpha-fund" in card
    assert "alpha: host" in host.run_command("fund-agents", "", source=tg)
    assert host.run_command("leaderboard") == "Leaderboard: not yet."
    assert host.run_command("join-fund").startswith("Usage")

    approved = host.run_command("fund-approve", f"77 {MEMBER_ADDR}", source=tg)
    assert approved.startswith("Member #1 approved") and '"depositor": "' + MEMBER_ADDR in approved
    stranger = SimpleNamespace(user_id="77", chat_id="-100500")
    refused = host.run_command("fund-approve", f"78 {TARGET}", source=stranger)
    assert refused.startswith("Not done: only the fund host")


def test_join_fund_command_for_a_guest(host, tmp_path, monkeypatch):
    guest_cfg = {"agent_name": "beta", "agent_identity_id": "202", "agent_address": GUEST_ADDR}
    guest = boot(tmp_path, monkeypatch, guest_cfg)  # same machine, same state dir
    out = guest.run_command("join-fund", "alpha-fund", source=SimpleNamespace(user_id="555", chat_id="-100500"))
    assert out.startswith("Asked to join alpha-fund as beta")
    listing = host.run_command("fund-agents", "alpha-fund")
    assert "beta: guest" in listing and "/fund-approve beta" in listing
    msg = host.run_command("fund-approve", "beta", source=SimpleNamespace(user_id="900", chat_id="-100500"))
    assert "Sign registerAgent" in msg and json.dumps(GUEST_ADDR) in msg
    done = host.run_command("fund-approve", "beta 0x" + "ef" * 32)
    assert "registered on the vault" in done
    assert guest.call_tool("fund_claim_topic", {"topic": "NVDA"})["ok"]


def test_join_fund_needs_identity_settings(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, {"agent_name": "beta"})
    out = ctx.run_command("join-fund", "alpha-fund")
    assert "fund.agent_identity_id" in out and "fund.agent_address" in out


def test_setup_update_scales_cap_by_the_funds_own_asset(tmp_path, monkeypatch):
    ctx = boot(tmp_path, monkeypatch, HOST_CFG)
    assert ctx.call_tool("fund_setup", {"fund": "alpha-fund", "asset": "WETH", "max_capital": "1"})["created"]
    upd = ctx.call_tool("fund_setup", {"fund": "alpha-fund", "max_capital": "2.5"})
    assert upd["ok"] and upd["created"] is False
    assert upd["state"]["max_capital"] == str(25 * 10**17)
    assert upd["prepare"]["body"]["asset"] == "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
    moved = ctx.call_tool("fund_setup", {"fund": "alpha-fund", "asset": "USDG"})
    assert moved["ok"] is False and "fixed" in moved["error"]
