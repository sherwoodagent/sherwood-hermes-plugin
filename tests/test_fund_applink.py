"""fund/applink.py: GET ${APP_BASE_URL}/api/tg/link (sherwood-app #152). Mocked, no network."""
from __future__ import annotations

from types import SimpleNamespace

from sherwood_monitor.fund.applink import AppLinkClient, ensure_member_address
from sherwood_monitor.fund.models import Agent
from sherwood_monitor.fund.service import FundService
from sherwood_monitor.fund.store import LocalJsonBackend
from sherwood_monitor.fund.applink import _http_get_json as REAL_GET  # the real transport (conftest stubs it)
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


def client(rec, secret="tok3n", base="https://app.example/"):
    return AppLinkClient(base, allowed_hosts="app.example", token=lambda: secret, get_json=rec)


def test_lookup_sends_the_bearer_secret_and_reads_the_address():
    rec = Recorder(payload={"success": True, "data": {"address": MEMBER_ADDR}})
    assert client(rec).linked_address(VAULT, 77) == MEMBER_ADDR
    url, headers = rec.calls[0]
    assert url == f"https://app.example/api/tg/link?fund={VAULT.lower()}&tg=77"
    assert headers == {"authorization": "Bearer tok3n"}
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


# -- review #28 F2 (probe2) ------------------------------------------------------------------------------
def test_only_https_on_an_allowlisted_host():
    rec = Recorder(payload={"address": MEMBER_ADDR})
    for base in ("http://app.example", "https://evil.example", "https://app.example:8443", "https://u:p@app.example",
                 "ftp://app.example"):
        assert client(rec, base=base).linked_address(VAULT, 77) is None, base
    assert rec.calls == []  # the token never left
    assert AppLinkClient("https://app.sherwood.sh", token=lambda: "t").base_ok()  # default allowlist
    assert not AppLinkClient("https://app.example", token=lambda: "t").base_ok()


def test_any_transport_error_means_not_linked():
    import http.client
    import ssl

    for exc in (http.client.BadStatusLine("GARBAGE"), http.client.RemoteDisconnected("x"), ssl.SSLError("bad"),
                ValueError("json"), RuntimeError("anything")):
        assert client(Recorder(exc=exc)).linked_address(VAULT, 77) is None


def test_redirects_are_refused_and_the_bearer_stays_home():
    """A 302 from the app is not followed: the redirect target never sees the Authorization header."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Evil(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    evil = HTTPServer(("127.0.0.1", 0), Evil)
    threading.Thread(target=evil.serve_forever, daemon=True).start()

    class App(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{evil.server_address[1]}/steal")
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    app = HTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=app.serve_forever, daemon=True).start()
    try:
        status, payload = REAL_GET(f"http://127.0.0.1:{app.server_address[1]}/api/tg/link", {"authorization": "Bearer t"}, 3)
    finally:
        app.shutdown()
        evil.shutdown()
    assert status == 302 and payload == {} and "auth" not in seen

