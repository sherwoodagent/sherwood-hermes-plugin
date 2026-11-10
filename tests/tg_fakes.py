"""Shared fakes for the Telegram tests (in-memory FundGateway, PTB-shaped mocks)."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sherwood_monitor.telegram.types import (
    DraftRequested,
    FundActionError,
    FundView,
    LeaderRow,
    Leg,
    Person,
    ProposalView,
)

CHAT = -1001234567890
HOST = 1
ALICE, BOB, CAROL, DAVE = 10, 11, 12, 13
VAULT = "0x1111111111111111111111111111111111111111"
GOV = "0x2222222222222222222222222222222222222222"
NOW = 1_700_000_000.0


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_proposal(**kw) -> ProposalView:
    base = dict(
        id="42",
        governor=GOV,
        proposer="atlas",
        thesis="Long NVDA into earnings",
        legs=(Leg("NVDA", 10000),),
        max_capital=3000,
        max_drawdown_bps=1500,
        bond_wood=12.4,
        status="voting",
        idea_by=ALICE,
        voting_ends_at=int(NOW) + 1800,
        against_bps=500,
    )
    base.update(kw)
    return ProposalView(**base)


class FakeGateway:
    """In-memory FundGateway. Mixes sync and async methods on purpose."""

    def __init__(self, *, proposals=None, host=HOST, chat=CHAT):
        self.fund = FundView(
            vault=VAULT, governor=GOV, name="Robin Fund", telegram_chat_id=chat, host_tg_id=host,
            nav=10000, pnl_bps=820, agents=("atlas",),
        )
        self.chat = chat
        self.proposals = {p.id: p for p in (proposals or [make_proposal()])}
        self.card_ids: dict[str, int] = {}
        self.drafts: list[DraftRequested] = []
        self.calls: list[tuple] = []
        self.signing_url = "https://app.sherwood.sh/host/sign?draft=d7"
        self.fail_ship = False

    def fund_for_chat(self, chat_id):
        return self.fund if int(chat_id) == self.chat else None

    async def fund_by_vault(self, vault):
        return self.fund if vault.lower() == VAULT else None

    def proposal(self, chat_id, proposal_id):
        return self.proposals.get(str(proposal_id)) if int(chat_id) == self.chat else None

    def people(self, chat_id):
        return {ALICE: Person(ALICE, "alice")}

    def wood_usd(self):
        return 0.25

    async def toggle_backer(self, chat_id, proposal_id, tg_id):
        p = self.proposals.get(proposal_id)
        if p is None:
            return None
        backers = set(p.backers)
        backers ^= {int(tg_id)}
        p = dataclasses.replace(p, backers=tuple(sorted(backers)))
        self.proposals[proposal_id] = p
        return p

    def set_card_message_id(self, chat_id, proposal_id, message_id):
        self.card_ids[proposal_id] = message_id
        self.proposals[proposal_id] = dataclasses.replace(self.proposals[proposal_id], card_message_id=message_id)

    def ship_draft(self, chat_id, draft_id, by):
        self.calls.append(("ship_draft", draft_id, by))
        if self.fail_ship:
            raise FundActionError("Bond swap still pending.")
        return "🚀 Proposal submitted."

    def host_signing_url(self, chat_id, draft_id, host_tg_id):
        self.calls.append(("host_signing_url", draft_id, host_tg_id))
        return self.signing_url

    def approve_member(self, chat_id, tg_id, by):
        self.calls.append(("approve_member", tg_id, by))
        return "Approved."

    def approve_agent(self, chat_id, name, by):
        self.calls.append(("approve_agent", name, by))
        return "Agent approved."

    def remove_agent(self, chat_id, name, by):
        self.calls.append(("remove_agent", name, by))
        return "Agent removed."

    def leaderboard(self, chat_id):
        return [LeaderRow(ALICE, 1240, 3)]

    async def on_draft_requested(self, event):
        self.drafts.append(event)


def fake_bot(bot_id: int = 999, username: str = "sherwoodagentbot") -> MagicMock:
    bot = MagicMock()
    bot.id = bot_id
    bot.username = username
    bot.can_read_all_group_messages = True
    counter = {"n": 500}

    async def _send(**kwargs):
        counter["n"] += 1
        return SimpleNamespace(message_id=counter["n"], **{k: v for k, v in kwargs.items() if k == "chat_id"})

    async def _api(endpoint, api_kwargs=None, **kwargs):  # Bot.do_api_request: methods PTB 22.8 does not know
        if endpoint == "sendWidget":
            counter["n"] += 1
            return {"message_id": counter["n"], "chat": {"id": (api_kwargs or {}).get("chat_id")}}
        return True

    bot.send_message = AsyncMock(side_effect=_send)
    bot.send_photo = AsyncMock(side_effect=_send)
    bot.do_api_request = AsyncMock(side_effect=_api)
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.edit_message_media = AsyncMock(return_value=True)
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    return bot


def refusal_text(q, bot):
    """What a tapper was told when a button did nothing: the alert's text, else the ephemeral reply to
    the callback (Bot API 10.4: ``callback_query_id`` without ``receiver_user_id``)."""
    call = q.answer.await_args
    if call and call.kwargs.get("show_alert"):
        return call.kwargs.get("text")
    for sent in reversed(bot.send_message.await_args_list):
        params = (sent.kwargs.get("api_kwargs") or {}).get("ephemeral_message_parameters") or {}
        if params == {"callback_query_id": q.id}:
            return sent.kwargs.get("text")
    return None


class FakeJob:
    def __init__(self, callback, name):
        self.callback = callback
        self.name = name
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs: list[FakeJob] = []

    def run_repeating(self, callback, interval, first=None, name=None, **kw):
        job = FakeJob(callback, name)
        job.interval = interval
        self.jobs.append(job)
        return job

    async def fire(self, job: FakeJob):
        await job.callback(SimpleNamespace(job=job))
