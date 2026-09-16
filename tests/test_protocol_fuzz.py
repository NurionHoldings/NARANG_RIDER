import base64
import json
import os

import pytest

from narang_rider.protocol_fuzz import (
    MAX_BODY_BYTES,
    ProtocolError,
    parse_headers,
    parse_import_row,
    parse_json_body,
    parse_jwt_envelope,
    parse_opaque_token,
    parse_partner_envelope,
    parse_request_target,
    parse_webhook,
    public_error,
    run_deterministic_fuzz,
)


def b64(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def valid_jwt(alg="RS256"):
    size = 256 if alg == "RS256" else 64
    header = b64({"alg": alg, "kid": "key-0001"})
    claims = b64({"iss": "issuer-01", "aud": "narang-01", "sub": "subject-01", "exp": 2_000_000_000})
    signature = base64.urlsafe_b64encode(b"x" * size).rstrip(b"=").decode()
    return f"{header}.{claims}.{signature}"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"a":1,"a":2}', "json.duplicate_key"),
        (b'{"n":NaN}', "json.number.non_finite"),
        (b'{"n":Infinity}', "json.number.non_finite"),
        (b"\xff", "body.invalid_utf8"),
        (b"{", "json.invalid"),
        (b"[" + b"1," * 256 + b"1]", "json.collection.too_large"),
        (b"[" * 14 + b"0" + b"]" * 14, "json.too_deep"),
        (b'"' + b"x" * 4097 + b'"', "json.string.invalid"),
    ],
)
def test_json_rejects_ambiguous_or_excessive_input(raw, code):
    with pytest.raises(ProtocolError) as caught:
        parse_json_body(raw)
    assert caught.value.code == code
    response = public_error(caught.value)
    assert set(response) == {"status", "error"}
    assert len(str(response)) < 128


def test_json_hard_byte_limit():
    with pytest.raises(ProtocolError) as caught:
        parse_json_body(b" " * (MAX_BODY_BYTES + 1))
    assert caught.value.status == 413


@pytest.mark.parametrize(
    "headers",
    [
        [(b"Authorization", b"Bearer one"), (b"authorization", b"Bearer two")],
        [(b"Content-Length", b"1"), (b"content-length", b"1")],
        [(b"X-Branch-Id", b"branch-01"), (b"x-branch-id", b"branch-01")],
        [(b"x-test", b"same"), (b"x-test", b"different")],
        [(b"x-\xff", b"value")],
        [(b"x-test", b"value\r\ninjected: yes")],
    ],
)
def test_headers_reject_duplicate_confusable_or_control_input(headers):
    with pytest.raises(ProtocolError):
        parse_headers(headers)


def test_identical_noncritical_header_is_deterministic():
    assert parse_headers([(b"x-test", b"same"), (b"x-test", b"same")]) == {"x-test": "same"}


@pytest.mark.parametrize("alg", ["none", "HS256", "RS512"])
def test_jwt_rejects_algorithm_confusion(alg):
    with pytest.raises(ProtocolError) as caught:
        parse_jwt_envelope(valid_jwt(alg))
    assert caught.value.code == "jwt.algorithm_rejected"


def test_jwt_accepts_only_structurally_bounded_asymmetric_envelope():
    header, claims, signature = parse_jwt_envelope(valid_jwt())
    assert header["alg"] == "RS256"
    assert claims["sub"] == "subject-01"
    assert len(signature) == 256


@pytest.mark.parametrize(
    "mutation",
    [
        lambda token: token + ".extra",
        lambda token: token.rsplit(".", 1)[0] + ".AA",
        lambda token: "é" + token,
        lambda token: token.replace(".", "=", 1),
    ],
)
def test_jwt_rejects_malformed_or_wrong_length_signature(mutation):
    with pytest.raises(ProtocolError):
        parse_jwt_envelope(mutation(valid_jwt()))


def test_webhook_rejects_duplicate_security_headers():
    headers = [
        (b"x-webhook-signature", b"sha256=" + b"a" * 64),
        (b"x-webhook-signature", b"sha256=" + b"b" * 64),
        (b"x-webhook-timestamp", b"1000"),
    ]
    with pytest.raises(ProtocolError):
        parse_webhook(headers, b'{"event_id":"event-0001"}', now=1000)


@pytest.mark.parametrize("timestamp", [b"-1", b"1e3", b"9999999999999", b"600"])
def test_webhook_rejects_invalid_or_expired_timestamp(timestamp):
    headers = [
        (b"x-webhook-signature", b"sha256=" + b"a" * 64),
        (b"x-webhook-timestamp", timestamp),
    ]
    with pytest.raises(ProtocolError):
        parse_webhook(headers, b'{"event_id":"event-0001"}', now=1000)


@pytest.mark.parametrize(
    "target",
    [
        b"/orders/../admin",
        b"/orders/%2Fadmin",
        b"/orders?branch_id=branch-01&branch_id=branch-02",
        b"//external.example/orders",
        b"/orders\\admin",
        b"/orders?bad",
    ],
)
def test_request_target_rejects_normalization_and_duplicate_ambiguity(target):
    with pytest.raises(ProtocolError):
        parse_request_target(target)


def test_request_target_accepts_canonical_path_and_query():
    path, query = parse_request_target(b"/v1/orders?limit=20&status=open")
    assert path == "/v1/orders"
    assert query == (("limit", "20"), ("status", "open"))


@pytest.mark.parametrize(
    "value",
    ["short", "nonce-0001\n", "ｎonce-0001", " nonce-0001", "nonce/0001"],
)
def test_security_tokens_are_ascii_and_grammar_bound(value):
    with pytest.raises(ProtocolError):
        parse_opaque_token(value, kind="nonce")


@pytest.mark.parametrize(
    "body",
    [
        b'{"event_id":"event-001","order_id":"order-001","branch_id":"branch-01","sequence":-1,"status":"picked-up"}',
        b'{"event_id":"event-001","order_id":"order-001","branch_id":"branch-01","sequence":1e100,"status":"picked-up"}',
        b'{"event_id":"event-001","order_id":"order-001","branch_id":"branch-01","sequence":1,"status":"picked-up","role":"admin"}',
        b'{"event_id":"event-001","event_id":"event-002","order_id":"order-001","branch_id":"branch-01","sequence":1,"status":"picked-up"}',
    ],
)
def test_partner_envelope_rejects_overflow_extension_and_duplicates(body):
    with pytest.raises(ProtocolError):
        parse_partner_envelope(body)


@pytest.mark.parametrize("field", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "a\nb", "a\x00b"])
def test_import_fields_reject_formula_and_record_injection(field):
    with pytest.raises(ProtocolError):
        parse_import_row([field])


def test_deterministic_fuzz_never_raises_uncaught_or_leaks_input():
    profile = os.getenv("NARANG_FUZZ_PROFILE", "ci")
    count = 20_000 if profile == "large" else 1_000
    result = run_deterministic_fuzz(seed=0x4E415241, count=count)
    assert result.failures == ()
    assert result.accepted + result.rejected == count
    assert result.rejected > 0


def test_fuzz_is_reproducible():
    assert run_deterministic_fuzz(51051, 250) == run_deterministic_fuzz(51051, 250)


def test_public_error_is_bounded_and_contains_no_exception_detail():
    error = ProtocolError("x" * 1_000, 422)
    assert public_error(error) == {"status": 422, "error": "x" * 64}
