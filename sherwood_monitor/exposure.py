"""Cross-fund exposure aggregation and concentration alerts."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .state_fetcher import fetch_vault_info

_log = logging.getLogger(__name__)

DEFAULT_CONCENTRATION_PCT = 30.0


@dataclass(frozen=True)
class ExposureReport:
    total_aum_usd: float
    by_protocol: dict[str, float]
    concentration_pct: dict[str, float]
    per_fund: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class ConcentrationAlert:
    protocol: str
    pct: float
    funds_exposed: list[str]


async def aggregate_exposure(
    sherwood_bin: str, funds: list[str]
) -> ExposureReport:
    total_aum = 0.0
    by_protocol: dict[str, float] = {}
    per_fund: dict[str, dict[str, float]] = {}

    results = await asyncio.gather(
        *(fetch_vault_info(sherwood_bin, s) for s in funds),
        return_exceptions=True,
    )
    for sub, info in zip(funds, results):
        if isinstance(info, Exception) or not info:
            _log.warning("exposure: skipping %s (%s)", sub, info)
            continue
        total_aum += float(info.get("aumUsd", 0))
        positions = info.get("positions", []) or []
        per = per_fund.setdefault(sub, {})
        for p in positions:
            proto = str(p.get("protocol", "")).lower()
            if not proto:
                continue
            usd = float(p.get("usd", 0))
            by_protocol[proto] = by_protocol.get(proto, 0.0) + usd
            per[proto] = per.get(proto, 0.0) + usd

    concentration = {}
    if total_aum > 0:
        for proto, usd in by_protocol.items():
            concentration[proto] = round(usd / total_aum * 100, 2)

    return ExposureReport(
        total_aum_usd=total_aum,
        by_protocol=by_protocol,
        concentration_pct=concentration,
        per_fund=per_fund,
    )


def check_concentration(
    report: ExposureReport, threshold_pct: float = DEFAULT_CONCENTRATION_PCT
) -> list[ConcentrationAlert]:
    alerts: list[ConcentrationAlert] = []
    for proto, pct in report.concentration_pct.items():
        if pct >= threshold_pct:
            exposed = [sub for sub, per in report.per_fund.items() if proto in per]
            alerts.append(
                ConcentrationAlert(protocol=proto, pct=pct, funds_exposed=exposed)
            )
    return alerts
