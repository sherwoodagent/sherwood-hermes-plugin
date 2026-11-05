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
    fund_link_message,
    fund_page_url,
    sign_link,
    verify_link_signature,
    vote_link_message,
)

SECRET = "sherwood-test-vector-secret"  # test vector only, never a real secret
VAULT = "0x1111111111111111111111111111111111111111"
GOV = "0x2222222222222222222222222222222222222222"
TG = 123456789
TS = 1700000000

V1_MSG = "0x1111111111111111111111111111111111111111|123456789|1700000000"
V1_SIG = "db93ae34690df02d34a6394373e409cdc8683b891431c10b59a42a837b1a680e"
V2_MSG = "0x2222222222222222222222222222222222222222:42|123456789|1700000000"
V2_SIG = "7f612dafad917a80a2d0c75a571fb9c8e7d24a770336d17848ac41b020da854a"
V3_MSG = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd|987654321|1762300800"
V3_SIG = "ae3700a91ef375f41a5fbfa4635c2ff3dec4f617b31e0f01ae599baa5cba67f7"


def test_vector_fund_message_and_sig():
    assert fund_link_message(VAULT, TG, TS) == V1_MSG
    assert sign_link(SECRET, V1_MSG) == V1_SIG
    # independent check against the stdlib
    assert hmac.new(SECRET.encode(), V1_MSG.encode(), hashlib.sha256).hexdigest() == V1_SIG


def test_vector_vote_message_and_sig():
    assert vote_link_message(GOV, 42, TG, TS) == V2_MSG
    assert sign_link(SECRET, V2_MSG) == V2_SIG


def test_vector_mixed_case_address_is_lowercased():
    msg = fund_link_message("0xAbCdEfAbCdEfAbCdEfAbCdEfAbCdEfAbCdEfAbCd", 987654321, 1762300800)
    assert msg == V3_MSG
    assert sign_link(SECRET, msg) == V3_SIG


def test_build_deposit_link_exact():
    url = build_link("deposit", tg_user_id=TG, secret=SECRET, fund=VAULT, ts=TS)
    assert url == (
        "https://app.sherwood.sh/tg/deposit?fund=0x1111111111111111111111111111111111111111"
        f"&tg=123456789&ts=1700000000&sig={V1_SIG}"
    )


@pytest.mark.parametrize("kind", ["join", "deposit", "withdraw"])
def test_fund_kinds_share_one_signature(kind):
    url = build_link(kind, tg_user_id=TG, secret=SECRET, fund=VAULT.upper().replace("0X", "0x"), ts=TS,
                     base_url="https://example.test/")
    assert url.startswith(f"https://example.test/tg/{kind}?fund={VAULT}&")
    assert url.endswith(f"&sig={V1_SIG}")


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
