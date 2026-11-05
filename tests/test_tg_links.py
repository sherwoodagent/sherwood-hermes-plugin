"""Link signing vectors (mirrored in docs/telegram.md for the app lane)."""
from __future__ import annotations

import hashlib
import hmac

import pytest

from sherwood_monitor.telegram.links import (
    build_link,
    canonical_address,
    decode_start_payload,
    encode_start_payload,
    build_sign_link,
    fund_link_message,
    fund_page_url,
    sign_link,
    sign_link_message,
    verify_link_signature,
    vote_link_message,
)

SECRET = "sherwood-test-vector-secret"  # test vector only, never a real secret
VAULT = "0x1111111111111111111111111111111111111111"
GOV = "0x2222222222222222222222222222222222222222"
TG = 123456789
TS = 1700000000

# Every signed message starts with the link kind (review #28 F5): a signature is valid for one route only.
V1_MSG = "deposit|0x1111111111111111111111111111111111111111|123456789|1700000000"
V1_SIG = "c971d2b7bc056d25899ac29ac05c9ddec157cbde6188398fb91b0c51bc8027d8"
V2_MSG = "vote|0x2222222222222222222222222222222222222222:42|123456789|1700000000"
V2_SIG = "fa036063f49872395b47d5474563e61999f6ef5a202a263a08c460770dc07aa9"
V3_MSG = "deposit|0xabcdefabcdefabcdefabcdefabcdefabcdefabcd|987654321|1762300800"
V3_SIG = "2da75792cd834a2b9bc9316e931008238928193e51268a8c43cbaedbd28e2c94"
JOIN_SIG = "515a3da455c6457e9e91d7f51c1e3222eb9d65bc93cfbdde88f1630acd62a209"
WITHDRAW_SIG = "903f69982ea0aef72e435801a4cdd9b1b9cd9a81a38a39b5841bc716ccc7bb9d"
V4_BODY = "eyJjaGFpbklkIjo0NjYzfQ"  # base64url('{"chainId":4663}')
V4_MSG = "sign|0x1111111111111111111111111111111111111111|approve-depositor|eyJjaGFpbklkIjo0NjYzfQ|123456789|1700000000"
V4_SIG = "452ba6c32a0dd01eee85cb04f3b59f587e3c12be41d6e85264cc46e3f6056f0a"


def test_vector_fund_message_and_sig():
    assert fund_link_message("deposit", VAULT, TG, TS) == V1_MSG
    assert sign_link(SECRET, V1_MSG) == V1_SIG
    # independent check against the stdlib
    assert hmac.new(SECRET.encode(), V1_MSG.encode(), hashlib.sha256).hexdigest() == V1_SIG


def test_vector_vote_message_and_sig():
    assert vote_link_message(GOV, 42, TG, TS) == V2_MSG
    assert sign_link(SECRET, V2_MSG) == V2_SIG


def test_vector_mixed_case_address_is_lowercased():
    msg = fund_link_message("deposit", "0xAbCdEfAbCdEfAbCdEfAbCdEfAbCdEfAbCdEfAbCd", 987654321, 1762300800)
    assert msg == V3_MSG
    assert sign_link(SECRET, msg) == V3_SIG


def test_build_deposit_link_exact():
    url = build_link("deposit", tg_user_id=TG, secret=SECRET, fund=VAULT, ts=TS)
    assert url == (
        "https://app.sherwood.sh/tg/deposit?fund=0x1111111111111111111111111111111111111111"
        f"&tg=123456789&ts=1700000000&sig={V1_SIG}"
    )


@pytest.mark.parametrize("kind,sig", [("join", JOIN_SIG), ("deposit", V1_SIG), ("withdraw", WITHDRAW_SIG)])
def test_each_fund_kind_has_its_own_signature(kind, sig):
    url = build_link(kind, tg_user_id=TG, secret=SECRET, fund=VAULT.upper().replace("0X", "0x"), ts=TS,
                     base_url="https://example.test/")
    assert url.startswith(f"https://example.test/tg/{kind}?fund={VAULT}&")
    assert url.endswith(f"&sig={sig}")


def test_a_join_link_cannot_be_replayed_as_a_withdraw_link():
    """Review #28 F5: the kind is part of the signed message."""
    join_sig = build_link("join", tg_user_id=TG, secret=SECRET, fund=VAULT, ts=TS).split("sig=")[1]
    assert join_sig == JOIN_SIG
    assert not verify_link_signature(SECRET, fund_link_message("withdraw", VAULT, TG, TS), join_sig, ts=TS, now=TS)
    assert verify_link_signature(SECRET, fund_link_message("join", VAULT, TG, TS), join_sig, ts=TS, now=TS)
    assert len({JOIN_SIG, V1_SIG, WITHDRAW_SIG}) == 3
    with pytest.raises(ValueError):
        fund_link_message("vote", VAULT, TG, TS)


def test_vector_sign_link():
    assert sign_link_message(VAULT, "approve-depositor", V4_BODY, TG, TS) == V4_MSG
    assert sign_link(SECRET, V4_MSG) == V4_SIG
    url = build_sign_link(vault=VAULT, action="approve-depositor", body={"chainId": 4663}, tg_user_id=TG,
                          secret=SECRET, ts=TS)
    assert url == (
        f"https://app.sherwood.sh/tg/sign?fund={VAULT}&action=approve-depositor&body={V4_BODY}"
        f"&tg=123456789&ts=1700000000&sig={V4_SIG}"
    )


def test_build_vote_link_exact():
    url = build_link("vote", tg_user_id=TG, secret=SECRET, governor=GOV, proposal_id=42, ts=TS)
    assert url == (
        "https://app.sherwood.sh/tg/vote?proposal=0x2222222222222222222222222222222222222222:42"
        f"&tg=123456789&ts=1700000000&sig={V2_SIG}"
    )


def test_public_fund_page():
    assert fund_page_url(VAULT.upper().replace("0X", "0x")) == f"https://app.sherwood.sh/tg/fund/{VAULT}"


def test_bad_inputs_rejected():
    with pytest.raises(ValueError):
        canonical_address("0x123")
    with pytest.raises(ValueError):
        build_link("deposit", tg_user_id=TG, secret=SECRET, ts=TS)  # no fund
    with pytest.raises(ValueError):
        build_link("vote", tg_user_id=TG, secret=SECRET, governor=GOV, ts=TS)  # no id
    with pytest.raises(ValueError):
        build_link("rugpull", tg_user_id=TG, secret=SECRET, fund=VAULT, ts=TS)
    with pytest.raises(ValueError):
        build_link("deposit", tg_user_id=TG, secret="", fund=VAULT, ts=TS)
    with pytest.raises(ValueError):
        vote_link_message(GOV, "4|2", TG, TS)  # would break the message framing


def test_reference_verifier():
    assert verify_link_signature(SECRET, V1_MSG, V1_SIG, ts=TS, now=TS + 60)
    assert verify_link_signature(SECRET, V1_MSG, V1_SIG.upper(), ts=TS, now=TS + 60)
    assert not verify_link_signature(SECRET, V1_MSG, V1_SIG, ts=TS, now=TS + 3600)  # stale
    assert not verify_link_signature(SECRET, V1_MSG, V2_SIG, ts=TS, now=TS)
    assert not verify_link_signature("other", V1_MSG, V1_SIG, ts=TS, now=TS)


def test_start_payload_roundtrip_and_limits():
    p = encode_start_payload("deposit", VAULT)
    assert p == "sfd_1111111111111111111111111111111111111111"
    assert decode_start_payload(p) == ("deposit", VAULT, None)
    v = encode_start_payload("vote", VAULT, 42)
    assert v == "sfv_1111111111111111111111111111111111111111_42"
    assert decode_start_payload(v) == ("vote", VAULT, "42")
    assert len(encode_start_payload("vote", VAULT, "9" * 19)) == 64
    with pytest.raises(ValueError):
        encode_start_payload("vote", VAULT, "9" * 20)
    assert decode_start_payload("hello") is None
    assert decode_start_payload("sfz_" + "1" * 40) is None
