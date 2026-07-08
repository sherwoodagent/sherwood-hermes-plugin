"""Post markdown summaries to XMTP groups via the sidecar."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sidecar import Sidecar

_log = logging.getLogger(__name__)


async def post_summary(sidecar: "Sidecar", subdomain: str, markdown: str) -> None:
    """Post a markdown summary to the fund's XMTP group.

    Resolves subdomain→group_id via the sidecar, sends the text. All
    failures logged and swallowed; never raises.
    """
    try:
        await sidecar.send_text(subdomain=subdomain, text=markdown, markdown=True)
    except Exception as exc:
        _log.warning("xmtp post failed for %s: %s", subdomain, exc)
