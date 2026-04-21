from unittest.mock import AsyncMock, MagicMock

import pytest

from sherwood_monitor.config import Config
from sherwood_monitor.event_buffer import EventBuffer
from sherwood_monitor.router import EventRouter, _DEDUPE_CAPACITY


@pytest.mark.asyncio
async def test_routes_chain_event(fixture):
    cfg = Config(xmtp_summaries=True)
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    router = EventRouter(buffer=buffer, cfg=cfg, post_fn=post)
    await router.route("alpha", fixture("chain_proposal_created"))
    buffer.push.assert_called_once()
    post.assert_called_once()


@pytest.mark.asyncio
async def test_routes_xmtp_message(fixture):
    cfg = Config()
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    router = EventRouter(buffer=buffer, cfg=cfg, post_fn=post)
    await router.route("alpha", fixture("xmtp_risk_alert"))
    buffer.push.assert_called_once()


@pytest.mark.asyncio
async def test_malformed_record_logged_not_raised(caplog):
    cfg = Config()
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    router = EventRouter(buffer=buffer, cfg=cfg, post_fn=post)
    await router.route("alpha", {"source": "martian"})
    buffer.push.assert_not_called()
    assert any("decode error" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_handler_exception_logged_not_raised(caplog):
    cfg = Config(xmtp_summaries=True)
    buffer = MagicMock(spec=EventBuffer)
    buffer.push.side_effect = RuntimeError("boom")
    post = AsyncMock()
    router = EventRouter(buffer=buffer, cfg=cfg, post_fn=post)
    # Should not raise despite buffer.push blowing up
    await router.route(
        "alpha",
        {
            "source": "chain",
            "type": "VoteCast",
            "block": 1,
            "tx": "0x",
            "args": {},
        },
    )
    assert any("handler error" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_counter_increments_on_route(fixture):
    cfg = Config()
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    router = EventRouter(buffer=buffer, cfg=cfg, post_fn=post)
    await router.route("alpha", fixture("chain_vote_cast"))
    await router.route("alpha", fixture("chain_proposal_settled"))
    assert router.events_seen("alpha") == 2
    assert router.last_event_at("alpha") is not None


@pytest.mark.asyncio
async def test_duplicate_xmtp_message_dropped(fixture):
    cfg = Config()
    buf = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    router = EventRouter(buffer=buf, cfg=cfg, post_fn=post)
    raw = fixture("xmtp_risk_alert")
    await router.route("alpha", raw)
    await router.route("alpha", raw)
    # Handler only fires once (buffer.push on the first call)
    assert buf.push.call_count == 1
    # Counter reflects only unique events seen
    assert router.events_seen("alpha") == 1


@pytest.mark.asyncio
async def test_dedupe_bounded_capacity(monkeypatch):
    # Lower the cap for test speed
    monkeypatch.setattr("sherwood_monitor.router._DEDUPE_CAPACITY", 3)
    cfg = Config()
    buf = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    router = EventRouter(buffer=buf, cfg=cfg, post_fn=post)
    # Push 5 distinct XMTP messages
    for i in range(5):
        await router.route(
            "alpha",
            {
                "source": "xmtp",
                "id": f"m{i}",
                "type": "RISK_ALERT",
                "text": "msg",
                "sentAt": "2026-01-01T00:00:00Z",
                "from": "0x",
            },
        )
    # Repeat message m0 — should NOT be deduped since it was evicted from LRU (cap=3; m0,m1 evicted)
    first_count = buf.push.call_count
    await router.route(
        "alpha",
        {
            "source": "xmtp",
            "id": "m0",
            "type": "RISK_ALERT",
            "text": "msg",
            "sentAt": "2026-01-01T00:00:00Z",
            "from": "0x",
        },
    )
    # m0 was evicted, so it treats as new
    assert buf.push.call_count == first_count + 1


@pytest.mark.asyncio
async def test_chain_events_not_deduped():
    """Chain events use block+tx as natural identifier; don't dedupe."""
    cfg = Config()
    buf = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    router = EventRouter(buffer=buf, cfg=cfg, post_fn=post)
    ev = {"source": "chain", "type": "VoteCast", "block": 100, "tx": "0x", "args": {}}
    await router.route("alpha", ev)
    await router.route("alpha", ev)
    # Chain path doesn't have LRU — both fire
    assert buf.push.call_count == 2
