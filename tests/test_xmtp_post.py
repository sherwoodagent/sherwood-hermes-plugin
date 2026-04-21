import pytest
from unittest.mock import AsyncMock, MagicMock

from sherwood_monitor.xmtp_post import post_summary


@pytest.mark.asyncio
async def test_post_summary_calls_sidecar_send_text():
    sidecar = MagicMock()
    sidecar.send_text = AsyncMock(return_value="msg_id_123")
    await post_summary(sidecar, "alpha-fund", "**hello**")
    sidecar.send_text.assert_awaited_once_with(
        subdomain="alpha-fund", text="**hello**", markdown=True
    )


@pytest.mark.asyncio
async def test_post_summary_swallows_errors(caplog):
    sidecar = MagicMock()
    sidecar.send_text = AsyncMock(side_effect=RuntimeError("boom"))
    await post_summary(sidecar, "alpha-fund", "hi")
    assert any("xmtp post failed" in r.message for r in caplog.records)
