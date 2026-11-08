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

import math
from dataclasses import asdict, dataclass, field, fields
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
    # Generic Call plan ({executeCalls, settleCalls, settlePatches, sweepTokens}), when this
    # draft is one. Empty for a portfolio or a raw-calldata draft. The CLI compiles it; the
    # plugin does not treat the stored plan as execution acceptance.
    generic_call: dict[str, Any] = field(default_factory=dict)
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
        plan = d.get("generic_call")
        d["generic_call"] = plan if isinstance(plan, dict) else {}
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


# The fund card's P&L is share-price based: (share price - 1) in bps, since a share starts at
# 1 asset. Clamped: a share price cannot go below 0 (-100%), and anything past +10,000% is a
# bad read rather than a fund.
PNL_BPS_MIN = -10_000
PNL_BPS_MAX = 1_000_000
# A block timestamp in seconds stays below this for millennia; a millisecond one does not.
MAX_PLAUSIBLE_TIMESTAMP = 10**11


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _units(value: Any) -> int | None:
    """Non-negative integer base units from a stored or read string (digits only), else None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() else None


def plausible_timestamp(value: Any) -> float | None:
    """Unix seconds, or None for anything else (NaN, inf, <= 0, milliseconds, text)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    return ts if math.isfinite(ts) and 0 < ts < MAX_PLAUSIBLE_TIMESTAMP else None


def share_price_pnl_bps(share_price: Any) -> int:
    """P&L since inception in bps from a share price (asset per whole share).

    `"1"` -> 0, `"1.0123"` -> 123, `"0.98"` -> -200 (half rounds away from zero); clamped to
    [PNL_BPS_MIN, PNL_BPS_MAX]. Missing or unreadable (no shares outstanding) -> 0.
    """
    if isinstance(share_price, Decimal):
        price = share_price if share_price.is_finite() else None
    else:
        price = _decimal(share_price)
    if price is None:
        return 0
    bps = int(((price - 1) * 10_000).to_integral_value(rounding=ROUND_HALF_UP))
    return max(PNL_BPS_MIN, min(PNL_BPS_MAX, bps))


def _ratio_bps(value: int, basis: int) -> int | None:
    """(value / basis - 1) in bps, clamped; None without a basis."""
    if basis <= 0:
        return None
    return share_price_pnl_bps(Decimal(value) / Decimal(basis))


@dataclass
class NavPosition:
    """One token of the active strategy (tg-card `activeProposal.positions[]`)."""

    token: str = ""
    symbol: str = ""  # "" when the token's symbol() read failed
    invested: str = "0"  # asset base units the strategy put into this token (its cost)
    token_amount: str = "0"  # token base units held
    decimals: int | None = None  # the token's decimals
    target_weight_bps: int = 0

    @property
    def label(self) -> str:
        if self.symbol:
            return self.symbol
        t = self.token
        return f"{t[:6]}…{t[-4:]}" if len(t) > 12 else (t or "?")

    @classmethod
    def from_dict(cls, raw: Any) -> "NavPosition":
        d = _pick(cls, raw if isinstance(raw, dict) else {})
        for key in ("token", "symbol"):
            d[key] = str(d.get(key) or "")
        for key in ("invested", "token_amount"):
            d[key] = str(_units(d.get(key)) or 0)
        d["decimals"] = None if d.get("decimals") is None else int(d["decimals"])
        d["target_weight_bps"] = int(d.get("target_weight_bps") or 0)
        return cls(**d)


@dataclass
class NavSnapshot:
    """The NAV block of the last tg-card read (`GET funds/<vault>/tg-card`), kept in the fund state.

    The Telegram fund card renders NAV, P&L and positions from it, and shows it as "last
    known" with its time when a read fails. Amounts stay strings exactly as the read gave
    them: base units (`total_assets`, `total_supply`, `idle_assets`, `position_value`) and
    the app's decimal formatting (`*_decimal`). None = the read said null.

    The vault's `totalAssets()` counts only the cash it holds: a PortfolioStrategy takes the
    capital out at execute. So NAV is that cash plus the active strategy's value
    (`positionValue` when valid, else what it invested), and the share price behind P&L is
    NAV / supply, not the read's cash-only `sharePriceDecimal`.
    """

    total_assets: str = ""  # vault cash, asset base units ("" = the read did not say)
    total_assets_decimal: str = "0"  # the same, as the read formatted it
    total_supply: str = ""  # share base units ("" = the read did not say)
    share_price_decimal: str | None = None  # the read's cash-only share price (used with nothing deployed)
    idle_assets: str | None = None  # asset base units the vault holds itself
    realized_pnl_decimal: str | None = None  # settled P&L in the scanned range
    unrealized_pnl_decimal: str | None = None  # the active strategy's value minus what it invested
    as_of: float | None = None  # the read's asOf.timestamp: block time, unix seconds
    as_of_block: str = ""
    read_at: float = 0  # this plugin's clock when the read was stored
    # 0.8.2: decimals and the active (Executed) strategy. None / "" in snapshots stored by 0.8.1.
    asset_decimals: int | None = None
    share_decimals: int | None = None
    active_proposal_id: str = ""  # "" = no strategy holds the fund's capital
    position_value: str | None = None  # the strategy's live value, asset base units
    position_value_valid: bool | None = None  # its oracle prices were good
    positions: list[NavPosition] = field(default_factory=list)
    # The executed proposal's maxCapital (asset base units, from the read's proposals list): what a
    # strategy with no positions and no live value (a Generic Call clone) is counted at.
    active_max_capital: str | None = None

    # -- derived -------------------------------------------------------------------------
    @property
    def cash_units(self) -> int | None:
        return _units(self.total_assets)

    @property
    def invested_units(self) -> int:
        return sum(_units(p.invested) or 0 for p in self.positions)

    @property
    def live_value_units(self) -> int | None:
        """The strategy's valid `positionValue`, else None."""
        return _units(self.position_value) if self.position_value_valid else None

    @property
    def deployed_units(self) -> int | None:
        """Capital the active strategy holds: its live value, else its cost (the positions'
        `investedAmount`, else the proposal's maxCapital); 0 with no strategy; None when a
        strategy is active and none of those was readable."""
        if not self.active_proposal_id:
            return 0
        live = self.live_value_units
        if live is not None:
            return live
        if self.positions:
            return self.invested_units
        return _units(self.active_max_capital) or None

    @property
    def nav_partial(self) -> bool:
        """A strategy is active and its value is unknown: NAV would be the vault's cash only.
        Also set when the read could not read the proposals while one is open (`active_proposal_id`
        "?" with no positions)."""
        return self.deployed_units is None

    @property
    def at_cost(self) -> bool:
        """A strategy is active and counted at what it invested: no valid live value. On Robinhood
        Chain the PortfolioStrategy has no `positionValue()`, so this is the usual case there."""
        return (bool(self.active_proposal_id) and self.live_value_units is None
                and bool(self.positions or _units(self.active_max_capital)))

    @property
    def nav(self) -> Decimal:
        """Cash plus the active strategy's value, in whole asset units."""
        decimal_cash = _decimal(self.total_assets_decimal) or Decimal(0)
        if self.asset_decimals is None:
            return decimal_cash  # a 0.8.1 snapshot: cash only, nothing deployed recorded
        cash = self.cash_units
        cash_value = decimal_cash if cash is None else Decimal(cash).scaleb(-self.asset_decimals)
        return cash_value + Decimal(self.deployed_units or 0).scaleb(-self.asset_decimals)

    @property
    def share_price(self) -> Decimal | None:
        """NAV per whole share (1 at inception); None with no shares or no way to know."""
        supply = _units(self.total_supply)
        if self.nav_partial or not supply:
            return None
        if self.share_decimals is not None and self.asset_decimals is not None:
            return self.nav / Decimal(supply).scaleb(-self.share_decimals)
        if not self.active_proposal_id:
            return _decimal(self.share_price_decimal)  # nothing deployed: the read's own price is right
        return None

    @property
    def pnl_bps(self) -> int:
        return share_price_pnl_bps(self.share_price)

    @property
    def shares_outstanding(self) -> bool:
        """Someone holds shares (a deposit landed), whether or not they joined in chat."""
        return bool(_units(self.total_supply))

    @property
    def taken_at(self) -> float:
        """When the snapshot describes: the block time, else when it was stored."""
        return float(self.as_of) if self.as_of else float(self.read_at)

    def check(self) -> "NavSnapshot":
        """Itself, or ValueError when a value would break the card (a non-finite NAV, a bad time)."""
        if not math.isfinite(float(self.nav)) or not math.isfinite(float(self.read_at)):
            raise ValueError("NAV snapshot has a non-finite value")
        if self.as_of is not None and plausible_timestamp(self.as_of) is None:
            raise ValueError("NAV snapshot has an implausible asOf")
        for row in self.position_rows():
            if not math.isfinite(row["value"]):
                raise ValueError("NAV snapshot has a non-finite position")
        return self

    def position_rows(self) -> list[dict[str, Any]]:
        """The card's Positions block while a strategy is active ([] with none: "none yet").

        Only `investedAmount` is per token; `positionValue` is the whole strategy's. So a
        one-token strategy shows its live value and P&L; several tokens show each at cost
        plus one "Unrealized" row (live value minus cost) when the value is readable. The
        vault's cash comes last (`cash: True`), so the rows add up to NAV.
        """
        if not self.active_proposal_id or self.asset_decimals is None:
            return []
        scale = 10**self.asset_decimals
        live = self.live_value_units
        cost = self.invested_units
        rows: list[dict[str, Any]] = []
        if len(self.positions) == 1 and live is not None:
            p = self.positions[0]
            rows.append({"symbol": p.label, "value": live / scale, "pnl_bps": _ratio_bps(live, _units(p.invested) or 0)})
        else:
            for p in self.positions:
                rows.append({"symbol": p.label, "value": (_units(p.invested) or 0) / scale, "note": "at cost"})
            strategy = f"Strategy #{self.active_proposal_id}"
            if live is not None and self.positions:
                rows.append({"symbol": "Unrealized", "value": (live - cost) / scale, "pnl_bps": _ratio_bps(live, cost)})
            elif live is not None:
                rows.append({"symbol": strategy, "value": live / scale})
            elif not self.positions and _units(self.active_max_capital):
                # No positions, no live value (a Generic Call clone): the cap it could pull.
                rows.append({"symbol": strategy, "value": (_units(self.active_max_capital) or 0) / scale,
                             "note": "at cost (max capital)"})
        cash = self.cash_units
        if cash and rows:
            rows.append({"symbol": "", "value": cash / scale, "cash": True})
        return rows

    @classmethod
    def from_dict(cls, raw: Any) -> "NavSnapshot | None":
        """A stored snapshot, or None (absent before 0.8.1, or a value that would break the card)."""
        if not isinstance(raw, dict):
            return None
        d = _pick(cls, raw)
        try:
            for key in ("total_assets", "total_supply", "as_of_block", "active_proposal_id"):
                d[key] = "" if d.get(key) is None else str(d[key])
            d["total_assets_decimal"] = str(d.get("total_assets_decimal") or "0")
            for key in ("share_price_decimal", "idle_assets", "realized_pnl_decimal", "unrealized_pnl_decimal",
                        "position_value", "active_max_capital"):
                if d.get(key) is not None:
                    d[key] = str(d[key])
            if d.get("as_of") is not None:
                d["as_of"] = float(d["as_of"])
            d["read_at"] = float(d.get("read_at") or 0)
            for key in ("asset_decimals", "share_decimals"):
                d[key] = None if d.get(key) is None else int(d[key])
            if d.get("position_value_valid") is not None:
                d["position_value_valid"] = as_bool(d["position_value_valid"])
            d["positions"] = [NavPosition.from_dict(x) for x in (raw.get("positions") or []) if isinstance(x, dict)]
            return cls(**d).check()
        except (TypeError, ValueError, ArithmeticError):
            return None


@dataclass
class FundState:
    """The whole shared state of one fund: what one JSON file holds."""

    fund: Fund
    members: list[Member] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    proposals: list[ProposalRecord] = field(default_factory=list)
    receipts: list[SettleReceipt] = field(default_factory=list)
    # NAV from the last tg-card read (None until one answers). Optional and additive: a 0.8.0
    # reader ignores the key, so the schema version stays 1.
    nav: NavSnapshot | None = None
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
            nav=NavSnapshot.from_dict(raw.get("nav")),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            revision=int(raw.get("revision", 0)),
            next_proposal_seq=int(raw.get("next_proposal_seq", 1)),
            next_member_no=int(raw.get("next_member_no", 1)),
            updated_at=float(raw.get("updated_at", 0)),
        )


def _load_list(cls: Any, raw: Iterable[dict[str, Any]] | None) -> list[Any]:
    return [cls.from_dict(x) for x in (raw or [])]
