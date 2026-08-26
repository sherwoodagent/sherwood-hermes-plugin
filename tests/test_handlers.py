from unittest.mock import AsyncMock, MagicMock

import pytest

from sherwood_monitor.config import Config
from sherwood_monitor.event_buffer import EventBuffer
from sherwood_monitor.handlers import handle_chain_event
from sherwood_monitor.models import ChainEvent


@pytest.fixture
def cfg():
    return Config(xmtp_summaries=True, sherwood_bin="sherwood")


def _event(type_: str, args: dict[str, str] | None = None) -> ChainEvent:
    return ChainEvent(type=type_, block=1, tx="0x0", args=args or {})


@pytest.mark.asyncio
async def test_proposal_created_injects_and_posts(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = _event(
        "ProposalCreated",
        {
            "proposalId": "1",
            "proposer": "0xabc",
            "metadataName": "Aero LP",
            "metadataDescription": "1 week",
            "performanceFeeBps": "1000",
            "strategyDuration": "604800",
        },
    )
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    call_content = buffer.push.call_args.args[0]
    assert 'fund="alpha"' in call_content
    assert 'type="ProposalCreated"' in call_content
    assert "Aero LP" in call_content
    post.assert_called_once()
    assert post.call_args.args[0] == "alpha"
    assert "Proposal #1" in post.call_args.args[1]


@pytest.mark.asyncio
async def test_proposal_settled_injects_and_posts(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = _event(
        "ProposalSettled",
        {"proposalId": "1", "pnl": "500000000", "duration": "604800", "performanceFee": "50000000"},
    )
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    post.assert_called_once()
    assert "pnl" in post.call_args.args[1].lower()


@pytest.mark.asyncio
async def test_vote_cast_injects_no_post(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = _event(
        "VoteCast",
        {"proposalId": "1", "voter": "0xabc", "support": "1", "weight": "1"},
    )
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    post.assert_not_called()


@pytest.mark.asyncio
async def test_xmtp_summaries_disabled_suppresses_post(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    cfg_no_post = Config(xmtp_summaries=False, sherwood_bin="sherwood")
    ev = _event("ProposalCreated", {"proposalId": "1"})
    await handle_chain_event("alpha", ev, buffer, cfg_no_post, post)
    buffer.push.assert_called_once()
    post.assert_not_called()


@pytest.mark.asyncio
async def test_deposited_and_withdrawn_skipped(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = _event("Deposited", {"amount": "100"})
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    post.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_event_logged_not_raised(cfg, caplog):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = _event("UFOSighting", {})
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_not_called()
    post.assert_not_called()
    assert any("unhandled" in r.message.lower() for r in caplog.records)


from sherwood_monitor.handlers import handle_xmtp_message
from sherwood_monitor.models import SessionMessage


def _msg(type_: str, text: str = "hi", sender: str = "0xpeer") -> SessionMessage:
    return SessionMessage(
        id="x",
        type=type_,
        text=text,
        sent_at="2026-04-15T10:00:00Z",
        from_=sender,
    )


@pytest.mark.asyncio
async def test_risk_alert_injects_with_high_priority(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    await handle_xmtp_message("alpha", _msg("RISK_ALERT", "HF low"), buffer, cfg, post)
    buffer.push.assert_called_once()
    content = buffer.push.call_args.args[0]
    assert 'priority="high"' in content
    assert "HF low" in content
    post.assert_not_called()


@pytest.mark.asyncio
async def test_approval_request_injects_with_human_escalate(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    await handle_xmtp_message(
        "alpha", _msg("APPROVAL_REQUEST", "trade"), buffer, cfg, post
    )
    content = buffer.push.call_args.args[0]
    assert "human-escalate" in content


@pytest.mark.asyncio
async def test_plain_message_without_mention_skipped(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    await handle_xmtp_message(
        "alpha", _msg("MESSAGE", "hello team"), buffer, cfg, post
    )
    buffer.push.assert_not_called()


@pytest.mark.asyncio
async def test_plain_message_with_mention_injected(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    await handle_xmtp_message(
        "alpha", _msg("MESSAGE", "@agent thoughts?"), buffer, cfg, post
    )
    buffer.push.assert_called_once()


@pytest.mark.asyncio
async def test_plain_message_mention_respects_config(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    cfg_all = Config(inject_mentions_only=False)
    await handle_xmtp_message(
        "alpha", _msg("MESSAGE", "no mention"), buffer, cfg_all, post
    )
    buffer.push.assert_called_once()


@pytest.mark.asyncio
async def test_reaction_always_skipped(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    cfg_all = Config(inject_mentions_only=False)
    await handle_xmtp_message("alpha", _msg("REACTION", "👍"), buffer, cfg_all, post)
    buffer.push.assert_not_called()


@pytest.mark.asyncio
async def test_strategy_proposal_injects(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    await handle_xmtp_message(
        "alpha", _msg("STRATEGY_PROPOSAL", "Aero LP"), buffer, cfg, post
    )
    buffer.push.assert_called_once()


# ---------------------------------------------------------------------------
# Sandbox + guardian events (SHE-98) — fixture-driven, end to end:
# event log JSON -> decode_record -> handle_chain_event -> auto-post.
# ---------------------------------------------------------------------------

from sherwood_monitor.models import decode_record


@pytest.mark.asyncio
async def test_sandbox_payload_stored_injects_and_posts(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = decode_record(fixture("chain_sandbox_payload_stored"))
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    injected = buffer.push.call_args.args[0]
    assert 'type="SandboxPayloadStored"' in injected
    post.assert_called_once()
    assert post.call_args.args[0] == "alpha"
    summary = post.call_args.args[1]
    assert "Proposal #42" in summary
    assert "sandbox payload stored" in summary
    assert "$250.00" in summary  # funding 250000000 = $250.00 USDC
    assert "calls: 3" in summary
    assert "tokens: 2" in summary


@pytest.mark.asyncio
async def test_sandbox_run_injects_no_post(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = decode_record(fixture("chain_sandbox_run"))
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    assert 'type="SandboxRun"' in buffer.push.call_args.args[0]
    post.assert_not_called()


@pytest.mark.asyncio
async def test_review_opened_injects_and_posts(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = decode_record(fixture("chain_review_opened"))
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    post.assert_called_once()
    summary = post.call_args.args[1]
    assert "Guardian review opened" in summary
    assert "proposal #42" in summary
    assert "1,500.00 WOOD" in summary  # 1500e18 stake


@pytest.mark.asyncio
async def test_review_resolved_blocked_injects_and_posts(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = decode_record(fixture("chain_review_resolved"))
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    post.assert_called_once()
    summary = post.call_args.args[1]
    assert "Guardian review resolved" in summary
    assert "BLOCKED" in summary
    assert "300.00 WOOD" in summary


@pytest.mark.asyncio
async def test_review_resolved_cleared_verdict(cfg):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = _event(
        "ReviewResolved",
        {"proposalId": "7", "blocked": "false", "slashedAmount": "0"},
    )
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    post.assert_called_once()
    summary = post.call_args.args[1]
    assert "cleared" in summary
    assert "BLOCKED" not in summary


@pytest.mark.asyncio
async def test_guardian_slashed_injects_and_posts(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    post = AsyncMock()
    ev = decode_record(fixture("chain_guardian_slashed"))
    await handle_chain_event("alpha", ev, buffer, cfg, post)
    buffer.push.assert_called_once()
    post.assert_called_once()
    summary = post.call_args.args[1]
    assert "Guardian slashed" in summary
    assert "0xGuardian00000000000000000000000000000005" in summary
    assert "150.00 WOOD" in summary
