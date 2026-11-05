"""Thin client for the hosted prepare API (`POST {base}/prepare/<route>`).

The API (sherwoodagent/sherwood `app/src/app/api/v1/prepare/*`) turns intent
into UNSIGNED transactions. This module only builds request bodies, posts
them, and parses the response. It never holds a key and never signs: the
returned `PreparedAction` goes to the signer (the agent's scoped wallet, or the
host's Privy wallet through the Telegram lane).

Wire format (`_lib/envelope.ts`):

    {"success": true,
     "data": {"txs": [{"to", "data", "value", "chainId"}], "preconditions": [...],
              "description": "...", "note": "..."},
     "meta": {"command": "prepare.propose", "chainId": 4663, "timestamp": "..."}}

Errors come back as `{"success": false, "error": "...", "meta": {...}}` with
HTTP 400 (usage), 404, 429 (rate limit, 60/min per IP), 503 or 500.
Amounts are always base-unit decimal STRINGS; JSON numbers lose precision.

Contract: sherwoodagent/sherwood docs/tx-builder.md (PR #739, SHE-515). Every
route takes `chainId: 4663`. `set-agent-fee`, `approve-depositors`,
`propose-portfolio`, `owner-stake` and the `funds/<vault>/tg-card` read 404 on
production until #739 deploys: a 404 raises a TxBuilderError that names the
route and says so, never a bare HTTP error.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import CHAIN_ID_ROBINHOOD

DEFAULT_TX_BUILDER_URL = "https://api.sherwood.sh/api/v1"

ROUTES = (
    "create-fund",
    "approve-depositor",
    "approve-depositors",
    "register-agent",
    "deposit",
    "propose",
    "propose-portfolio",
    "vote",
    "execute",
    "settle",
    "redeem",
    "identity-mint",
    "governor-set",
    "set-agent-fee",
    "owner-stake",
)
# Added by sherwood PR #739; 404 on production until it deploys.
NEW_ROUTES = frozenset({"approve-depositors", "propose-portfolio", "set-agent-fee", "owner-stake", "tg-card"})
AGENT_FEE_MAX_BPS = 2500  # the route's own bound; the fund rules cap it at 1500
VOTES = ("For", "Against", "Abstain")
GOVERNOR_PARAMS = (
    "votingPeriod",
    "executionWindow",
    "vetoThresholdBps",
    "maxPerformanceFeeBps",
    "maxStrategyDuration",
    "cooldownPeriod",
)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]*$")
_UINT_RE = re.compile(r"^\d+$")
_POS_UINT_RE = re.compile(r"^[1-9]\d*$")

# (url, body, timeout) -> (http_status, parsed_json_body)
PostJson = Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]]


class TxBuilderError(Exception):
    def __init__(self, route: str, message: str, status: int | None = None) -> None:
        super().__init__(f"prepare/{route}: {message}" + (f" (HTTP {status})" if status else ""))
        self.route = route
        self.status = status
        self.message = message


@dataclass(frozen=True)
class UnsignedTx:
    to: str
    data: str
    value: str  # hex, e.g. "0x0"
    chain_id: int

    def as_call(self) -> dict[str, str]:
        return {"to": self.to, "data": self.data, "value": self.value}


@dataclass(frozen=True)
class PreparedAction:
    command: str
    txs: list[UnsignedTx]
    preconditions: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""
    note: str = ""
    chain_id: int | None = None
    # Every other `data` field the route returned (propose-portfolio: clone,
    # bondQuoteWood, bondApproveWood, bondShortfallWood, prelude, warnings, ...).
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def warnings(self) -> list[Any]:
        return list(self.extra.get("warnings") or [])

    @property
    def prelude(self) -> list[dict[str, Any]]:
        """Txs the caller prepends (the USDG->WOOD bond-buffer swap). Send prelude + txs."""
        return list(self.extra.get("prelude") or [])

    def wallet_send_calls(self, from_address: str) -> list[dict[str, Any]]:
        """EIP-5792 `wallet_sendCalls` params, one batch per chain.

        Some actions span chains (approve-agent pairs a fund-chain tx with a
        Base attestation), so callers get a list and send each batch on its
        own chain.
        """
        batches: dict[int, list[UnsignedTx]] = {}
        for tx in self.txs:
            batches.setdefault(tx.chain_id, []).append(tx)
        return [
            {
                "version": "2.0.0",
                "chainId": hex(chain),
                "from": from_address,
                "atomicRequired": False,
                "calls": [tx.as_call() for tx in txs],
            }
            for chain, txs in batches.items()
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "chainId": self.chain_id,
            "txs": [{**tx.as_call(), "chainId": tx.chain_id} for tx in self.txs],
            "preconditions": self.preconditions,
            "description": self.description,
            "note": self.note,
            **({"extra": self.extra} if self.extra else {}),
        }


_ENVELOPE_KEYS = frozenset({"txs", "preconditions", "description", "note"})


def _not_deployed(route: str) -> str:
    if route in NEW_ROUTES:
        return "route not deployed on this API yet (HTTP 404); it ships with sherwood PR #739 (SHE-515)"
    return "route not found (HTTP 404)"


def parse_envelope(route: str, status: int, payload: dict[str, Any]) -> PreparedAction:
    if status == 404 and not (isinstance(payload, dict) and payload.get("success")):
        raise TxBuilderError(route, _not_deployed(route), 404)
    if not isinstance(payload, dict):
        raise TxBuilderError(route, "response is not a JSON object", status)
    if not payload.get("success"):
        raise TxBuilderError(route, str(payload.get("error") or "request failed"), status)
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    try:
        txs = [
            UnsignedTx(
                to=str(t["to"]),
                data=str(t["data"]),
                value=str(t.get("value", "0x0")),
                chain_id=int(t["chainId"]),
            )
            for t in data.get("txs", [])
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise TxBuilderError(route, f"malformed tx in response: {exc}", status) from exc
    return PreparedAction(
        command=str(meta.get("command", f"prepare.{route}")),
        txs=txs,
        preconditions=list(data.get("preconditions", [])),
        description=str(data.get("description", "")),
        note=str(data.get("note", "") or ""),
        chain_id=meta.get("chainId"),
        extra={k: v for k, v in data.items() if k not in _ENVELOPE_KEYS},
    )


def _urllib_post_json(url: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"content-type": "application/json", "accept": "application/json", "user-agent": "sherwood-monitor"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https base URL from config
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        try:
            parsed = json.loads(err.read().decode() or "{}")
        except (ValueError, OSError):
            parsed = {"success": False, "error": err.reason}
        return err.code, parsed


def _urllib_get_json(url: str, timeout: float) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, method="GET", headers={"accept": "application/json", "user-agent": "sherwood-monitor"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https base URL from config
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        try:
            parsed = json.loads(err.read().decode() or "{}")
        except (ValueError, OSError):
            parsed = {"success": False, "error": err.reason}
        return err.code, parsed


# (url, timeout) -> (http_status, parsed_json_body)
GetJson = Callable[[str, float], tuple[int, dict[str, Any]]]


# -- body builders (pure; validate what the API would reject) ----------------------------
def _addr(route: str, name: str, value: Any) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.match(value):
        raise TxBuilderError(route, f"`{name}` must be a 0x-prefixed 40-hex address")
    return value


def _uint(route: str, name: str, value: Any, *, positive: bool = False) -> str:
    text = str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if not (_POS_UINT_RE if positive else _UINT_RE).match(text):
        raise TxBuilderError(route, f"`{name}` must be a {'positive ' if positive else ''}integer (base units, as a string)")
    return text


def _ipfs(route: str, value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("ipfs://") or len(value) <= len("ipfs://"):
        raise TxBuilderError(route, "`metadataURI` must be an ipfs://<CID> URI (pin the JSON first)")
    return value


def _calls(route: str, name: str, calls: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for i, c in enumerate(calls):
        target = _addr(route, f"{name}[{i}].target", c.get("target"))
        data = c.get("data")
        if not isinstance(data, str) or not _HEX_RE.match(data):
            raise TxBuilderError(route, f"`{name}[{i}].data` must be 0x hex")
        out.append({"target": target, "data": data, "value": _uint(route, f"{name}[{i}].value", c.get("value", "0"))})
    return out


def build_propose_body(
    *,
    vault: str,
    strategy: str,
    metadata_uri: str,
    strategy_duration: int,
    max_capital: int | str,
    max_drawdown_bps: int,
    execute_calls: list[dict[str, Any]],
    execute_call_caps: list[int | str],
    settlement_calls: list[dict[str, Any]],
    settlement_call_caps: list[int | str],
    co_proposers: list[dict[str, Any]] | None = None,
    chain_id: int = CHAIN_ID_ROBINHOOD,
) -> dict[str, Any]:
    route = "propose"
    if len(execute_calls) != len(execute_call_caps):
        raise TxBuilderError(route, "one execute cap per execute call")
    if len(settlement_calls) != len(settlement_call_caps):
        raise TxBuilderError(route, "one settlement cap per settlement call")
    if not isinstance(max_drawdown_bps, int) or not 0 <= max_drawdown_bps <= 10_000:
        raise TxBuilderError(route, "`envelope.maxDrawdownBps` must be an integer in [0, 10000]")
    body: dict[str, Any] = {
        "chainId": chain_id,
        "vault": _addr(route, "vault", vault),
        "strategy": _addr(route, "strategy", strategy),
        "metadataURI": _ipfs(route, metadata_uri),
        "strategyDuration": _uint(route, "strategyDuration", strategy_duration, positive=True),
        "envelope": {"maxCapital": _uint(route, "envelope.maxCapital", max_capital, positive=True), "maxDrawdownBps": max_drawdown_bps},
        "executeCalls": _calls(route, "executeCalls", execute_calls),
        "executeCallCaps": [_uint(route, "executeCallCaps", c) for c in execute_call_caps],
        "settlementCalls": _calls(route, "settlementCalls", settlement_calls),
        "settlementCallCaps": [_uint(route, "settlementCallCaps", c) for c in settlement_call_caps],
    }
    if co_proposers:
        body["coProposers"] = co_proposers
    return body


def build_propose_portfolio_body(
    *,
    vault: str,
    proposer: str,
    legs: list[dict[str, Any]],
    strategy_duration: int | str,
    max_capital: int | str | None = None,
    max_capital_decimal: str | None = None,
    metadata_uri: str | None = None,
    name: str | None = None,
    description: str | None = None,
    max_drawdown_bps: int | None = None,
    bond_buffer_bps: int | None = None,
    salt: str | None = None,
    chain_id: int = CHAIN_ID_ROBINHOOD,
) -> dict[str, Any]:
    """`POST prepare/propose-portfolio` body. Legs: `{symbol|token, weightBps, swapRoute?}`, weights sum to 10000."""
    route = "propose-portfolio"
    if not 1 <= len(legs) <= 20:
        raise TxBuilderError(route, "1..20 legs")
    out_legs: list[dict[str, Any]] = []
    total = 0
    for i, leg in enumerate(legs):
        w = leg.get("weightBps", leg.get("weight_bps"))
        if not isinstance(w, int) or w <= 0:
            raise TxBuilderError(route, f"legs[{i}].weightBps must be a positive integer")
        total += w
        item: dict[str, Any] = {"weightBps": w}
        if leg.get("token"):
            item["token"] = _addr(route, f"legs[{i}].token", leg["token"])
        elif leg.get("symbol"):
            item["symbol"] = str(leg["symbol"]).upper()
        else:
            raise TxBuilderError(route, f"legs[{i}] needs a symbol or a token address")
        if leg.get("swapRoute"):
            item["swapRoute"] = str(leg["swapRoute"])
        out_legs.append(item)
    if total != 10_000:
        raise TxBuilderError(route, f"leg weights must sum to 10000 bps (got {total})")
    if (max_capital is None) == (max_capital_decimal is None):
        raise TxBuilderError(route, "pass exactly one of max_capital (base units) or max_capital_decimal")
    if (metadata_uri is None) == (name is None):
        raise TxBuilderError(route, "pass exactly one of metadata_uri or name (+ description)")
    body: dict[str, Any] = {
        "chainId": chain_id,
        "vault": _addr(route, "vault", vault),
        "proposer": _addr(route, "proposer", proposer),
        "legs": out_legs,
        "strategyDuration": str(strategy_duration),
    }
    if max_capital is not None:
        body["maxCapital"] = _uint(route, "maxCapital", max_capital, positive=True)
    else:
        body["maxCapitalDecimal"] = str(max_capital_decimal)
    if metadata_uri is not None:
        body["metadataURI"] = _ipfs(route, metadata_uri)
    else:
        body["name"] = str(name)
        if description:
            body["description"] = str(description)
    if max_drawdown_bps is not None:
        if not isinstance(max_drawdown_bps, int) or not 0 <= max_drawdown_bps <= 10_000:
            raise TxBuilderError(route, "`maxDrawdownBps` must be an integer in [0, 10000]")
        body["maxDrawdownBps"] = max_drawdown_bps
    if bond_buffer_bps is not None:
        if not isinstance(bond_buffer_bps, int) or not 0 <= bond_buffer_bps <= 2000:
            raise TxBuilderError(route, "`bondBufferBps` must be an integer in [0, 2000]")
        body["bondBufferBps"] = bond_buffer_bps
    if salt:
        body["salt"] = salt
    return body


class TxBuilderClient:
    """Typed wrappers over the prepare routes the fund flow needs, plus the tg-card read."""

    def __init__(
        self,
        base_url: str = DEFAULT_TX_BUILDER_URL,
        *,
        post_json: PostJson | None = None,
        get_json: GetJson | None = None,
        timeout: float = 20.0,
        chain_id: int = CHAIN_ID_ROBINHOOD,
    ) -> None:
        self.base_url = (base_url or DEFAULT_TX_BUILDER_URL).rstrip("/")
        self.timeout = timeout
        self.chain_id = chain_id
        self._post_json = post_json or _urllib_post_json
        self._get_json = get_json or _urllib_get_json

    def url_for(self, route: str) -> str:
        return f"{self.base_url}/prepare/{route}"

    def prepare(self, route: str, body: dict[str, Any]) -> PreparedAction:
        if route not in ROUTES:
            raise TxBuilderError(route, f"unknown route (known: {', '.join(ROUTES)})")
        payload = {"chainId": self.chain_id, **body}
        try:
            status, parsed = self._post_json(self.url_for(route), payload, self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TxBuilderError(route, f"request failed: {exc}") from exc
        return parse_envelope(route, status, parsed)

    # -- routes ---------------------------------------------------------------------------
    def create_fund(
        self,
        *,
        creator: str,
        asset: str,
        creator_agent_id: int | str,
        metadata_uri: str,
        name: str,
        symbol: str,
        subdomain: str,
        open_deposits: bool = False,
    ) -> PreparedAction:
        r = "create-fund"
        return self.prepare(r, {
            "creator": _addr(r, "creator", creator),
            "asset": _addr(r, "asset", asset),
            "creatorAgentId": _uint(r, "creatorAgentId", creator_agent_id),
            "metadataURI": _ipfs(r, metadata_uri),
            "name": name,
            "symbol": symbol,
            "subdomain": subdomain,
            "openDeposits": bool(open_deposits),
        })

    def approve_depositor(self, *, vault: str, depositor: str) -> PreparedAction:
        r = "approve-depositor"
        return self.prepare(r, {"vault": _addr(r, "vault", vault), "depositor": _addr(r, "depositor", depositor)})

    def approve_depositors(self, *, vault: str, depositors: list[str]) -> PreparedAction:
        """N members in one `vault.approveDepositors(address[])` tx."""
        r = "approve-depositors"
        if not depositors:
            raise TxBuilderError(r, "`depositors` needs at least one address")
        return self.prepare(r, {
            "vault": _addr(r, "vault", vault),
            "depositors": [_addr(r, f"depositors[{i}]", d) for i, d in enumerate(depositors)],
        })

    def set_agent_fee(self, *, vault: str, bps: int) -> PreparedAction:
        """`vault.setAgentFeeBps`. The route accepts 0..2500 and warns above 1500; the fund
        rules refuse above 1500 before this is built (guardians block such proposals)."""
        r = "set-agent-fee"
        if not isinstance(bps, int) or not 0 <= bps <= AGENT_FEE_MAX_BPS:
            raise TxBuilderError(r, f"`bps` must be an integer in [0, {AGENT_FEE_MAX_BPS}]")
        return self.prepare(r, {"vault": _addr(r, "vault", vault), "bps": bps})

    def owner_stake(self, *, owner: str, amount_wood: int | str | None = None) -> PreparedAction:
        """`StakedWood.prepareOwnerStake` for a wallet that already created a vault."""
        r = "owner-stake"
        body: dict[str, Any] = {"owner": _addr(r, "owner", owner)}
        if amount_wood is not None:
            body["amountWood"] = _uint(r, "amountWood", amount_wood)
        return self.prepare(r, body)

    def register_agent(self, *, vault: str, agent_address: str, agent_id: int | str) -> PreparedAction:
        r = "register-agent"
        return self.prepare(r, {
            "vault": _addr(r, "vault", vault),
            "agentAddress": _addr(r, "agentAddress", agent_address),
            "agentId": _uint(r, "agentId", agent_id),
        })

    def deposit(
        self,
        *,
        vault: str,
        receiver: str,
        amount: int | str | None = None,
        amount_decimal: str | None = None,
        wrap_eth: bool = False,
    ) -> PreparedAction:
        r = "deposit"
        if (amount is None) == (amount_decimal is None):
            raise TxBuilderError(r, "pass exactly one of amount (base units) or amount_decimal")
        body: dict[str, Any] = {"vault": _addr(r, "vault", vault), "receiver": _addr(r, "receiver", receiver)}
        if amount is not None:
            body["amount"] = _uint(r, "amount", amount, positive=True)
        else:
            body["amountDecimal"] = str(amount_decimal)
        if wrap_eth:
            body["wrapEth"] = True
        return self.prepare(r, body)

    def propose(self, body: dict[str, Any]) -> PreparedAction:
        """Send a body from `build_propose_body` (it validates the shape)."""
        return self.prepare("propose", body)

    def propose_portfolio(self, body: dict[str, Any]) -> PreparedAction:
        """Send a body from `build_propose_portfolio_body`.

        Returns txs in the CLI's order, bond approve -> clone -> propose (the
        clone is dropped when a never-executed one already sits at the CREATE2
        address), plus `extra` with clone, salt, bondQuoteWood,
        bondApproveWood, bondShortfallWood, an empty `prelude` slot for the
        USDG->WOOD buffer swap, and `warnings` (open proposal / cooldown).
        """
        return self.prepare("propose-portfolio", body)

    def fund_card(self, vault: str, *, check: list[str] | None = None) -> dict[str, Any]:
        """`GET funds/<vault>/tg-card?chain=4663`: the one read behind the Telegram card.

        Returns the envelope's `data` (NAV, P&L, positions, settlements,
        members, agents, `proposals.open[]` with againstBps / voteEnd /
        review {opened, resolved, blocked, coverage}, nextAction, and
        `proposeGate` {openProposals, cooldownUntil, ready}).
        """
        r = "tg-card"
        url = f"{self.base_url}/funds/{_addr(r, 'vault', vault)}/tg-card?chain={self.chain_id}"
        if check:
            url += "&check=" + ",".join(_addr(r, "check", a) for a in check)
        try:
            status, parsed = self._get_json(url, self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TxBuilderError(r, f"request failed: {exc}") from exc
        if status == 404 and not (isinstance(parsed, dict) and parsed.get("success")):
            raise TxBuilderError(r, _not_deployed(r), 404)
        if not isinstance(parsed, dict) or not parsed.get("success"):
            raise TxBuilderError(r, str((parsed or {}).get("error") if isinstance(parsed, dict) else "bad response") or "request failed", status)
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise TxBuilderError(r, "response has no data object", status)
        return data

    def vote(self, *, vault: str, proposal_id: int | str, vote: str) -> PreparedAction:
        r = "vote"
        if vote not in VOTES:
            raise TxBuilderError(r, f"`vote` must be one of {', '.join(VOTES)}")
        return self.prepare(r, {"vault": _addr(r, "vault", vault), "proposalId": _uint(r, "proposalId", proposal_id), "vote": vote})

    def execute(self, *, vault: str, proposal_id: int | str) -> PreparedAction:
        r = "execute"
        return self.prepare(r, {"vault": _addr(r, "vault", vault), "proposalId": _uint(r, "proposalId", proposal_id)})

    def settle(self, *, vault: str, proposal_id: int | str) -> PreparedAction:
        r = "settle"
        return self.prepare(r, {"vault": _addr(r, "vault", vault), "proposalId": _uint(r, "proposalId", proposal_id)})

    def redeem(self, *, vault: str, receiver: str, shares: int | str, owner: str | None = None) -> PreparedAction:
        r = "redeem"
        body = {
            "vault": _addr(r, "vault", vault),
            "receiver": _addr(r, "receiver", receiver),
            "shares": _uint(r, "shares", shares, positive=True),
        }
        if owner is not None:
            body["owner"] = _addr(r, "owner", owner)
        return self.prepare(r, body)

    def identity_mint(self, *, name: str, description: str | None = None, image: str | None = None) -> PreparedAction:
        r = "identity-mint"
        if not name or not name.strip():
            raise TxBuilderError(r, "`name` is required")
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if image:
            body["image"] = image
        return self.prepare(r, body)

    def governor_set(self, *, vault: str, param: str, value: int | str) -> PreparedAction:
        r = "governor-set"
        if param not in GOVERNOR_PARAMS:
            raise TxBuilderError(r, f"`param` must be one of {', '.join(GOVERNOR_PARAMS)}")
        return self.prepare(r, {"vault": _addr(r, "vault", vault), "param": param, "value": _uint(r, "value", value)})
