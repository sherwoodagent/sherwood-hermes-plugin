"""fund/applink.py: GET ${APP_BASE_URL}/api/tg/link (sherwood-app #152). Mocked, no network."""
from __future__ import annotations

from types import SimpleNamespace

from sherwood_monitor.fund.applink import AppLinkClient, ensure_member_address
from sherwood_monitor.fund.models import Agent
from sherwood_monitor.fund.service import FundService
from sherwood_monitor.fund.store import LocalJsonBackend
from tests.fund_helpers import MEMBER_ADDR, VAULT, Clock, fund


class Recorder:
    def __init__(self, status=200, payload=None, exc=None):
        self.status, self.payload, self.exc = status, payload, exc
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        if self.exc:
            raise self.exc
        return self.status, self.payload


def client(rec, secret="s3cret"):
    return AppLinkClient("https://app.example/", secret=lambda: secret, get_json=rec)


def test_lookup_sends_the_bearer_secret_and_reads_the_address():
    rec = Recorder(payload={"success": True, "data": {"address": MEMBER_ADDR}})
    assert client(rec).linked_address(VAULT, 77) == MEMBER_ADDR
    url, headers = rec.calls[0]
    assert url == f"https://app.example/api/tg/link?fund={VAULT.lower()}&tg=77"
    assert headers == {"authorization": "Bearer s3cret"}
    assert client(Recorder(payload={"address": MEMBER_ADDR})).linked_address(VAULT, "77") == MEMBER_ADDR


def test_lookup_never_raises_and_means_not_linked():
    assert client(Recorder(status=404, payload={})).linked_address(VAULT, 77) is None
    assert client(Recorder(payload={"data": {"address": "nope"}})).linked_address(VAULT, 77) is None
    assert client(Recorder(exc=OSError("down"))).linked_address(VAULT, 77) is None
    no_secret = Recorder(payload={"address": MEMBER_ADDR})
    assert client(no_secret, secret=None).linked_address(VAULT, 77) is None and not no_secret.calls
    assert client(Recorder()).linked_address("not-a-vault", 77) is None


def test_ensure_member_address_records_the_linked_wallet(tmp_path):
    svc = FundService(LocalJsonBackend(tmp_path), clock=Clock())
    svc.create_fund(fund(), Agent(name="alpha", identity_id="101", address="0x" + "33" * 20))
    svc.request_membership("alpha-fund", "77")
    rec = Recorder(payload={"data": {"address": MEMBER_ADDR}})
    rt = SimpleNamespace(service=svc, applink=client(rec))
    assert ensure_member_address(rt, "alpha-fund", "77") == MEMBER_ADDR
    assert svc.load("alpha-fund").member("77").address == MEMBER_ADDR
    assert ensure_member_address(rt, "alpha-fund", "77") == MEMBER_ADDR and len(rec.calls) == 1  # on record now
    member, body = svc.approve_member("alpha-fund", "alpha", "77")
    assert body["depositor"] == MEMBER_ADDR
    nothing = SimpleNamespace(service=svc, applink=client(Recorder(status=404, payload={})))
    assert ensure_member_address(nothing, "alpha-fund", "78") == ""
