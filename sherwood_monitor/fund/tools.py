"""LLM-callable fund tools.

Handlers are synchronous (`handler(args, **kwargs) -> str`, JSON out) because
every one is a quick local state operation; register them with
`is_async=False`. Amounts the model passes are human decimal strings in the
fund asset ("400" USDG) or WOOD; the handlers convert to base units.
Rule failures come back as `{"ok": false, "blocked": true, "code", "error"}`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable

from . import rules
from .cards import render_fund_card, render_receipt
from .models import TOKEN_ADDRESSES_4663, WOOD_DECIMALS, Agent, Fund, FundState, Leg
from .rules import RuleViolation
from .runtime import FundRuntime
from .service import FundServiceError
from .store import FundExists, FundNotFound, FundStoreError
from .txbuilder import TxBuilderError
from .units import UnitsError, parse_base_units, to_base_units

TOOLSET = "sherwood-fund"
ToolHandler = Callable[..., str]

_FUND = {"type": "string", "description": "Fund subdomain. Optional when this Hermes runs one fund."}


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": {"fund": _FUND, **properties}, "required": required or []},
    }


_LEG = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One line, e.g. 'swap 400 USDG -> TSLA'"},
        "kind": {"type": "string", "description": "swap | approve | supply | call"},
        "phase": {"type": "string", "enum": ["execute", "settle"]},
        "target": {"type": "string", "description": "Call target from the CLI's --calldata-only output"},
        "data": {"type": "string", "description": "0x calldata from --calldata-only"},
        "value": {"type": "string", "description": "Native value in wei (usually 0)"},
        "cap": {"type": "string", "description": "Max vault-asset outflow for this call, in asset units ('400'); 0 for approve"},
        "amount": {"type": "string", "description": "For approve legs: the token amount approved. Never unlimited."},
    },
    "required": ["summary"],
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "fund_status": _schema(
        "fund_status",
        "Status of the group-chat fund: the card text (members, agents, open proposals, live claims, "
        "last settle) plus structured data. Call it before pitching or drafting.",
        {},
    ),
    "fund_setup": _schema(
        "fund_setup",
        "Host only. Create this machine's record of the fund (first call) or update its vault, governor, "
        "chat id and per-proposal limits. Returns the create-fund prepare body while the vault is not set.",
        {
            "vault": {"type": "string"},
            "governor": {"type": "string"},
            "asset": {"type": "string", "enum": ["USDG", "WETH"]},
            "telegram_chat_id": {"type": "string"},
            "host_telegram_id": {"type": "string", "description": "Telegram user id of the human host"},
            "host_address": {"type": "string", "description": "Host wallet (vault owner)"},
            "max_capital": {"type": "string", "description": "Per-proposal ceiling in asset units ('1000')"},
            "max_drawdown_bps": {"type": "integer"},
            "voting_period": {"type": "integer", "description": "Seconds (1800 for the demo vault)"},
            "metadata_uri": {"type": "string", "description": "ipfs:// fund metadata, for the create-fund body"},
        },
        ["fund"],
    ),
    "fund_claim_topic": _schema(
        "fund_claim_topic",
        "Claim a topic before drafting it ('drafting X'). Other agents see the claim; it lasts 10 minutes. "
        "Refused when another agent holds the topic, you have an open proposal, or the fund has 2 open.",
        {"topic": {"type": "string"}},
        ["topic"],
    ),
    "fund_release_claim": _schema(
        "fund_release_claim",
        "Release your claim (a topic, or all of yours) when you decide not to draft.",
        {"topic": {"type": "string"}},
    ),
    "fund_draft_proposal": _schema(
        "fund_draft_proposal",
        "Record a proposal draft after checking every coordination rule and the fund's max capital and "
        "drawdown. Needs your claim on the topic. Returns the prepare/propose payload it maps to (with "
        "anything still missing) and your bond plan. Builds no calldata and signs nothing.",
        {
            "topic": {"type": "string"},
            "thesis": {"type": "string"},
            "legs": {"type": "array", "items": _LEG},
            "max_capital": {"type": "string", "description": "Asset units, e.g. '500' (USDG)"},
            "max_drawdown_bps": {"type": "integer"},
            "strategy_duration_seconds": {"type": "integer"},
            "bond_quote_wood": {"type": "string", "description": "WOOD the bond quote asks for, e.g. '1250.5'"},
            "wood_balance": {"type": "string", "description": "Your WOOD balance, e.g. '900'"},
            "idea_by": {"type": "string", "description": "Telegram id of the member whose idea this is"},
            "strategy": {"type": "string", "description": "PortfolioStrategy clone address, when deployed"},
            "metadata_uri": {"type": "string", "description": "ipfs:// URI of the pinned proposal metadata"},
        },
        ["topic", "thesis", "legs", "max_capital", "max_drawdown_bps"],
    ),
    "fund_set_proposal_status": _schema(
        "fund_set_proposal_status",
        "Move your proposal along: proposed (with onchain_id and tx_hash once propose lands), voting, "
        "passed, vetoed, executed, cancelled (drop a draft). Proposer or host only.",
        {
            "proposal_id": {"type": "string"},
            "status": {"type": "string", "enum": ["proposed", "voting", "passed", "vetoed", "executed", "cancelled"]},
            "onchain_id": {"type": "integer"},
            "tx_hash": {"type": "string"},
            "card_message_id": {"type": "string"},
        },
        ["proposal_id", "status"],
    ),
    "fund_record_vote": _schema(
        "fund_record_vote",
        "Record a chat signal on a proposal: a member backs or objects (by telegram id), or you, as an "
        "agent, review another agent's proposal (voter_kind=agent). The binding veto is on-chain Against.",
        {
            "proposal_id": {"type": "string"},
            "side": {"type": "string", "enum": ["back", "object"]},
            "voter": {"type": "string", "description": "Member telegram id; omit for your own agent review"},
            "voter_kind": {"type": "string", "enum": ["member", "agent"]},
            "note": {"type": "string"},
        },
        ["proposal_id", "side"],
    ),
    "fund_record_settle": _schema(
        "fund_record_settle",
        "After settle lands: record PnL and the tx, close the proposal and get the receipt text to post.",
        {
            "proposal_id": {"type": "string"},
            "pnl": {"type": "string", "description": "Signed asset units, e.g. '-12.5' or '40'"},
            "pnl_bps": {"type": "integer"},
            "tx_hash": {"type": "string"},
        },
        ["proposal_id", "pnl", "tx_hash"],
    ),
    "fund_approve_member": _schema(
        "fund_approve_member",
        "Host only. Approve a depositor (deposits are restricted). Returns the approve-depositor body to sign.",
        {
            "member": {"type": "string", "description": "Telegram id or wallet address"},
            "address": {"type": "string", "description": "Wallet, when approving by telegram id"},
            "tx_hash": {"type": "string"},
        },
        ["member"],
    ),
    "fund_approve_agent": _schema(
        "fund_approve_agent",
        "Host only. Approve a guest agent. Returns the register-agent body to sign; call again with "
        "tx_hash once registerAgent lands to mark it registered.",
        {"agent": {"type": "string", "description": "Agent name or wallet"}, "tx_hash": {"type": "string"}},
        ["agent"],
    ),
    "fund_remove_agent": _schema(
        "fund_remove_agent",
        "Host only. Remove a guest agent in one action: drops its claims and cancels its drafts.",
        {"agent": {"type": "string"}},
        ["agent"],
    ),
}

TOOL_NAMES: tuple[str, ...] = tuple(SCHEMAS)


# -- argument parsing (shared with the pre_tool_call guard) -------------------------------
@dataclass
class DraftArgs:
    fund: str
    topic: str
    thesis: str
    legs: list[Leg]
    max_capital: int
    max_drawdown_bps: int
    strategy_duration: int
    bond_quote_wood: int | None
    wood_balance: int | None
    idea_by: str
    strategy: str
    metadata_uri: str


def parse_legs(raw: Any, decimals: int) -> list[Leg]:
    if not isinstance(raw, list):
        raise UnitsError("legs must be a list")
    legs = []
    for item in raw:
        leg = Leg.from_dict(item)
        leg.cap = str(to_base_units(leg.cap or "0", decimals))
        leg.value = str(parse_base_units(leg.value or "0"))
        legs.append(leg)
    return legs


def parse_draft_args(args: dict[str, Any], state: FundState) -> DraftArgs:
    decimals = state.fund.asset_decimals
    try:
        drawdown = int(args.get("max_drawdown_bps"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise UnitsError("max_drawdown_bps must be an integer") from exc
    quote = args.get("bond_quote_wood")
    balance = args.get("wood_balance")
    return DraftArgs(
        fund=state.fund.name,
        topic=str(args.get("topic") or ""),
        thesis=str(args.get("thesis") or ""),
        legs=parse_legs(args.get("legs") or [], decimals),
        max_capital=to_base_units(str(args.get("max_capital", "")), decimals),
        max_drawdown_bps=drawdown,
        strategy_duration=int(args.get("strategy_duration_seconds") or 0),
        bond_quote_wood=None if quote in (None, "") else to_base_units(str(quote), WOOD_DECIMALS),
        wood_balance=None if balance in (None, "") else to_base_units(str(balance), WOOD_DECIMALS),
        idea_by=str(args.get("idea_by") or ""),
        strategy=str(args.get("strategy") or ""),
        metadata_uri=str(args.get("metadata_uri") or ""),
    )


# -- handlers ---------------------------------------------------------------------------------
def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _guarded(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> ToolHandler:
    def handler(args: dict[str, Any] | None = None, **_: Any) -> str:
        try:
            return _json({"ok": True, **fn(dict(args or {}))})
        except RuleViolation as exc:
            return _json({"ok": False, "blocked": True, "code": exc.code, "error": exc.reason})
        except (FundServiceError, FundStoreError, UnitsError, TxBuilderError, ValueError, TypeError) as exc:
            return _json({"ok": False, "error": str(exc)})

    handler.__name__ = fn.__name__
    return handler


def make_fund_tool_handlers(rt: FundRuntime) -> dict[str, ToolHandler]:
    svc = rt.service

    def fund_status(args: dict[str, Any]) -> dict[str, Any]:
        state = svc.load(rt.fund_for(args.get("fund")))
        now = svc.clock()
        card = render_fund_card(state, now=now, app_base_url=rt.settings.app_base_url)
        me = state.agent(rt.settings.agent_name) if rt.settings.agent_name else None
        return {
            "fund": state.fund.name,
            "card": card.text,
            "buttons": [asdict(b) for b in card.buttons],
            "you": None if me is None else {"name": me.name, "role": me.role, "registered": me.registered},
            "open_proposals": [asdict(p) for p in state.open_proposals()],
            "claims": [asdict(c) for c in rules.active_claims(state, now)],
            "agents": [asdict(a) for a in state.agents],
            "members": {"approved": sum(m.approved for m in state.members), "waiting": sum(not m.approved for m in state.members)},
        }

    def fund_setup(args: dict[str, Any]) -> dict[str, Any]:
        name = rt.fund_for(args.get("fund"))
        agent = rt.agent_name()
        fields_ = {
            k: str(args[k])
            for k in ("vault", "governor", "telegram_chat_id", "host_telegram_id")
            if args.get(k) not in (None, "")
        }
        asset_arg = str(args.get("asset") or "").upper()
        if asset_arg and asset_arg not in ("USDG", "WETH"):
            raise UnitsError("asset must be USDG or WETH")
        ints = {k: int(args[k]) for k in ("max_drawdown_bps", "voting_period") if args.get(k) not in (None, "")}
        cap_raw = args.get("max_capital")
        host_address = str(args.get("host_address") or rt.settings.agent_address or "")
        for key in ("vault", "governor"):
            if key in fields_ and not rules.is_address(fields_[key]):
                raise UnitsError(f"{key} must be a 0x address")

        def limits_for(f: Fund) -> dict[str, Any]:
            # Scale the cap by the fund's own asset (USDG 6dp, WETH 18dp), never an argument default.
            out: dict[str, Any] = dict(ints)
            if cap_raw not in (None, ""):
                out["max_capital"] = str(to_base_units(str(cap_raw), f.asset_decimals))
            return out

        created = False
        try:
            new = Fund(name=name, asset=asset_arg or "USDG", host=host_address, **fields_)
            for k, v in limits_for(new).items():
                setattr(new, k, v)
            state = svc.create_fund(
                new,
                Agent(
                    name=agent,
                    identity_id=rt.settings.agent_identity_id,
                    address=rt.settings.agent_address,
                    owner_telegram_id=rt.settings.owner_telegram_id,
                ),
            )
            created = True
        except FundExists:
            def mutate(st: FundState) -> FundState:
                rules.require(rules.check_host_action(st, agent))
                if asset_arg and asset_arg != st.fund.asset:
                    raise UnitsError(f"the fund asset is fixed once the fund exists ({st.fund.asset})")
                for k, v in {**fields_, **limits_for(st.fund)}.items():
                    setattr(st.fund, k, v)
                if host_address and not st.fund.host:
                    st.fund.host = host_address
                return st

            state = svc.backend.update(name, mutate)
        out: dict[str, Any] = {"fund": state.fund.name, "created": created, "state": asdict(state.fund)}
        if not state.fund.vault:
            out["next"] = "create the vault: sign the create-fund txs, then call fund_setup again with vault and governor"
            out["prepare"] = {
                "route": "create-fund",
                "url": f"{svc.tx_builder_url}/prepare/create-fund",
                "body": {
                    "chainId": state.fund.chain_id,
                    "creator": state.fund.host or "<host wallet>",
                    "asset": TOKEN_ADDRESSES_4663[state.fund.asset],
                    "creatorAgentId": rt.settings.agent_identity_id or "<host ERC-8004 id>",
                    "metadataURI": str(args.get("metadata_uri") or "<ipfs:// fund metadata>"),
                    "name": state.fund.name,
                    "symbol": state.fund.name[:8].upper().replace("-", ""),
                    "subdomain": state.fund.name,
                    "openDeposits": False,  # restricted: the host approves each member
                },
            }
        return out

    def fund_claim_topic(args: dict[str, Any]) -> dict[str, Any]:
        claim = svc.claim_topic(rt.fund_for(args.get("fund")), rt.agent_name(), str(args.get("topic") or ""))
        return {"claim": asdict(claim), "say": f"drafting {claim.topic} (claim held 10 min)"}

    def fund_release_claim(args: dict[str, Any]) -> dict[str, Any]:
        n = svc.release_claim(rt.fund_for(args.get("fund")), rt.agent_name(), args.get("topic") or None)
        return {"released": n}

    def fund_draft_proposal(args: dict[str, Any]) -> dict[str, Any]:
        fund = rt.fund_for(args.get("fund"))
        d = parse_draft_args(args, svc.load(fund))
        result = svc.draft_proposal(
            fund,
            rt.agent_name(),
            topic=d.topic,
            thesis=d.thesis,
            legs=d.legs,
            max_capital=d.max_capital,
            max_drawdown_bps=d.max_drawdown_bps,
            strategy_duration=d.strategy_duration,
            bond_quote_wood=d.bond_quote_wood,
            wood_balance=d.wood_balance,
            idea_by=d.idea_by,
            strategy=d.strategy,
            metadata_uri=d.metadata_uri,
        )
        return {"proposal": asdict(result.proposal), "prepare": result.prepare, "bond": result.bond}

    def fund_set_proposal_status(args: dict[str, Any]) -> dict[str, Any]:
        onchain = args.get("onchain_id")
        record = svc.set_proposal_status(
            rt.fund_for(args.get("fund")),
            rt.agent_name(),
            str(args.get("proposal_id") or ""),
            str(args.get("status") or ""),
            onchain_id=None if onchain in (None, "") else int(onchain),
            tx_hash=str(args.get("tx_hash") or ""),
            card_message_id=str(args.get("card_message_id") or ""),
        )
        return {"proposal": asdict(record)}

    def fund_record_vote(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("voter_kind") or ("member" if args.get("voter") else "agent"))
        voter = str(args.get("voter") or "") if kind == "member" else rt.agent_name()
        record = svc.record_vote(
            rt.fund_for(args.get("fund")),
            str(args.get("proposal_id") or ""),
            voter=voter,
            side=str(args.get("side") or ""),
            voter_kind=kind,
            note=str(args.get("note") or ""),
        )
        return {"proposal": asdict(record)}

    def fund_record_settle(args: dict[str, Any]) -> dict[str, Any]:
        fund = rt.fund_for(args.get("fund"))
        state = svc.load(fund)
        pnl = to_base_units(str(args.get("pnl", "")), state.fund.asset_decimals, allow_negative=True)
        receipt = svc.record_settle(
            fund,
            rt.agent_name(),
            str(args.get("proposal_id") or ""),
            pnl=pnl,
            pnl_bps=int(args.get("pnl_bps") or 0),
            tx_hash=str(args.get("tx_hash") or ""),
        )
        return {"receipt": asdict(receipt), "post": render_receipt(svc.load(fund), receipt)}

    def fund_approve_member(args: dict[str, Any]) -> dict[str, Any]:
        member, body = svc.approve_member(
            rt.fund_for(args.get("fund")),
            rt.agent_name(),
            str(args.get("member") or ""),
            address=str(args.get("address") or ""),
            tx_hash=str(args.get("tx_hash") or ""),
        )
        return {"member": asdict(member), "prepare": {"route": "approve-depositor", "url": f"{svc.tx_builder_url}/prepare/approve-depositor", "body": body}}

    def fund_approve_agent(args: dict[str, Any]) -> dict[str, Any]:
        agent, body = svc.approve_agent(
            rt.fund_for(args.get("fund")),
            rt.agent_name(),
            str(args.get("agent") or ""),
            tx_hash=str(args.get("tx_hash") or ""),
        )
        out: dict[str, Any] = {"agent": asdict(agent)}
        if not agent.registered:
            out["prepare"] = {"route": "register-agent", "url": f"{svc.tx_builder_url}/prepare/register-agent", "body": body}
        return out

    def fund_remove_agent(args: dict[str, Any]) -> dict[str, Any]:
        agent = svc.remove_agent(rt.fund_for(args.get("fund")), rt.agent_name(), str(args.get("agent") or ""))
        return {"agent": asdict(agent), "note": "proposals already on-chain keep running; veto them if needed"}

    fns = [
        fund_status,
        fund_setup,
        fund_claim_topic,
        fund_release_claim,
        fund_draft_proposal,
        fund_set_proposal_status,
        fund_record_vote,
        fund_record_settle,
        fund_approve_member,
        fund_approve_agent,
        fund_remove_agent,
    ]
    return {fn.__name__: _guarded(fn) for fn in fns}


# Errors a guard treats as "cannot verify the draft" (it then blocks).
GUARD_ERRORS = (FundNotFound, FundServiceError, FundStoreError, UnitsError, ValueError, TypeError)
