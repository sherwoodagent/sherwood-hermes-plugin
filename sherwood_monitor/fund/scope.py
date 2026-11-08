"""What this fund can actually propose, from its configured chain and the CLI's template list.

Skill files that describe another network stay in the pack (they are reference for a chain
where that template exists). They are labeled out of network only for the fund being
answered, and only when that fund's chain is one they say they are not on, or when
``sherwood strategy list`` did not print them as available. Nothing here bans a template
inside the CLI itself.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .models import CHAIN_ID_ROBINHOOD, FundState
from .store import FundNotFound

_log = logging.getLogger(__name__)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TEMPLATE_LINE = re.compile(r"^\s*(.+?)\s+\(([a-z0-9-]+)\)\s*$")
_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills" / "sherwood-agent" / "skills"
_DISCOVERY: dict[str, dict[str, Any]] = {}

Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def _strip(text: str) -> str:
    return _ANSI.sub("", text)


def parse_strategy_list(text: str) -> dict[str, Any]:
    """Split ``sherwood strategy list`` into available and unavailable template keys."""
    available: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []
    network = ""
    bucket = available
    for raw in _strip(text).splitlines():
        line = raw.rstrip()
        if "Strategy Templates" in line and "(" in line and ")" in line:
            network = line[line.rfind("(") + 1 : line.rfind(")")]
        if "Not available" in line:
            bucket = unavailable
            continue
        match = _TEMPLATE_LINE.match(line.strip())
        if not match:
            continue
        name, key = match.group(1).strip(), match.group(2)
        if name.lower().startswith("template"):
            continue
        bucket.append({"name": name, "key": key})
    if not available and not unavailable:
        raise ValueError("no templates in strategy list output")
    return {"ok": True, "network": network, "available": available, "unavailable": unavailable, "source": "sherwood strategy list"}


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    from ..childenv import child_env

    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=child_env())


def discover_strategy_templates(
    sherwood_bin: str = "sherwood", *, timeout: float = 3.0, runner: Runner | None = None, refresh: bool = False
) -> dict[str, Any]:
    """Read-only ``sherwood strategy list``. Cached per binary. Failure is ``ok: false``, never a guessed list."""
    key = sherwood_bin or "sherwood"
    if not refresh and key in _DISCOVERY:
        return _DISCOVERY[key]
    run = runner or _run
    if runner is None and shutil.which(key) is None:
        result = {"ok": False, "error": f"`{key}` is not on PATH", "available": [], "unavailable": []}
        _DISCOVERY[key] = result
        return result
    try:
        proc = run([key, "strategy", "list"], timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {"ok": False, "error": f"`{key} strategy list` failed ({exc.__class__.__name__})", "available": [], "unavailable": []}
        _DISCOVERY[key] = result
        return result
    if proc.returncode != 0:
        result = {
            "ok": False,
            "error": f"`{key} strategy list` exited {proc.returncode}",
            "available": [],
            "unavailable": [],
        }
        _DISCOVERY[key] = result
        return result
    try:
        result = parse_strategy_list(proc.stdout or "")
    except ValueError as exc:
        result = {"ok": False, "error": str(exc), "available": [], "unavailable": []}
    _DISCOVERY[key] = result
    return result


def reference_skills(chain_id: int) -> list[dict[str, str]]:
    """Bundled skills whose own text says they are not available on ``chain_id``.

    Only Robinhood Chain (4663) is named that way in the pack today. Another chain gets
    no such label from this scan: the CLI list is what says what is deployed there.
    """
    if chain_id != CHAIN_ID_ROBINHOOD:
        return []
    needle = f"Not available on Robinhood Chain ({chain_id})"
    found: list[dict[str, str]] = []
    if not _SKILLS_DIR.is_dir():
        return found
    for path in sorted(_SKILLS_DIR.rglob("SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle not in text[:2000]:
            continue
        name = path.parent.name
        for line in text.splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip("'\"")
                break
        found.append({"name": name, "path": str(path.relative_to(_SKILLS_DIR.parent.parent))})
    return found


def fund_scope(state: FundState, discovered: dict[str, Any] | None = None) -> dict[str, Any]:
    """Facts for one configured fund. No yields. No claim that a skill is executable."""
    fund = state.fund
    discovered = discovered if discovered is not None else {"ok": False, "error": "not checked", "available": [], "unavailable": []}
    available = [row["key"] for row in discovered.get("available") or []] if discovered.get("ok") else []
    unavailable = [row["key"] for row in discovered.get("unavailable") or []] if discovered.get("ok") else []
    skills = reference_skills(fund.chain_id)
    if discovered.get("ok"):
        skills = [s for s in skills if s["name"] not in available]
    chain_name = "Robinhood Chain" if fund.chain_id == CHAIN_ID_ROBINHOOD else f"chain {fund.chain_id}"
    return {
        "fund": fund.name,
        "chain_id": fund.chain_id,
        "chain": chain_name,
        "robinhood_brokerage": False,
        "asset": fund.asset,
        "vault": fund.vault,
        "vault_set": bool(fund.vault),
        "strategies": {
            "discovered": bool(discovered.get("ok")),
            "source": discovered.get("source") or discovered.get("error") or "",
            "network": discovered.get("network") or "",
            "available": available,
            "unavailable": unavailable,
        },
        "reference_only_skills": [s["name"] for s in skills],
        "generic_call": (
            "A Generic Call plan can express calls on this fund's chain after the host ships it. "
            "It does not make a protocol from another chain executable here, and it is not execution acceptance."
        ),
        "yields": "Do not state an APY, yield, or return unless a tool result in this turn gave that number for this vault.",
        "ideas_vs_available": (
            "A skill file or a general idea is not a strategy wired up on this fund. "
            "Name a template as available only when it is in strategies.available from `sherwood strategy list` "
            "for this process. Otherwise call it a general idea, or say the list could not be read."
        ),
    }


def scope_text(scope: dict[str, Any]) -> str:
    strategies = scope.get("strategies") or {}
    if strategies.get("discovered"):
        listed = ", ".join(strategies.get("available") or []) or "(none printed)"
        discovery = f"`sherwood strategy list` on {strategies.get('network') or 'the CLI network'}: {listed}."
    else:
        discovery = (
            "Strategy list was not read"
            + (f" ({strategies.get('source')})." if strategies.get("source") else ".")
            + " Do not invent which templates are deployed."
        )
    reference = ", ".join(scope.get("reference_only_skills") or [])
    reference_line = (
        f"Out of network for this fund, from those skills' own text (not a ban on other chains' tooling): {reference}."
        if reference
        else "No bundled skill labels itself unavailable on this fund's chain. Still do not assume one is executable here."
    )
    vault = scope.get("vault") or "not set"
    chain_note = ""
    if scope.get("chain_id") == CHAIN_ID_ROBINHOOD:
        available = set(strategies.get("available") or [])
        labels = (
            ("moonwell-supply", "Moonwell"),
            ("levered-swap", "levered swap"),
            ("memecoin-alpha", "memecoin alpha"),
            ("aerodrome-lp", "Aerodrome"),
            ("venice-inference", "Venice"),
            ("hyperliquid-perp", "Hyperliquid"),
            ("hyperliquid-grid", "Hyperliquid grid"),
        )
        # Named only when this fund's list does not show them. stETH/rETH, Base and Solana are not
        # templates on Robinhood Chain; they stay labeled as other-network examples for this fund.
        names = [label for key, label in labels if key not in available] + ["stETH/rETH", "Base", "Solana"]
        listed = ", ".join(names)
        if strategies.get("discovered"):
            chain_note = (
                f"Other-network examples for this Robinhood Chain fund (not in strategies.available): {listed}. "
                "Do not quote a yield for them.\n"
            )
        else:
            chain_note = (
                f"On this Robinhood Chain fund, {listed} are other-network examples, not strategies wired up here, "
                "unless `sherwood strategy list` prints the matching template as available. Do not quote a yield for them.\n"
            )
    return (
        "<sherwood-fund-scope>\n"
        f"Active fund {scope.get('fund')} on {scope.get('chain')} (chain id {scope.get('chain_id')}), "
        f"asset {scope.get('asset')}, vault {vault}. This chain is not the Robinhood brokerage.\n"
        f"{chain_note}"
        f"{discovery}\n"
        f"{reference_line}\n"
        f"{scope.get('ideas_vs_available')}\n"
        f"{scope.get('generic_call')}\n"
        f"{scope.get('yields')}\n"
        "</sherwood-fund-scope>"
    )


def make_fund_scope_source(rt: Any) -> Callable[..., dict[str, str] | None]:
    """pre_llm context: the configured default fund, or an explicit 'do not claim' line when there is none."""

    def source(**_: Any) -> dict[str, str] | None:
        name = (getattr(rt.settings, "default_fund", "") or "").strip()
        if not name:
            return {
                "context": (
                    "<sherwood-fund-scope>\n"
                    "No default fund is configured. Do not say a strategy, protocol, or yield is wired up. "
                    "Call fund_status for the fund being discussed and answer from that fund's chain, vault, "
                    "and strategies list.\n"
                    "</sherwood-fund-scope>"
                )
            }
        try:
            state = rt.service.load(name)
        except FundNotFound:
            return {
                "context": (
                    "<sherwood-fund-scope>\n"
                    f"Default fund {name} has no local record. Do not invent its chain or its strategies.\n"
                    "</sherwood-fund-scope>"
                )
            }
        except Exception as exc:
            _log.warning("fund scope skipped: %s", exc)
            return None
        try:
            discovered = discover_strategy_templates(getattr(rt, "sherwood_bin", "sherwood"))
        except Exception as exc:
            _log.warning("strategy list skipped: %s", exc)
            discovered = {"ok": False, "error": exc.__class__.__name__, "available": [], "unavailable": []}
        return {"context": scope_text(fund_scope(state, discovered))}

    return source
