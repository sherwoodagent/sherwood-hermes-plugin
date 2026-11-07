"""0.8.1: the Telegram fund card renders the vault's NAV from the tg-card read, never a hard-wired 0.

`/fund` (and `post_fund_card`) read the chain first when the 30 s throttle allows;
a failed read renders the last snapshot with "as of HH:MM UTC". Authorization, the
pitch flow and the other cards never trigger a read. The fixture is the real read
from tests/test_fund_nav_snapshot.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram")

from sherwood_monitor.fund.service import FundService  # noqa: E402
from sherwood_monitor.fund.store import LocalJsonBackend  # noqa: E402
from sherwood_monitor.fund.telegram import ChainSync, FundTelegramBridge, fund_dict, fund_to_view  # noqa: E402
from sherwood_monitor.fund.txbuilder import TxBuilderError  # noqa: E402
from sherwood_monitor.telegram.cards import members_label, nav_notice, render_fund_card  # noqa: E402
from sherwood_monitor.telegram.handler import FundHandlers  # noqa: E402
from sherwood_monitor.telegram.runtime import TelegramRuntime  # noqa: E402
from sherwood_monitor.telegram.settings import TelegramSettings  # noqa: E402
from sherwood_monitor.telegram.types import FundView, MemberView  # noqa: E402
from tests.conftest import load_fixture  # noqa: E402
from tests.fund_helpers import Clock, fund, host_agent  # noqa: E402
from tests.tg_fakes import CHAT as TG_CHAT, FakeGateway, FakeJobQueue, fake_bot  # noqa: E402

LIVE = load_fixture("tg_card_open_usdg_100")
AS_OF = 1794091159  # the fixture's asOf.timestamp: 2026-11-07T22:39:19Z
CHAT = -100500  # fund_helpers.fund() telegram_chat_id


def make_service(tmp_path, clock) -> FundService:
    svc = FundService(LocalJsonBackend(tmp_path / "funds"), clock=clock)
    svc.create_fund(fund(open_deposits=True), host_agent())
    return svc


@pytest.fixture
def clock():
    return Clock(AS_OF + 41.0)


def view(**kw) -> FundView:
    base = dict(vault="0x" + "11" * 20, governor="", name="alpha-fund", nav_symbol="USDG", agents=("alpha",))
    base.update(kw)
    return FundView(**base)


def test_the_live_read_renders_its_nav(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    svc.apply_chain_card("alpha-fund", LIVE)
    text, _ = render_fund_card(fund_to_view(fund_dict(svc.load("alpha-fund"), now=clock())), now=clock())
    assert text == (
        "🏦 <b>alpha-fund</b>\n"
        "NAV <b>100 USDG</b> · P&amp;L <b>+0.0%</b>\n"
        "\n"
        "<b>Positions</b>\n"
        "• none yet\n"
        "\n"
        "Members 0 (depositors haven't tapped 🎟 Join yet) · Agents alpha"
    )


@pytest.mark.parametrize(
    "deposits_open, shares, members, label",
    [
        (True, True, (), "Members 0 (depositors haven't tapped 🎟 Join yet)"),
        (False, True, (), "Members 0"),  # restricted: every depositor is approved, so joined
        (True, False, (), "Members 0"),  # nobody deposited yet: 0 is right
        (True, True, (MemberView(10, 1, 0),), "Members 1"),  # a joined member: the count stands
    ],
)
def test_members_says_why_it_is_zero_next_to_a_funded_vault(deposits_open, shares, members, label):
    f = view(deposits_open=deposits_open, shares_outstanding=shares, members=members, nav=100)
    assert members_label(f) == label
    assert f"{label} · Agents alpha" in render_fund_card(f, now=AS_OF)[0]


def test_a_stale_snapshot_shows_last_known_nav_with_its_time():
    f = view(nav=100, nav_as_of=AS_OF, nav_stale=True)
    text, _ = render_fund_card(f, now=AS_OF + 600, notice="Make me admin")
    assert "NAV <b>100 USDG</b> · P&amp;L <b>+0.0%</b>" in text  # the last value, never 0
    assert text.endswith("Members 0 · Agents alpha\n\n<i>Last known NAV, as of 22:39 UTC.</i>\n<i>Make me admin</i>")
    assert nav_notice(f, AS_OF + 86_400) == "Last known NAV, as of Nov 7 22:39 UTC."  # another day: the date too
    assert nav_notice(view(nav=100, nav_as_of=AS_OF), AS_OF) is None  # fresh: no notice


def test_no_snapshot_and_a_failed_read_is_unavailable_not_zero():
    text, _ = render_fund_card(view(nav_stale=True), now=AS_OF)
    assert "NAV <b>unavailable</b> · P&amp;L <b>n/a</b>" in text and "NAV <b>0" not in text
    assert text.endswith("<i>NAV unavailable: the chain read failed. Try /fund again in a minute.</i>")


# -- the read behind the card: throttled, only for the fund card ---------------------------


class StubClient:
    def __init__(self, card=None):
        self.card = card if card is not None else LIVE["data"]
        self.fail = False
        self.calls = 0

    def fund_card(self, vault):
        self.calls += 1
        if self.fail:
            raise TxBuilderError("tg-card", "request failed: timed out")
        return self.card


def make_bridge(tmp_path, clock):
    svc = make_service(tmp_path, clock)
    client = StubClient()
    rt = SimpleNamespace(service=svc, settings=SimpleNamespace(agent_name="", host_cosigns=False, app_base_url=""))
    return FundTelegramBridge(rt, chain_sync=ChainSync(svc, client, clock=clock)), client


def test_the_fund_card_reads_the_chain_when_due_and_not_inside_the_throttle(tmp_path, clock):
    bridge, client = make_bridge(tmp_path, clock)
    assert bridge.get_fund_card_for_chat(CHAT)["nav"] == 100.0 and client.calls == 1
    clock.advance(29)
    assert bridge.get_fund_card_for_chat(CHAT)["nav"] == 100.0 and client.calls == 1  # throttled: the snapshot
    clock.advance(1)
    bridge.get_fund_card_for_chat(CHAT)
    assert client.calls == 2
    assert bridge.get_fund_card_for_chat(-1) is None and client.calls == 2  # no fund in that chat: no read


def test_other_fund_lookups_never_read_the_chain(tmp_path, clock):
    bridge, client = make_bridge(tmp_path, clock)
    assert bridge.get_fund_for_chat(CHAT)["nav"] == 0.0  # authorization, the pitch flow, other cards
    assert client.calls == 0
    bridge.get_fund_card_for_chat(CHAT)
    assert bridge.get_fund_for_chat(CHAT)["nav"] == 100.0 and client.calls == 1  # they see the snapshot


def test_a_failed_read_renders_the_last_snapshot_as_of_its_time(tmp_path, clock):
    bridge, client = make_bridge(tmp_path, clock)
    bridge.get_fund_card_for_chat(CHAT)
    clock.advance(600)
    client.fail = True
    d = bridge.get_fund_card_for_chat(CHAT)
    assert client.calls == 2 and (d["nav"], d["nav_stale"], d["nav_as_of"]) == (100.0, True, AS_OF)
    text, _ = render_fund_card(fund_to_view(d), now=clock())
    assert "NAV <b>100 USDG</b>" in text and "<i>Last known NAV, as of 22:39 UTC.</i>" in text
    clock.advance(10)  # inside the throttle after the failure: no read, still flagged
    assert bridge.get_fund_card_for_chat(CHAT)["nav_stale"] and client.calls == 2
    clock.advance(30)
    client.fail = False
    d = bridge.get_fund_card_for_chat(CHAT)
    assert client.calls == 3 and not d["nav_stale"]


def test_an_unexpected_read_error_still_renders_the_snapshot(tmp_path, clock):
    bridge, client = make_bridge(tmp_path, clock)
    bridge.get_fund_card_for_chat(CHAT)
    clock.advance(60)
    client.fund_card = lambda vault: (_ for _ in ()).throw(RuntimeError("boom"))
    d = bridge.get_fund_card_for_chat(CHAT)
    assert (d["nav"], d["nav_stale"]) == (100.0, True)


def test_a_failed_first_read_is_unavailable(tmp_path, clock):
    bridge, client = make_bridge(tmp_path, clock)
    client.fail = True
    d = bridge.get_fund_card_for_chat(CHAT)
    assert (d["nav"], d["nav_stale"], d["nav_as_of"]) == (0.0, True, None)
    assert "NAV <b>unavailable</b>" in render_fund_card(fund_to_view(d), now=clock())[0]


async def test_the_gateway_card_read_runs_off_the_loop(tmp_path, clock):
    bridge, client = make_bridge(tmp_path, clock)
    gw = bridge.gateway()
    f = await gw.fund_card_for_chat(int(CHAT))
    assert f.nav == 100.0 and f.shares_outstanding and client.calls == 1
    assert (await gw.fund_for_chat(int(CHAT))).nav == 100.0 and client.calls == 1


# -- /fund and post_fund_card go through the card read --------------------------------------


class CardGateway(FakeGateway):
    """FakeGateway plus the optional fund_card_for_chat (a chain read happened: NAV 100, stale)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.card_reads = 0
        self.card_fund = view(nav=100, nav_as_of=AS_OF, nav_stale=True, telegram_chat_id=TG_CHAT, host_tg_id=1)

    async def fund_card_for_chat(self, chat_id):
        self.card_reads += 1
        return self.card_fund if int(chat_id) == self.chat else None


def make_runtime(gw, **settings_kw):
    rt = TelegramRuntime(gw, TelegramSettings(bot_username="sherwoodagentbot", **settings_kw), clock=lambda: AS_OF + 600)
    bot = fake_bot()
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator"))  # no caps notice
    rt.attach(SimpleNamespace(bot=bot, job_queue=FakeJobQueue()))
    return rt, bot


def fund_command(chat_id=TG_CHAT):
    msg = MagicMock()
    msg.text = "/fund"
    msg.chat = SimpleNamespace(id=chat_id, type="supergroup")
    msg.reply_html = AsyncMock(return_value=SimpleNamespace(message_id=600))
    msg.reply_text = AsyncMock()
    return SimpleNamespace(effective_message=msg), msg


async def test_the_fund_command_renders_from_the_card_read():
    gw = CardGateway()
    rt, bot = make_runtime(gw)
    upd, msg = fund_command()
    await FundHandlers(rt).cmd_fund(upd, SimpleNamespace(bot=bot))
    text = msg.reply_html.await_args.args[0]
    assert gw.card_reads == 1
    assert "NAV <b>100 USDG</b>" in text and text.endswith("<i>Last known NAV, as of 22:39 UTC.</i>")


async def test_a_chat_outside_the_allowlist_never_triggers_a_read():
    gw = CardGateway()
    rt, bot = make_runtime(gw, allowed_chat_ids=frozenset({-42}))
    upd, msg = fund_command()
    await FundHandlers(rt).cmd_fund(upd, SimpleNamespace(bot=bot))
    assert gw.card_reads == 0 and not msg.reply_html.await_count and not msg.reply_text.await_count


async def test_post_fund_card_renders_from_the_card_read():
    gw = CardGateway()
    rt, bot = make_runtime(gw)
    await rt.post_fund_card(TG_CHAT)
    assert gw.card_reads == 1
    assert "NAV <b>100 USDG</b>" in bot.send_message.await_args.kwargs["text"]


async def test_a_gateway_without_the_card_read_falls_back_to_fund_for_chat():
    rt, bot = make_runtime(FakeGateway())
    await rt.post_fund_card(TG_CHAT)
    assert "NAV <b>10,000 USDC</b>" in bot.send_message.await_args.kwargs["text"]
