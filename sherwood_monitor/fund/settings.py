"""Fund settings for this Hermes process.

Resolution order for each key (first non-empty wins):
1. Hermes plugin settings (`plugins.entries.sherwood-monitor.settings.<key>`,
   declared in plugin.yaml `config_schema`), read with `ctx.get_config`.
2. The `fund:` section of `~/.hermes/plugins/sherwood-monitor/config.yaml`.
3. Environment, for the two URL keys (`TX_BUILDER_URL`, `APP_BASE_URL`),
   through `secrets.get_secret` so Hermes' secret scope is respected.
4. The defaults below.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

from . import secrets
from .txbuilder import DEFAULT_TX_BUILDER_URL

_log = logging.getLogger(__name__)

DEFAULT_APP_BASE_URL = "https://sherwood.sh"


@dataclass(frozen=True)
class FundSettings:
    agent_name: str = ""  # this Hermes's agent name inside funds
    agent_identity_id: str = ""  # its ERC-8004 token id
    agent_address: str = ""  # its scoped wallet
    owner_telegram_id: str = ""  # the human who runs this Hermes
    default_fund: str = ""
    tx_builder_url: str = DEFAULT_TX_BUILDER_URL
    app_base_url: str = DEFAULT_APP_BASE_URL
    fund_state_backend: str = "local"  # local | http
    fund_state_dir: str = ""  # local backend root; "" = ~/.hermes/plugins/sherwood-monitor/funds
    fund_state_url: str = ""  # http backend base URL (stub for now)


_ENV_FALLBACK = {
    "tx_builder_url": secrets.TX_BUILDER_URL,
    "app_base_url": secrets.APP_BASE_URL,
}


def _yaml_section(cfg_path: Path | None) -> dict[str, Any]:
    if cfg_path is None or not cfg_path.exists():
        return {}
    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except (yaml.YAMLError, OSError) as exc:
        _log.warning("fund settings: cannot read %s: %s", cfg_path, exc)
        return {}
    section = raw.get("fund") if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def _ctx_value(ctx: Any, key: str) -> Any:
    getter = getattr(ctx, "get_config", None) if ctx is not None else None
    if not callable(getter):
        return None
    try:
        value = getter(key, None)
    except Exception:  # a host without plugin settings, or a rejected key
        return None
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def load_fund_settings(cfg_path: Path | None = None, ctx: Any = None) -> FundSettings:
    section = _yaml_section(cfg_path)
    values: dict[str, Any] = {}
    for f in fields(FundSettings):
        value = _ctx_value(ctx, f.name)
        if value in (None, ""):
            value = section.get(f.name)
        if value in (None, "") and f.name in _ENV_FALLBACK:
            value = secrets.get_secret(_ENV_FALLBACK[f.name])
        if value in (None, ""):
            continue
        values[f.name] = str(value).strip()
    return FundSettings(**values)
