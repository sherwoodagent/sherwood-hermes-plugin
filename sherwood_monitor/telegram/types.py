"""View types the Telegram layer renders, and the port it needs from fund state.

The plugin-core lane owns the real ``ProposalRecord`` / ``Fund`` models and the
JSON store (``sherwood_monitor/fund/``). This module mirrors only what the
cards need, as frozen dataclasses, so renderers and tests never import the
fund package. ``sherwood_monitor/fund/telegram.py`` maps real records onto
these views and implements :class:`FundGateway`.

Pure module: no Telegram or Hermes imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Mapping, Protocol, Sequence, runtime_checkable

PROPOSAL_STATUSES = (
    "draft", "proposed", "voting", "passed", "review", "approved", "vetoed", "executed", "settled", "cancelled",
)
DEFAULT_THRESHOLD_BPS = 2000


@dataclass(frozen=True)
class Person:
    """A Telegram user as the cards show them."""

    tg_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    @property
    def handle(self) -> str:
        """``@username`` when set, else initials (``AS``), else ``user<id>``."""
        if self.username:
            return "@" + self.username.lstrip("@")
        initials = "".join(
            part.strip()[0].upper() for part in (self.first_name, self.last_name) if part and part.strip()
        )
        return initials or f"user{self.tg_id}"


@dataclass(frozen=True)
class Leg:
    """A leg as the card shows it. ``weight_bps == 0`` = a summary line (raw-calldata legs)."""

    symbol: str
    weight_bps: int


@dataclass(frozen=True)
class ProposalView:
    """Mirror of plugin-core's ``ProposalRecord`` (only the fields cards read)."""

    id: str
    governor: str
    proposer: str  # proposer agent name
    thesis: str
    legs: tuple[Leg, ...] = ()
    max_capital: float = 0.0
    max_drawdown_bps: int = 0
    bond_wood: float = 0.0
    status: str = "draft"
    backers: tuple[int, ...] = ()
    objectors: tuple[int, ...] = ()
    idea_by: int | None = None
    card_message_id: int | None = None
    voting_ends_at: int | None = None
    against_bps: int = 0
    threshold_bps: int = DEFAULT_THRESHOLD_BPS
    image_url: str | None = None
    capital_symbol: str = "USDC"
    onchain_id: int | None = None  # the governor's proposal id; vote links need it
    review_ends_at: int | None = None  # guardian review window end (status "review")
    guardian_coverage: str = ""  # tg-card review.coverage.kind: none | partial | full | unpriced
    bond_buffer_status: str = ""  # propose-portfolio bondBuffer.status (sherwood #740)
    bond_buffer_note: str = ""  # bufferNote, or the 409 premium message
    # proposal_signer: agent: the signed CLI command the proposer agent runs once shipped ("" otherwise).
    propose_command: str = ""

    @property
    def display_id(self) -> str:
        return str(self.onchain_id) if self.onchain_id is not None else self.id


@dataclass(frozen=True)
class Position:
    symbol: str
    value: float
    pnl_bps: int | None = None


@dataclass(frozen=True)
class MemberView:
    tg_id: int
    number: int  # member #N, in join order
    share_bps: int
    joined_at: int | None = None  # unix seconds
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    address: str | None = None  # the wallet linked in the app (/tg/join), when known

    @property
    def person(self) -> Person:
        return Person(self.tg_id, self.username, self.first_name, self.last_name)


@dataclass(frozen=True)
class FundView:
    """Mirror of plugin-core's ``Fund``."""

    vault: str
    governor: str
    name: str
    telegram_chat_id: int | None = None
    host_tg_id: int | None = None
    members: tuple[MemberView, ...] = ()
    agents: tuple[str, ...] = ()
    nav: float = 0.0
    pnl_bps: int = 0
    positions: tuple[Position, ...] = ()
    open_proposals: tuple[ProposalView, ...] = ()
    nav_symbol: str = "USDC"
    cooldown_until: int | None = None  # no new proposal before this (governor cooldown)
    # Deposits open to anyone (no approve-depositor step): Join hands out the deposit link too.
    deposits_open: bool = False


@dataclass(frozen=True)
class Receipt:
    """Settlement receipt: ``+8.2% on NVDA · idea by @alice · backed by 5``."""

    proposal_id: str
    pnl_bps: int
    symbol: str
    idea_by: int | None = None
    backers: int = 0


@dataclass(frozen=True)
class LeaderRow:
    tg_id: int
    pnl_bps: int
    ideas: int = 0
    wins: int = 0


@dataclass(frozen=True)
class DraftRequested:
    """Emitted when a pitch earns enough 🔥 or the host says "ship it".

    The first three fields are the contract with plugin-core; the rest is
    context it can use (which chat, what fired it).
    """

    idea_by: int
    text: str
    source_message_id: int
    chat_id: int = 0
    trigger: str = "fire"  # "fire" | "ship_it"
    triggered_by: int | None = None
    fire_count: int = 0
    idea_by_name: str | None = None
    created_at: float = field(default=0.0, compare=False)


class FundActionError(Exception):
    """Raised by a gateway action with a short, user-facing reason (shown as a toast)."""


MaybeAwaitable = Any  # T | Awaitable[T]; callers use maybe_await()


@runtime_checkable
class FundGateway(Protocol):
    """What the Telegram layer needs from fund state. Methods may be sync or async.

    ``sherwood_monitor.fund.telegram.CallbackFundGateway`` implements this from
    plain callables the plugin-core lane wires. Actions (``ship_draft``,
    ``approve_*``, ``remove_agent``) return a short toast on success and raise
    :class:`FundActionError` with a user-facing reason on failure.
    """

    def fund_for_chat(self, chat_id: int) -> FundView | None | Awaitable[FundView | None]: ...

    def fund_by_vault(self, vault: str) -> FundView | None | Awaitable[FundView | None]: ...

    def proposal(self, chat_id: int, proposal_id: str) -> ProposalView | None | Awaitable[ProposalView | None]: ...

    def people(self, chat_id: int) -> Mapping[int, Person] | Awaitable[Mapping[int, Person]]: ...

    def wood_usd(self) -> float | None | Awaitable[float | None]: ...

    def toggle_backer(self, chat_id: int, proposal_id: str, tg_id: int) -> ProposalView | None | Awaitable[ProposalView | None]: ...

    def set_card_message_id(self, chat_id: int, proposal_id: str, message_id: int) -> MaybeAwaitable: ...

    def ship_draft(self, chat_id: int, draft_id: str, by_tg_id: int) -> str | Awaitable[str]: ...

    def host_signing_url(self, chat_id: int, draft_id: str, host_tg_id: int) -> str | None | Awaitable[str | None]: ...

    def approve_member(self, chat_id: int, tg_id: int, by_tg_id: int) -> str | Awaitable[str]: ...

    def approve_agent(self, chat_id: int, agent_name: str, by_tg_id: int) -> str | Awaitable[str]: ...

    def remove_agent(self, chat_id: int, agent_name: str, by_tg_id: int) -> str | Awaitable[str]: ...

    def leaderboard(self, chat_id: int) -> Sequence[LeaderRow] | Awaitable[Sequence[LeaderRow]]: ...

    def on_draft_requested(self, event: DraftRequested) -> MaybeAwaitable: ...


async def maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable; return it unchanged otherwise."""
    if hasattr(value, "__await__"):
        return await value
    return value
