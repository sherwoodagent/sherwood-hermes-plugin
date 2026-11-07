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

from ..session import current_caller
from . import authz, rules
from .applink import ensure_member_address
from .cards import render_fund_card, render_receipt
from .models import TOKEN_ADDRESSES_4663, WOOD_DECIMALS, Agent, Fund, FundState, Leg, as_bool
from .rules import RuleViolation
from .runtime import FundRuntime
from .service import FundServiceError
from . import signed_cli
from .signed_cli import CLI_NOTE, HANDOUT_HOLD_SECONDS, SHIP_TTL_SECONDS, register_agent_command
from .store import FundExists, FundNotFound, FundStoreError, scrub_paths

# Set once, then only the operator changes them (`hermes sherwood fund-bind`), never a tool.
LOCKED_FUND_FIELDS = ("vault", "governor", "telegram_chat_id", "host_telegram_id")
from .txbuilder import TxBuilderClient, TxBuilderError
from .units import UnitsError, parse_base_units, to_base_units

TOOLSET = "sherwood-fund"
ToolHandler = Callable[..., str]

# Chat- and model-facing answer to an approve-member request on a fund with open deposits.
OPEN_DEPOSITS_NOTE = (
    "deposits are open on this fund, nothing to approve: anyone can deposit from the fund card's "
    "💰 Deposit or 🎟 Join button"
)

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
        "symbol": {"type": "string", "description": "PortfolioStrategy leg: the stock token symbol, e.g. 'NVDA'"},
        "weight_bps": {"type": "integer", "description": "PortfolioStrategy leg: weight in bps of max capital (6000 = 60%)"},
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
            "agent_fee_bps": {
                "type": "integer",
                "description": "Vault agent fee in bps, at most 1500 (guardians refuse to approve near the 20% cap); 500 recommended",
            },
            "metadata_uri": {"type": "string", "description": "ipfs:// fund metadata, for the create-fund body"},
            "open_deposits": {
                "type": "boolean",
                "description": "Deposits open to anyone (create-fund openDeposits: true): members join and deposit with no "
                "host approval. Default false (the host approves each member). Once the vault exists, the chain's "
                "value (fund card read) replaces this.",
            },
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
            "quote_usd_per_wood": {
                "type": "string",
                "description": "From your prepare/propose-portfolio response: `quoteUsdPerWood` (the ledger's WOOD price) "
                "exactly as returned. Never `feedUsdPerWood`.",
            },
            "bond_buffer_status": {"type": "string", "description": "That response's `bondBuffer.status` (swap, covered, insufficient-usdg, premium-over-cap, ...)"},
            "bond_buffer_note": {"type": "string", "description": "That response's `bufferNote`, or the 409 premium message"},
        },
        ["topic", "thesis", "legs", "max_capital", "max_drawdown_bps"],
    ),
    "fund_set_proposal_status": _schema(
        "fund_set_proposal_status",
        "Move your proposal along: proposed (with onchain_id and tx_hash once propose lands), voting, "
        "review (guardian review window after voting), approved (a guardian approved with enough WOOD "
        "coverage; execute needs it), passed, vetoed, executed, cancelled (drop a draft). Proposer or host only.",
        {
            "proposal_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["proposed", "voting", "passed", "review", "approved", "vetoed", "executed", "cancelled"],
            },
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
        "Host only. Approve a depositor (deposits are restricted). Returns the approve-depositor body to sign. "
        "Without `address`, the wallet the member linked in the app (/tg/join) is used when there is one. "
        "On a fund with open deposits there is nothing to approve and it says so.",
        {
            "member": {"type": "string", "description": "Telegram id or wallet address"},
            "address": {"type": "string", "description": "Wallet, when approving by telegram id"},
            "tx_hash": {"type": "string"},
        },
        ["member"],
    ),
    "fund_approve_agent": _schema(
        "fund_approve_agent",
        "Host only. Approve a guest agent. Returns the register-agent body to sign (proposal_signer: host) or the "
        "Sherwood CLI command the host agent runs, `sherwood vault add ...`, which its CLI wallet signs "
        "(proposal_signer: agent); call again "
        "with tx_hash once registerAgent lands to mark it registered.",
        {"agent": {"type": "string", "description": "Agent name or wallet"}, "tx_hash": {"type": "string"}},
        ["agent"],
    ),
    "fund_remove_agent": _schema(
        "fund_remove_agent",
        "Host only. Remove a guest agent in one action: drops its claims and cancels its drafts.",
        {"agent": {"type": "string"}},
        ["agent"],
    ),
    "fund_pending_commands": _schema(
        "fund_pending_commands",
        "With proposal_signer: agent, the Sherwood CLI commands still to run (the CLI's wallet signs them): your "
        "drafts the host shipped that are not proposed yet (`sherwood strategy propose portfolio ...`) and, if "
        "you are the host, approved guest agents not registered yet (`sherwood vault add ...`). A listed propose "
        "command is marked handed out and held back for 30 minutes; propose commands are held while the vault "
        "has an open proposal on chain. Run each only in a `hermes -z` one-shot or an approve-mode cron, never "
        "from the group chat, then record it as each entry's `then` says.",
        {"include_recent": {
            "type": "boolean",
            "description": "List propose commands handed out in the last 30 minutes again. Only when you know that run did not propose.",
        }},
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
            return _json({"ok": False, "error": scrub_paths(exc)})

    handler.__name__ = fn.__name__
    return handler


def make_fund_tool_handlers(rt: FundRuntime) -> dict[str, ToolHandler]:
    svc = rt.service

    def host_gate(fund: str | None) -> str | None:
        """Refuse unless this turn's sender may use the host's authority; returns the caller's tg id."""
        caller = current_caller()
        try:
            state = svc.load(fund) if fund else None
        except FundNotFound:
            state = None
        rules.require(authz.check_caller(state, caller, rt.settings.owner_telegram_id))
        return caller.telegram_user

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
            "cooldown_until": state.fund.cooldown_until,
            "pitch_claims": [asdict(c) for c in rules.active_claims(state, now) if c.idea_by],
            "deposits_open": state.fund.open_deposits,
        }

    def fund_setup(args: dict[str, Any]) -> dict[str, Any]:
        name = rt.fund_for(args.get("fund"))
        host_gate(name)
        agent = rt.agent_name()
        fields_ = {
            k: str(args[k])
            for k in ("vault", "governor", "telegram_chat_id", "host_telegram_id")
            if args.get(k) not in (None, "")
        }
        asset_arg = str(args.get("asset") or "").upper()
        if asset_arg and asset_arg not in ("USDG", "WETH"):
            raise UnitsError("asset must be USDG or WETH")
        ints = {k: int(args[k]) for k in ("max_drawdown_bps", "voting_period", "agent_fee_bps") if args.get(k) not in (None, "")}
        if "agent_fee_bps" in ints:
            rules.require(rules.check_agent_fee(ints["agent_fee_bps"]))
        cap_raw = args.get("max_capital")
        open_raw = args.get("open_deposits")
        try:
            open_deposits = None if open_raw is None else as_bool(open_raw)
        except ValueError as exc:
            raise UnitsError("open_deposits must be true or false") from exc
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
            if open_deposits is not None:
                new.open_deposits = open_deposits
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
                for key in LOCKED_FUND_FIELDS:
                    current = str(getattr(st.fund, key) or "")
                    if key in fields_ and current and fields_[key].lower() != current.lower():
                        raise rules.RuleViolation(rules.deny(
                            "locked",
                            f"{key} is already set ({current}); it cannot be changed from here. "
                            "If it must change, ask the operator to rebind the fund.",
                        ))
                for k, v in {**fields_, **limits_for(st.fund)}.items():
                    setattr(st.fund, k, v)
                if open_deposits is not None:
                    st.fund.open_deposits = open_deposits
                    st.fund.chain_closed_reads = 0
                if host_address and not st.fund.host:
                    st.fund.host = host_address
                return st

            state = svc.backend.update(name, mutate)
        out: dict[str, Any] = {"fund": state.fund.name, "created": created, "state": asdict(state.fund)}
        notes = []
        if not rules.check_proposable_asset(state).ok:
            notes.append(rules.check_proposable_asset(state).reason)
        if state.fund.agent_fee_bps is None:
            notes.append(
                "set the vault agent fee to <= 15% before the first proposal (500 bps recommended; the default 20% "
                "makes guardians refuse to approve): `sherwood vault set-agent-fee --bps 500 --calldata-only`, "
                "host signs, then fund_setup agent_fee_bps=500"
            )
        if notes:
            out["notes"] = notes
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
                    # false: restricted, the host approves each member; true: open to anyone.
                    "openDeposits": state.fund.open_deposits,
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
            quote_usd_per_wood=str(args.get("quote_usd_per_wood") or ""),
            bond_buffer_status=str(args.get("bond_buffer_status") or ""),
            bond_buffer_note=str(args.get("bond_buffer_note") or ""),
        )
        return {"proposal": asdict(result.proposal), "prepare": result.prepare, "bond": result.bond}

    def fund_set_proposal_status(args: dict[str, Any]) -> dict[str, Any]:
        onchain = args.get("onchain_id")
        fund = rt.fund_for(args.get("fund"))
        host_gate(fund)
        record = svc.set_proposal_status(
            fund,
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
        fund = rt.fund_for(args.get("fund"))
        if kind == "member":
            caller = current_caller()
            rules.require(authz.check_member_vote_caller(svc.load(fund), caller, voter, rt.settings.owner_telegram_id))
        record = svc.record_vote(
            fund,
            str(args.get("proposal_id") or ""),
            voter=voter,
            side=str(args.get("side") or ""),
            voter_kind=kind,
            note=str(args.get("note") or ""),
        )
        return {"proposal": asdict(record)}

    def fund_record_settle(args: dict[str, Any]) -> dict[str, Any]:
        fund = rt.fund_for(args.get("fund"))
        host_gate(fund)
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
        fund = rt.fund_for(args.get("fund"))
        caller_tg = host_gate(fund)
        state = svc.load(fund)
        # Authorize first; only then the credentialed app lookup and its state write.
        rules.require(rules.check_caller_is_host(state, caller_tg))
        rules.require(rules.check_host_action(state, rt.agent_name()))
        if state.fund.open_deposits:
            return {"open_deposits": True, "approved": False, "note": OPEN_DEPOSITS_NOTE}
        if not args.get("address"):
            ensure_member_address(rt, fund, str(args.get("member") or ""))  # app link lookup
        member, body = svc.approve_member(
            fund,
            rt.agent_name(),
            str(args.get("member") or ""),
            address=str(args.get("address") or ""),
            tx_hash=str(args.get("tx_hash") or ""),
            caller_telegram_id=caller_tg,
        )
        return {"member": asdict(member), "prepare": {"route": "approve-depositor", "url": f"{svc.tx_builder_url}/prepare/approve-depositor", "body": body}}

    def fund_approve_agent(args: dict[str, Any]) -> dict[str, Any]:
        fund = rt.fund_for(args.get("fund"))
        caller_tg = host_gate(fund)
        agent, body = svc.approve_agent(
            fund,
            rt.agent_name(),
            str(args.get("agent") or ""),
            tx_hash=str(args.get("tx_hash") or ""),
            caller_telegram_id=caller_tg,
        )
        out: dict[str, Any] = {"agent": asdict(agent)}
        if not agent.registered and rt.settings.host_cosigns:
            out["prepare"] = {"route": "register-agent", "url": f"{svc.tx_builder_url}/prepare/register-agent", "body": body}
        elif not agent.registered:
            # proposal_signer: agent: the vault owner is the host agent's CLI wallet, not a browser wallet.
            out["command"] = register_agent_command(svc.load(fund).fund, agent)
            out["note"] = CLI_NOTE
            out["then"] = f"fund_approve_agent agent={agent.name} tx_hash=<registerAgent tx hash>"
        return out

    def fund_remove_agent(args: dict[str, Any]) -> dict[str, Any]:
        fund = rt.fund_for(args.get("fund"))
        caller_tg = host_gate(fund)
        agent = svc.remove_agent(fund, rt.agent_name(), str(args.get("agent") or ""), caller_telegram_id=caller_tg)
        return {"agent": asdict(agent), "note": "proposals already on-chain keep running; veto them if needed"}

    def chain_hold(state: FundState, my_address: str, drafts: list[Any], include_recent: bool) -> str | None:
        """Read the fund card before handing out a propose: why not to propose now, or None.

        Holds, in this order:
        - the card cannot be read, or a section that matters failed: the API reports a failed
          section as a `warnings` entry with empty proposal lists and a null `proposeGate`
          (fail closed: an unattended run must not guess);
        - the vault has an open proposal on chain (a propose reverts VaultHasOpenProposal, after
          the bond approve and clone may already have gone out);
        - a proposal from this agent's wallet is on chain, newer than anything recorded here (id
          above the highest recorded on-chain id and, when the card gives a created time, created
          after the draft was shipped): an earlier run may have proposed and not recorded it.
          `include_recent=true` bypasses only this one, for a proposal that predates this record.
        """
        try:
            card = TxBuilderClient(rt.settings.tx_builder_url, timeout=8.0).fund_card(state.fund.vault)
        except TxBuilderError as exc:
            return f"could not read the vault's proposals from the fund card ({scrub_paths(exc)}); propose commands are held until it answers"
        warnings = [str(w) for w in (card.get("warnings") or []) if isinstance(w, str)]
        failed = [w for w in warnings if any(k in w.lower() for k in _PROPOSAL_READ_WARNINGS)]
        if failed:
            return f"the fund card could not read the vault's proposals ({'; '.join(failed)}); propose commands are held until it can"
        gate = card.get("proposeGate")
        if not isinstance(gate, dict):
            return (
                "the fund card has no proposeGate (the governor's open-proposal count / cooldown read failed); "
                "propose commands are held until it answers"
            )
        props = card.get("proposals") if isinstance(card.get("proposals"), dict) else {}
        rows = [r for key in ("open", "recent") for r in (props.get(key) or []) if isinstance(r, dict)]
        if isinstance(card.get("activeProposal"), dict):
            rows.append(card["activeProposal"])
        recorded = [p.onchain_id for p in state.proposals if p.onchain_id is not None]
        highest = max(recorded, default=0)
        shipped = min(float(d.shipped_at or 0) for d in drafts) if drafts else 0.0
        mine: set[int] = set()
        for r in rows:
            rid = str(r.get("id") or "")
            if not (my_address and rid.isdigit() and str(r.get("proposer") or "").lower() == my_address.lower()):
                continue
            created = _created_at(r)
            if int(rid) > highest and (created is None or created >= shipped):
                mine.add(int(rid))
        unrecorded = ""
        if mine:
            ids = ", #".join(str(i) for i in sorted(mine))
            which = " or ".join(d.id for d in drafts) or "your draft"
            unrecorded = (
                f"proposal #{ids} from your wallet is on chain and newer than anything recorded here. If it is draft "
                f"{which}, record it: fund_set_proposal_status proposal_id=<draft id> status=proposed "
                "onchain_id=<that id> tx_hash=<propose tx hash>. Otherwise it predates this fund record and can be "
                "ignored: list again with include_recent=true"
            )
        try:
            open_n = int(gate.get("openProposals") or 0)
        except (TypeError, ValueError):
            open_n = 0
        open_ids = [str(r.get("id")) for r in (props.get("open") or []) if isinstance(r, dict) and r.get("id") is not None]
        if open_n > 0 or open_ids:
            ids = f" (#{', #'.join(open_ids)})" if open_ids else ""
            reason = (
                f"the vault has {max(open_n, len(open_ids))} open proposal(s) on chain{ids}: a propose now reverts "
                "VaultHasOpenProposal; wait until it is settled or vetoed (and the cooldown has passed)"
            )
            return f"{reason}. Also: {unrecorded}" if unrecorded else reason
        if unrecorded and not include_recent:
            return unrecorded
        return None

    def fund_pending_commands(args: dict[str, Any]) -> dict[str, Any]:
        state = svc.load(rt.fund_for(args.get("fund")))
        me = rt.agent_name().lower()
        out: dict[str, Any] = {"fund": state.fund.name, "proposal_signer": rt.settings.proposal_signer}
        if rt.settings.host_cosigns:
            out.update(proposals=[], registrations=[], note=(
                "proposal_signer is host: the host co-signs shipped drafts and signs registerAgent on the signing "
                "page; there is no CLI command to run here"
            ))
            return out
        try:
            include_recent = as_bool(args.get("include_recent"))
        except ValueError as exc:
            raise UnitsError("include_recent must be true or false") from exc
        now = svc.clock()
        mine = [
            p for p in state.proposals
            if p.status == "draft" and p.shipped_at and p.propose_command and p.proposer_agent.lower() == me
        ]
        held: list[dict[str, Any]] = []
        expired: list[dict[str, Any]] = []
        candidates = []
        for p in mine:
            if now - float(p.shipped_at or 0) > SHIP_TTL_SECONDS:
                expired.append({"proposal_id": p.id, "topic": p.topic, "shipped_at": p.shipped_at,
                                "reason": "shipped over 24 hours ago: not runnable; ask the host to ship it again"})
            elif p.handed_out_at and now - p.handed_out_at < HANDOUT_HOLD_SECONDS and not include_recent:
                held.append({"proposal_id": p.id, "topic": p.topic, "handed_out_at": p.handed_out_at, "reason": (
                    "handed out less than 30 minutes ago: if that run proposed it, record it with "
                    "fund_set_proposal_status; pass include_recent=true only if you know it did not run"
                )})
            else:
                candidates.append(p)
        hold = None
        if candidates:
            agent = state.agent(me)
            hold = chain_hold(state, (agent.address if agent else "") or rt.settings.agent_address, candidates, include_recent)
        if hold:
            out["chain_hold"] = hold
            held += [{"proposal_id": p.id, "topic": p.topic, "reason": hold} for p in candidates]
            candidates = []
        proposals = [
            {
                "proposal_id": p.id,
                "topic": p.topic,
                "shipped_at": p.shipped_at,
                "command": p.propose_command,
                "then": f"fund_set_proposal_status proposal_id={p.id} status=proposed onchain_id=<on-chain id> "
                        "tx_hash=<propose tx hash>",
            }
            for p in candidates
        ]
        if current_caller().messaging:
            # A chat turn has no terminal and cannot run them: listing here must not hold back the
            # operator's one-shot or cron.
            out["handed_out"] = False
        else:
            svc.mark_handed_out(state.fund.name, [p.id for p in candidates])
            out["handed_out"] = bool(candidates)
        registrations = []
        host = state.host_agent()
        if host is not None and host.name.lower() == me:
            for a in state.agents:
                if a.approved and not a.registered and not a.removed:
                    try:
                        cmd = register_agent_command(state.fund, a)
                    except RuleViolation:
                        continue
                    registrations.append({
                        "agent": a.name,
                        "command": cmd,
                        "then": f"fund_approve_agent agent={a.name} tx_hash=<registerAgent tx hash>",
                    })
        out.update(proposals=proposals, registrations=registrations, held_back=held, expired=expired, note=CLI_NOTE)
        if proposals or registrations:
            warning = signed_cli.cli_version_warning(rt.sherwood_bin)
            if warning:
                out["cli_warning"] = warning
        if proposals and state.fund.cooldown_until and state.fund.cooldown_until > now:
            out["cooldown_until"] = state.fund.cooldown_until
            out["wait"] = "the governor cooldown is on: a propose before cooldown_until reverts CooldownNotElapsed"
        return out

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
        fund_pending_commands,
    ]
    return {fn.__name__: _guarded(fn) for fn in fns}


# tg-card `warnings` that mean the proposal reads behind the propose hold failed (sherwood
# app/src/app/api/v1/funds/_lib/tg-card.ts): "Could not read [some of] the vault's proposals.",
# "Could not read the governor's open-proposal count / cooldown."
_PROPOSAL_READ_WARNINGS = ("the vault's proposals", "open-proposal count")


def _created_at(row: dict[str, Any]) -> float | None:
    """A card proposal row's creation time (unix seconds) when the card gives one; None otherwise."""
    for key in ("createdAt", "created", "proposedAt", "snapshotTimestamp"):
        try:
            value = float(row.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


# Errors a guard treats as "cannot verify the draft" (it then blocks).
GUARD_ERRORS = (FundNotFound, FundServiceError, FundStoreError, UnitsError, ValueError, TypeError)
