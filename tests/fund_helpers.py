"""Builders shared by the fund tests."""
from __future__ import annotations

from sherwood_monitor.fund.models import Agent, Fund, FundState, Leg, Member, ProposalRecord

VAULT = "0x" + "11" * 20
GOVERNOR = "0x" + "22" * 20
HOST_ADDR = "0x" + "33" * 20
GUEST_ADDR = "0x" + "44" * 20
GUEST2_ADDR = "0x" + "55" * 20
MEMBER_ADDR = "0x" + "66" * 20
TARGET = "0x" + "77" * 20
TX = "0x" + "ab" * 32
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def host_agent(registered: bool = True) -> Agent:
    return Agent(name="alpha", identity_id="101", address=HOST_ADDR, role="host", approved=True, registered=registered)


def guest_agent(name: str = "beta", address: str = GUEST_ADDR, registered: bool = True) -> Agent:
    return Agent(name=name, identity_id="202", address=address, role="guest", approved=registered, registered=registered)


def fund(**kw) -> Fund:
    base = dict(name="alpha-fund", vault=VAULT, governor=GOVERNOR, host=HOST_ADDR, host_telegram_id="900",
                telegram_chat_id="-100500", max_capital="1000000000", max_drawdown_bps=2000)
    base.update(kw)
    return Fund(**base)


def state(**kw) -> FundState:
    s = FundState(fund=fund(**kw), agents=[host_agent(), guest_agent(), guest_agent("gamma", GUEST2_ADDR)])
    s.members.append(Member(telegram_id="1", address=MEMBER_ADDR, approved=True, member_no=1))
    s.members.append(Member(telegram_id="2", approved=False))
    return s


def legs(cap: str = "400000000") -> list[Leg]:
    return [Leg(summary="swap 400 USDG -> TSLA", kind="swap", cap=cap), Leg(summary="swap TSLA -> USDG", phase="settle")]


def open_proposal(pid: str, agent: str, status: str = "proposed") -> ProposalRecord:
    return ProposalRecord(id=pid, proposer_agent=agent, thesis="t", status=status, max_capital="1", topic=pid)
