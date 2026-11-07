"""Typed records for a group-chat fund (host + join modes).

Everything here is plain dataclasses that round-trip through JSON. Amounts are
kept as decimal strings of BASE UNITS (USDG has 6 decimals, WETH and WOOD 18)
so nothing loses precision on the way to the prepare API, which also wants
base-unit strings.

`from_dict` on every record ignores unknown keys, so a newer writer (a guest on
a newer plugin version, or the hosted fund-state API) never breaks an older
reader.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable, TypeVar

SCHEMA_VERSION = 1

CHAIN_ID_ROBINHOOD = 4663
ASSET_DECIMALS: dict[str, int] = {"USDG": 6, "WETH": 18}
WOOD_DECIMALS = 18
# Robinhood Chain mainnet tokens (sherwoodagent/sherwood sdk/src/addresses.ts, chain 4663).
TOKEN_ADDRESSES_4663: dict[str, str] = {
    "USDG": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
    "WETH": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
    "WOOD": "0xF8BC08092C06dB6148114DCf82AF881F1085f92b",
}

DEFAULT_VOTING_PERIOD_SECONDS = 1800
DEFAULT_VETO_THRESHOLD_BPS = 2000  # optimistic veto: Against >= 20% of supply
# Governor cooldown after a vetoed or settled proposal: the next propose reverts
# CooldownNotElapsed() until it passes (fork run 2026-11-05, finding 3).
DEFAULT_COOLDOWN_SECONDS = 3600
# The guardian review refuses to Approve near the governor's 20% fee cap
# (EXCESSIVE_PERFORMANCE_FEE); set the vault agent fee at or below 15%.
MAX_AGENT_FEE_BPS = 1500
# PortfolioStrategy needs a USD-pegged vault asset (AssetNotUsdPegged on WETH).
PROPOSABLE_ASSETS = frozenset({"USDG"})
# A settled proposal's bond stays in ProposerBondEscrow this long after
# executedAt + duration; a vetoed one comes back at once.
BOND_CHALLENGE_WINDOW_DAYS = 14
CLAIM_TTL_SECONDS = 600
MAX_OPEN_PER_FUND = 2
MAX_OPEN_PER_AGENT = 1

FUND_MODES = ("host", "guest")
AGENT_ROLES = ("host", "guest")

# Proposal lifecycle. `cancelled` is not on-chain: it closes a draft that was
# never proposed (or an agent's drafts when the host removes it), so an
# abandoned draft cannot hold a slot forever. After voting the governor runs a
# guardian review (`review`, 30 min); execute needs a staked guardian's
# Approve with enough WOOD coverage (`approved`).
PROPOSAL_STATUSES = (
    "draft",
    "proposed",
    "voting",
    "passed",
    "review",
    "approved",
    "vetoed",
    "executed",
    "settled",
    "cancelled",
)
# A draft counts as open: it holds the agent's single slot and one of the
# fund's two slots from the moment it is recorded.
OPEN_STATUSES = frozenset({"draft", "proposed", "voting", "passed", "review", "approved", "executed"})
TERMINAL_STATUSES = frozenset({"vetoed", "settled", "cancelled"})

T = TypeVar("T")


def _pick(cls: type[T], raw: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    return {k: v for k, v in raw.items() if k in names}


def _opt_int(v: Any) -> int | None:
    return None if v is None or v == "" else int(v)


def as_bool(v: Any) -> bool:
    """A JSON/tool flag as a bool: true/false, 1/0, the strings "true"/"false", or null (false).
    Anything else raises ValueError."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    text = str(v).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no", ""):
        return False
    raise ValueError(f"expected true or false, got {v!r}")


@dataclass
class Fund:
    """One fund. `name` is the Sherwood subdomain and the state-file key.

    `mode` records how this state file came to exist: `host` when it is the
    fund's home (created by the host's Hermes), `guest` when it is a guest's
    mirror created by `/join-fund`. Permissions never read it; they read the
    acting agent's role.
    """

    name: str
    vault: str = ""
    governor: str = ""
    chain_id: int = CHAIN_ID_ROBINHOOD
    asset: str = "USDG"
    host: str = ""  # host wallet address (vault owner)
    host_telegram_id: str = ""
    telegram_chat_id: str = ""
    voting_period: int = DEFAULT_VOTING_PERIOD_SECONDS
    veto_threshold_bps: int = DEFAULT_VETO_THRESHOLD_BPS
    mode: str = "host"
    # Per-proposal ceilings the plugin enforces before any calldata is built.
    # None = no plugin ceiling (the governor's own caps still apply on-chain).
    max_capital: str | None = None  # asset base units
    max_drawdown_bps: int | None = None
    # Vault agent (performance) fee; None = not recorded. At most MAX_AGENT_FEE_BPS.
    agent_fee_bps: int | None = None
    cooldown_period: int = DEFAULT_COOLDOWN_SECONDS
    # No new proposal before this time (set when a proposal is vetoed or settled).
    cooldown_until: float | None = None
    # Deposits open to anyone (vault created with openDeposits: true while the factory's
    # depositsRestricted is off): no approve-depositor step. Set by fund_setup; the tg-card
    # read's `fund.depositsOpen` replaces it whenever that read answers (the chain wins).
    open_deposits: bool = False
    # Consecutive tg-card reads that said `depositsOpen: false` while the record says open. The
    # app's rule read fails closed (an unread rule comes back false, with no warning), so an open
    # fund closes only after two such reads in a row.
    chain_closed_reads: int = 0
    created_at: float = 0

    @property
    def asset_decimals(self) -> int:
        return ASSET_DECIMALS.get(self.asset.upper(), 18)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Fund":
        d = _pick(cls, raw)
        d["max_drawdown_bps"] = _opt_int(d.get("max_drawdown_bps"))
        d["agent_fee_bps"] = _opt_int(d.get("agent_fee_bps"))
        if "open_deposits" in d:
            d["open_deposits"] = as_bool(d["open_deposits"])
        if "chain_closed_reads" in d:
            d["chain_closed_reads"] = _opt_int(d["chain_closed_reads"]) or 0
        if d.get("cooldown_until") is not None:
            d["cooldown_until"] = float(d["cooldown_until"])
        if d.get("max_capital") is not None:
            d["max_capital"] = str(d["max_capital"])
        for key in ("telegram_chat_id", "host_telegram_id"):
            if key in d and d[key] is not None:
                d[key] = str(d[key])
        return cls(**d)


@dataclass
class Member:
    telegram_id: str
    address: str = ""
    approved: bool = False
    joined_at: float = 0  # when the member asked to join
    member_no: int | None = None  # approval order, 1-based (internal; cards never show it)
    # Member #N on cards: this wallet's deposit order on chain (Nth wallet to deposit), from the
    # tg-card read. None until the chain says so.
    deposit_no: int | None = None
    approved_at: float | None = None
    approve_tx: str = ""
    username: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Member":
        d = _pick(cls, raw)
        d["telegram_id"] = str(d.get("telegram_id", ""))
        return cls(**d)


@dataclass
class BondPolicy:
    """How an agent sizes and funds its own WOOD proposer bond."""

    bond_bps: int = 100  # bond = 1% of the proposal's coverage (max capital)
    approve_buffer_bps: int = 500  # approve quote + 5%, never unlimited
    requote_drift_bps: int = 500  # re-quote when the WOOD price moves > 5%
    swap_gap_from: str = "USDG"  # top up a WOOD shortfall from this token

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "BondPolicy":
        return cls(**_pick(cls, raw or {}))


@dataclass
class Agent:
    name: str
    identity_id: str = ""  # ERC-8004 token id
    address: str = ""  # the agent's own scoped wallet
    role: str = "guest"
    approved: bool = False  # host said yes in chat
    registered: bool = False  # vault.registerAgent landed (host signed it)
    bond_policy: BondPolicy = field(default_factory=BondPolicy)
    owner_telegram_id: str = ""
    requested_at: float = 0
    approved_at: float | None = None
    registered_at: float | None = None
    register_tx: str = ""
    removed: bool = False
    removed_at: float | None = None

    @property
    def active(self) -> bool:
        return self.registered and not self.removed

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Agent":
        d = _pick(cls, raw)
        d["bond_policy"] = BondPolicy.from_dict(raw.get("bond_policy"))
        d["identity_id"] = str(d.get("identity_id", ""))
        return cls(**d)


@dataclass
class Claim:
    """An agent's "drafting X" reservation of a topic, CLAIM_TTL_SECONDS long."""

    agent: str
    topic: str
    expires_at: float
    created_at: float = 0
    # Set when the claim came from a chat pitch (DraftRequested): whose idea it
    # was, the pitch text, and where it was pitched. The draft inherits idea_by.
    idea_by: str = ""
    pitch: str = ""
    chat_id: str = ""
    source_message_id: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Claim":
        return cls(**_pick(cls, raw))


@dataclass
class Leg:
    """One step of a proposal.

    `summary` is the human line ("swap 400 USDG -> TSLA"). When the agent
    already has calldata (CLI `--calldata-only`), `target`/`data`/`value` carry
    it and `cap` is the max gross outflow of the vault asset for that call, in
    asset base units (0 for an approve).
    """

    summary: str
    kind: str = "call"
    phase: str = "execute"  # execute | settle
    target: str = ""
    data: str = ""
    value: str = "0"
    cap: str = "0"
    amount: str = ""  # token base units for approve legs ("" when n/a)
    # PortfolioStrategy legs: the stock token and its weight (bps of max capital).
    # The cards show these; prepare/propose-portfolio takes them directly.
    symbol: str = ""
    weight_bps: int = 0

    @property
    def has_call(self) -> bool:
        return bool(self.target and self.data)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | str) -> "Leg":
        if isinstance(raw, str):
            return cls(summary=raw)
        d = _pick(cls, raw)
        for key in ("value", "cap", "amount"):
            if key in d and d[key] is not None:
                d[key] = str(d[key])
        d.setdefault("summary", "")
        if d.get("weight_bps") not in (None, ""):
            d["weight_bps"] = int(d["weight_bps"])
        return cls(**d)


@dataclass
class Review:
    agent: str
    verdict: str  # back | object
    note: str = ""
    at: float = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Review":
        return cls(**_pick(cls, raw))


@dataclass
class ProposalRecord:
    id: str  # local id ("p-3"); the on-chain id lives in onchain_id
    proposer_agent: str
    thesis: str
    legs: list[Leg] = field(default_factory=list)
    max_capital: str = "0"  # asset base units
    max_drawdown_bps: int = 0
    bond_wood: str = "0"  # WOOD base units the proposer bonds
    status: str = "draft"
    governor: str = ""
    topic: str = ""
    onchain_id: int | None = None
    propose_tx: str = ""
    strategy_duration: int = 0
    backers: list[str] = field(default_factory=list)  # telegram ids
    objectors: list[str] = field(default_factory=list)  # telegram ids
    reviews: list[Review] = field(default_factory=list)  # other agents
    idea_by: str = ""  # telegram id of the member whose idea it was
    card_message_id: str = ""
    voting_ends_at: float | None = None
    # Chain reads (tx-builder GET funds/<vault>/tg-card): Against as bps of the
    # votable supply, the guardian review window, and the guardian Approve
    # coverage when the read exposes it ("" = unknown).
    against_bps: int = 0
    review_ends_at: float | None = None
    guardian_coverage: str = ""
    chain_synced_at: float | None = None
    # The chat message the idea was pitched in (drafts that came from a pitch).
    pitch_message_id: int | None = None
    # From the agent's own prepare/propose-portfolio quote (sherwood #740): the LEDGER's WOOD
    # price exactly as returned (the drift basis echoed on re-prepare), the bond-buffer status
    # (swap | covered | insufficient-usdg | premium-over-cap | ...) and a host-facing note.
    quote_usd_per_wood: str = ""
    bond_buffer_status: str = ""
    bond_buffer_note: str = ""
    # The host's "ship it" on the draft card (go-ahead to propose).
    shipped_at: float | None = None
    shipped_by: str = ""
    # proposal_signer: agent: the Sherwood CLI command the proposer agent runs (outside the
    # plugin, in a cron run or `hermes -z`; its CLI wallet signs) once the host ships the draft.
    # "" otherwise. fund_pending_commands stamps handed_out_at when it lists it.
    propose_command: str = ""
    handed_out_at: float | None = None
    created_at: float = 0
    updated_at: float = 0

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProposalRecord":
        d = _pick(cls, raw)
        d["legs"] = [Leg.from_dict(x) for x in raw.get("legs", [])]
        d["reviews"] = [Review.from_dict(x) for x in raw.get("reviews", [])]
        d["backers"] = [str(x) for x in raw.get("backers", [])]
        d["objectors"] = [str(x) for x in raw.get("objectors", [])]
        d["onchain_id"] = _opt_int(d.get("onchain_id"))
        d["pitch_message_id"] = _opt_int(d.get("pitch_message_id"))
        if d.get("against_bps") not in (None, ""):
            d["against_bps"] = int(d["against_bps"])
        for key in ("max_capital", "bond_wood"):
            if key in d:
                d[key] = str(d[key])
        return cls(**d)


@dataclass
class SettleReceipt:
    proposal_id: str
    agent: str
    asset: str
    pnl: str  # signed asset base units
    pnl_bps: int = 0
    tx_hash: str = ""
    onchain_id: int | None = None
    idea_by: str = ""
    thesis: str = ""
    settled_at: float = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SettleReceipt":
        d = _pick(cls, raw)
        d["pnl"] = str(d.get("pnl", "0"))
        d["onchain_id"] = _opt_int(d.get("onchain_id"))
        return cls(**d)


@dataclass
class FundState:
    """The whole shared state of one fund: what one JSON file holds."""

    fund: Fund
    members: list[Member] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    proposals: list[ProposalRecord] = field(default_factory=list)
    receipts: list[SettleReceipt] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    revision: int = 0  # bumped on every write; the HTTP backend uses it for If-Match
    next_proposal_seq: int = 1
    next_member_no: int = 1
    updated_at: float = 0

    # -- lookups -----------------------------------------------------------
    def agent(self, name: str) -> Agent | None:
        key = (name or "").strip().lower()
        for a in self.agents:
            if a.name.lower() == key:
                return a
        return None

    def agent_by_address(self, address: str) -> Agent | None:
        key = (address or "").strip().lower()
        if not key:
            return None
        for a in self.agents:
            if a.address.lower() == key:
                return a
        return None

    def host_agent(self) -> Agent | None:
        for a in self.agents:
            if a.role == "host" and not a.removed:
                return a
        return None

    def member(self, who: str) -> Member | None:
        """Find a member by telegram id or wallet address."""
        key = str(who or "").strip().lower()
        if not key:
            return None
        for m in self.members:
            if m.telegram_id.lower() == key or (m.address and m.address.lower() == key):
                return m
        return None

    def proposal(self, proposal_id: str | int) -> ProposalRecord | None:
        key = str(proposal_id).strip()
        for p in self.proposals:
            if p.id == key:
                return p
        # Accept the on-chain id too ("12" or "#12").
        bare = key.lstrip("#")
        if bare.isdigit():
            for p in self.proposals:
                if p.onchain_id is not None and p.onchain_id == int(bare):
                    return p
        return None

    def open_proposals(self) -> list[ProposalRecord]:
        return [p for p in self.proposals if p.is_open]

    # -- (de)serialization ------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FundState":
        return cls(
            fund=Fund.from_dict(raw["fund"]),
            members=_load_list(Member, raw.get("members")),
            agents=_load_list(Agent, raw.get("agents")),
            claims=_load_list(Claim, raw.get("claims")),
            proposals=_load_list(ProposalRecord, raw.get("proposals")),
            receipts=_load_list(SettleReceipt, raw.get("receipts")),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            revision=int(raw.get("revision", 0)),
            next_proposal_seq=int(raw.get("next_proposal_seq", 1)),
            next_member_no=int(raw.get("next_member_no", 1)),
            updated_at=float(raw.get("updated_at", 0)),
        )


def _load_list(cls: Any, raw: Iterable[dict[str, Any]] | None) -> list[Any]:
    return [cls.from_dict(x) for x in (raw or [])]
