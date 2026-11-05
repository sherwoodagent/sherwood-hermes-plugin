"""In-session slash commands (`ctx.register_command`).

Each handler takes the raw argument string plus whichever session keywords
Hermes passes (`session_id`, `session_key`, `platform`, `source`) and returns
text. `source` (a SessionSource) only tells us who typed the command and in
which chat; it is not an authorization grant. Host-only commands still need
the local agent to be the fund's host, and the on-chain step still needs the
host wallet's signature.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .applink import ensure_member_address
from .cards import render_agents, render_fund_card, short_addr
from .models import Agent
from .rules import RuleViolation, is_address
from .runtime import FundRuntime, session_ids
from .service import FundServiceError
from .store import FundNotFound, FundStoreError
from .units import UnitsError

CommandHandler = Callable[..., str]
_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

COMMANDS: dict[str, tuple[str, str]] = {
    # name: (description, args hint)
    "fund": ("Fund status card: members, agents, open proposals, claims", "[fund]"),
    "join-fund": ("Bring this Hermes into a fund as a co-manager (host approves)", "<fund>"),
    "fund-approve": ("Host: approve a member (telegram id or wallet) or a guest agent", "<telegram_id|address|agent> [address|tx]"),
    "fund-agents": ("List the fund's agents, their claims and open proposals", "[fund]"),
    "leaderboard": ("Fund leaderboard", ""),
}


def _safe(fn: Callable[..., str]) -> CommandHandler:
    def handler(raw_args: str = "", **kwargs: Any) -> str:
        try:
            return fn((raw_args or "").strip(), kwargs.get("source"))
        except RuleViolation as exc:
            return f"Not done: {exc.reason}"
        except FundNotFound as exc:
            return f"{exc}. Ask the host which fund to use, or check fund_state_backend."
        except (FundServiceError, FundStoreError, UnitsError, ValueError) as exc:
            return f"Not done: {exc}"

    handler.__name__ = fn.__name__
    return handler


def make_fund_commands(rt: FundRuntime) -> dict[str, CommandHandler]:
    svc = rt.service

    def fund(raw: str, source: Any) -> str:
        _, chat = session_ids(source)
        state = svc.load(rt.fund_for(raw.split()[0] if raw else None, chat_id=chat))
        card = render_fund_card(state, now=svc.clock(), app_base_url=rt.settings.app_base_url)
        return card.as_text()

    def join_fund(raw: str, source: Any) -> str:
        if not raw:
            return "Usage: /join-fund <fund>"
        name = raw.split()[0]
        s = rt.settings
        missing = [k for k, v in (("agent_name", s.agent_name), ("agent_identity_id", s.agent_identity_id), ("agent_address", s.agent_address)) if not v]
        if missing:
            return (
                "Set " + ", ".join(f"fund.{k}" for k in missing) + " in ~/.hermes/plugins/sherwood-monitor/config.yaml first: "
                "a guest brings its own ERC-8004 identity and its own scoped wallet."
            )
        user, _ = session_ids(source)
        if user and s.owner_telegram_id and user != s.owner_telegram_id:
            # On Telegram this command shows up as /join_fund in the group menu; a
            # member typing it must not volunteer this Hermes as a co-manager.
            return (
                "/join-fund brings a Hermes agent into a fund as a co-manager; only this Hermes's owner runs it. "
                "Members: tap 🎟 Join on the /fund card for your personal join link."
            )
        agent = svc.request_agent(
            rt.fund_for(name),
            Agent(name=s.agent_name, identity_id=s.agent_identity_id, address=s.agent_address, owner_telegram_id=s.owner_telegram_id or (user or "")),
        )
        return (
            f"Asked to join {name} as {agent.name} (identity #{agent.identity_id}, wallet {short_addr(agent.address)}).\n"
            f"Host: run /fund-approve {agent.name} and sign registerAgent. "
            "Once registered, this agent proposes with its own WOOD bond."
        )

    def fund_approve(raw: str, source: Any) -> str:
        parts = raw.split()
        if not parts:
            return "Usage: /fund-approve <telegram_id|address|agent> [address|tx_hash]"
        who, extra = parts[0], parts[1:]
        address = next((x for x in extra if is_address(x)), "")
        tx_hash = next((x for x in extra if _TX_RE.match(x)), "")
        user, chat = session_ids(source)
        fund_name = rt.fund_for(None, chat_id=chat)
        actor = rt.agent_name()
        state = svc.load(fund_name)
        if state.agent(who) or state.agent_by_address(who):
            agent, body = svc.approve_agent(fund_name, actor, who, tx_hash=tx_hash, caller_telegram_id=user)
            if agent.registered:
                return f"{agent.name} is registered on the vault: it can claim topics and propose now."
            return (
                f"Approved {agent.name}. Sign registerAgent from the host wallet to finish:\n"
                f"POST {svc.tx_builder_url}/prepare/register-agent {json.dumps(body)}\n"
                f"Then: /fund-approve {agent.name} <tx_hash>"
            )
        if not address:
            ensure_member_address(rt, fund_name, who)  # the wallet the member linked in /tg/join, if any
        try:
            member, body = svc.approve_member(fund_name, actor, who, address=address, tx_hash=tx_hash, caller_telegram_id=user)
        except RuleViolation as exc:
            if exc.code != "missing_address":
                raise
            return (
                f"Not done: {who} has not linked a wallet in the app yet (🎟 Join, /tg/join). "
                f"Pass it yourself: /fund-approve {who} <address>"
            )
        return (
            f"Member #{member.member_no} approved ({member.telegram_id or short_addr(member.address)}). "
            f"Sign approveDepositor from the host wallet:\nPOST {svc.tx_builder_url}/prepare/approve-depositor {json.dumps(body)}"
        )

    def fund_agents(raw: str, source: Any) -> str:
        _, chat = session_ids(source)
        state = svc.load(rt.fund_for(raw.split()[0] if raw else None, chat_id=chat))
        return render_agents(state, now=svc.clock())

    def leaderboard(raw: str, source: Any) -> str:
        _, chat = session_ids(source)
        name = rt.fund_for(raw.split()[0] if raw else None, chat_id=chat)
        rows = svc.leaderboard(name)
        if not rows:
            return f"Leaderboard ({name}): no settled ideas yet."
        lines = [f"Leaderboard ({name}), members by the P&L of their settled ideas:"]
        for i, r in enumerate(rows[:10], start=1):
            lines.append(f"{i}. member {r['tg_id']}: {r['pnl_bps'] / 100:+.1f}% over {r['ideas']} idea(s), {r['wins']} win(s)")
        return "\n".join(lines)

    return {
        "fund": _safe(fund),
        "join-fund": _safe(join_fund),
        "fund-approve": _safe(fund_approve),
        "fund-agents": _safe(fund_agents),
        "leaderboard": _safe(leaderboard),
    }
