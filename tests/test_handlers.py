from unittest.mock import MagicMock

import pytest

from sherwood_monitor.config import Config
from sherwood_monitor.event_buffer import EventBuffer
from sherwood_monitor.handlers import handle_chain_event
from sherwood_monitor.models import ChainEvent, decode_record


@pytest.fixture
def cfg():
    return Config(sherwood_bin="sherwood")


def _event(type_: str, args: dict[str, str] | None = None) -> ChainEvent:
    return ChainEvent(type=type_, block=1, tx="0x0", args=args or {})


@pytest.mark.asyncio
async def test_proposal_created_injects(cfg):
    buffer = MagicMock(spec=EventBuffer)
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
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()
    call_content = buffer.push.call_args.args[0]
    assert 'fund="alpha"' in call_content
    assert 'type="ProposalCreated"' in call_content
    assert "Aero LP" in call_content


@pytest.mark.asyncio
async def test_proposal_settled_injects(cfg):
    buffer = MagicMock(spec=EventBuffer)
    ev = _event(
        "ProposalSettled",
        {"proposalId": "1", "pnl": "500000000", "duration": "604800", "performanceFee": "50000000"},
    )
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()
    assert "pnl: 500000000" in buffer.push.call_args.args[0]


@pytest.mark.asyncio
async def test_vote_cast_injects(cfg):
    buffer = MagicMock(spec=EventBuffer)
    ev = _event(
        "VoteCast",
        {"proposalId": "1", "voter": "0xabc", "support": "1", "weight": "1"},
    )
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()


@pytest.mark.asyncio
async def test_deposited_and_withdrawn_skipped(cfg):
    buffer = MagicMock(spec=EventBuffer)
    ev = _event("Deposited", {"amount": "100"})
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_event_logged_not_raised(cfg, caplog):
    buffer = MagicMock(spec=EventBuffer)
    ev = _event("UFOSighting", {})
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_not_called()
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
    await handle_xmtp_message("alpha", _msg("RISK_ALERT", "HF low"), buffer, cfg)
    buffer.push.assert_called_once()
    content = buffer.push.call_args.args[0]
    assert 'priority="high"' in content
    assert "HF low" in content


@pytest.mark.asyncio
async def test_approval_request_injects_with_human_escalate(cfg):
    buffer = MagicMock(spec=EventBuffer)
    await handle_xmtp_message(
        "alpha", _msg("APPROVAL_REQUEST", "trade"), buffer, cfg
    )
    content = buffer.push.call_args.args[0]
    assert "human-escalate" in content


@pytest.mark.asyncio
async def test_plain_message_without_mention_skipped(cfg):
    buffer = MagicMock(spec=EventBuffer)
    await handle_xmtp_message(
        "alpha", _msg("MESSAGE", "hello team"), buffer, cfg
    )
    buffer.push.assert_not_called()


@pytest.mark.asyncio
async def test_plain_message_with_mention_injected(cfg):
    buffer = MagicMock(spec=EventBuffer)
    await handle_xmtp_message(
        "alpha", _msg("MESSAGE", "@agent thoughts?"), buffer, cfg
    )
    buffer.push.assert_called_once()


@pytest.mark.asyncio
async def test_plain_message_mention_respects_config(cfg):
    buffer = MagicMock(spec=EventBuffer)
    cfg_all = Config(inject_mentions_only=False)
    await handle_xmtp_message(
        "alpha", _msg("MESSAGE", "no mention"), buffer, cfg_all
    )
    buffer.push.assert_called_once()


@pytest.mark.asyncio
async def test_reaction_always_skipped(cfg):
    buffer = MagicMock(spec=EventBuffer)
    cfg_all = Config(inject_mentions_only=False)
    await handle_xmtp_message("alpha", _msg("REACTION", "👍"), buffer, cfg_all)
    buffer.push.assert_not_called()


@pytest.mark.asyncio
async def test_strategy_proposal_injects(cfg):
    buffer = MagicMock(spec=EventBuffer)
    await handle_xmtp_message(
        "alpha", _msg("STRATEGY_PROPOSAL", "Aero LP"), buffer, cfg
    )
    buffer.push.assert_called_once()


# ---------------------------------------------------------------------------
# Sandbox + guardian events (SHE-98) — fixture-driven, end to end:
# event log JSON -> decode_record -> handle_chain_event -> injection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_payload_stored_injects(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    ev = decode_record(fixture("chain_sandbox_payload_stored"))
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()
    injected = buffer.push.call_args.args[0]
    assert 'type="SandboxPayloadStored"' in injected
    assert 'fund="alpha"' in injected


@pytest.mark.asyncio
async def test_sandbox_run_injects(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    ev = decode_record(fixture("chain_sandbox_run"))
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()
    assert 'type="SandboxRun"' in buffer.push.call_args.args[0]


@pytest.mark.asyncio
async def test_review_opened_injects(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    ev = decode_record(fixture("chain_review_opened"))
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()
    assert 'type="ReviewOpened"' in buffer.push.call_args.args[0]


@pytest.mark.asyncio
async def test_review_resolved_injects(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    ev = decode_record(fixture("chain_review_resolved"))
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()
    assert 'type="ReviewResolved"' in buffer.push.call_args.args[0]


@pytest.mark.asyncio
async def test_guardian_slashed_injects(cfg, fixture):
    buffer = MagicMock(spec=EventBuffer)
    ev = decode_record(fixture("chain_guardian_slashed"))
    await handle_chain_event("alpha", ev, buffer, cfg)
    buffer.push.assert_called_once()
    injected = buffer.push.call_args.args[0]
    assert 'type="GuardianSlashed"' in injected
    assert "0xGuardian00000000000000000000000000000005" in injected
