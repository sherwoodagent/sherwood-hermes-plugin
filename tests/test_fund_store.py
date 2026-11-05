"""Fund state store: JSON round-trip, atomic updates, locking, schema guard."""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from sherwood_monitor.fund.models import FundState, Leg, ProposalRecord
from sherwood_monitor.fund.store import (
    FundExists,
    FundNotFound,
    FundStateBackend,
    FundStoreError,
    HttpFundStateBackend,
    LocalJsonBackend,
    SchemaTooNew,
    default_state_dir,
    open_backend,
    validate_fund_name,
)
from tests.fund_helpers import state


def test_models_round_trip_and_ignore_unknown_keys():
    s = state()
    s.proposals.append(ProposalRecord(id="p-1", proposer_agent="beta", thesis="t", legs=[Leg(summary="x", cap="5")], onchain_id=7))
    raw = s.to_dict()
    raw["future_field"] = 1
    raw["fund"]["future"] = "x"
    raw["agents"][0]["future"] = True
    back = FundState.from_dict(json.loads(json.dumps(raw)))
    assert back.to_dict() == s.to_dict()
    assert back.proposal("7") is back.proposal("p-1")
    assert back.proposal("#7").legs[0].cap == "5"


def test_create_load_update(tmp_path: Path):
    be = LocalJsonBackend(tmp_path, clock=lambda: 42.0)
    assert isinstance(be, FundStateBackend)
    be.create(state())
    assert be.exists("alpha-fund") and be.list_funds() == ["alpha-fund"]
    loaded = be.load("alpha-fund")
    assert loaded.revision == 1 and loaded.updated_at == 42.0

    out = be.update("alpha-fund", lambda st: setattr(st.fund, "vault", "0x" + "99" * 20) or "done")
    assert out == "done"
    again = be.load("alpha-fund")
    assert again.fund.vault == "0x" + "99" * 20 and again.revision == 2
    assert (tmp_path / "alpha-fund.json").stat().st_mode & 0o777 == 0o600


def test_create_twice_refused(tmp_path: Path):
    be = LocalJsonBackend(tmp_path)
    be.create(state())
    with pytest.raises(FundExists):
        be.create(state())


def test_missing_fund(tmp_path: Path):
    with pytest.raises(FundNotFound):
        LocalJsonBackend(tmp_path).load("alpha-fund")
    with pytest.raises(FundNotFound):
        LocalJsonBackend(tmp_path).update("alpha-fund", lambda s: None)


def test_failed_mutation_writes_nothing(tmp_path: Path):
    be = LocalJsonBackend(tmp_path)
    be.create(state())
    before = (tmp_path / "alpha-fund.json").read_text()

    def boom(st):
        st.fund.vault = "changed"
        raise RuntimeError("rule failed")

    with pytest.raises(RuntimeError):
        be.update("alpha-fund", boom)
    assert (tmp_path / "alpha-fund.json").read_text() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_schema_too_new_refused(tmp_path: Path):
    be = LocalJsonBackend(tmp_path)
    be.create(state())
    p = tmp_path / "alpha-fund.json"
    raw = json.loads(p.read_text())
    raw["schema_version"] = 99
    p.write_text(json.dumps(raw))
    with pytest.raises(SchemaTooNew):
        be.load("alpha-fund")


def test_corrupt_file_reported(tmp_path: Path):
    (tmp_path / "alpha-fund.json").write_text("{not json")
    with pytest.raises(FundStoreError):
        LocalJsonBackend(tmp_path).load("alpha-fund")


@pytest.mark.parametrize("bad", ["../etc", "Alpha Fund", "", "a/b", "-lead"])
def test_fund_names_are_safe_file_names(bad):
    with pytest.raises(FundStoreError):
        validate_fund_name(bad)


def test_default_dir_and_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SHERWOOD_FUND_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_state_dir() == tmp_path / ".hermes" / "plugins" / "sherwood-monitor" / "funds"
    monkeypatch.setenv("SHERWOOD_FUND_STATE_DIR", str(tmp_path / "x"))
    assert LocalJsonBackend().root == tmp_path / "x"


def test_http_backend_is_a_clear_stub():
    be = open_backend("http", state_url="https://example.invalid/api")
    assert isinstance(be, HttpFundStateBackend)
    with pytest.raises(FundStoreError, match="not available yet"):
        be.load("alpha-fund")
    with pytest.raises(FundStoreError):
        open_backend("carrier-pigeon")


def _bump(root: str, n: int) -> None:
    be = LocalJsonBackend(root)

    def inc(st):
        st.next_member_no += 1

    for _ in range(n):
        be.update("alpha-fund", inc)


def test_concurrent_processes_never_lose_an_update(tmp_path: Path):
    """Host and guest processes share the file; the lock serializes read-modify-write."""
    be = LocalJsonBackend(tmp_path)
    be.create(state())
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_bump, args=(str(tmp_path), 25)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    final = be.load("alpha-fund")
    assert final.next_member_no == 1 + 100
    assert final.revision == 1 + 100
