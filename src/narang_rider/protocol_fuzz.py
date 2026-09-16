"""Bounded protocol parsers and deterministic adversarial corpus runner.

This module intentionally has no network or external-fuzzer dependency.  Every
public parser rejects ambiguous input before authorization or persistence.
"""

from __future__ import annotations

import base64
import json
import math
import posixpath
import random
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

MAX_BODY_BYTES = 64 * 1024
MAX_HEADERS = 64
MAX_HEADER_BYTES = 16 * 1024
MAX_DEPTH = 12
MAX_NODES = 2_000
MAX_COLLECTION = 256
MAX_STRING = 4_096
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
HEADER_NAME_RE = re.compile(r"^[a-z0-9!#$%&'*+.^_`|~-]+$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
CRITICAL_HEADERS = {
    "authorization",
    "content-length",
    "content-type",
    "host",
    "idempotency-key",
    "x-branch-id",
    "x-csrf-token",
    "x-webhook-signature",
    "x-webhook-timestamp",
}
CRITICAL_QUERY = {"branch_id", "grant", "nonce", "token"}


@dataclass(frozen=True)
class ProtocolError(Exception):
    code: str
    status: int = 400

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class FuzzResult:
    seed: int
    cases: int
    accepted: int
    rejected: int
    failures: tuple[str, ...]


def _fail(code: str, status: int = 400) -> None:
    raise ProtocolError(code, status)


def _decode_ascii(raw: bytes, code: str) -> str:
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        _fail(code)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _fail(code)
    return value


def parse_headers(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    if len(raw_headers) > MAX_HEADERS:
        _fail("headers.too_many", 431)
    if sum(len(name) + len(value) for name, value in raw_headers) > MAX_HEADER_BYTES:
        _fail("headers.too_large", 431)
    parsed: dict[str, str] = {}
    for raw_name, raw_value in raw_headers:
        name = _decode_ascii(raw_name, "header.name.invalid").lower()
        value = _decode_ascii(raw_value, "header.value.invalid").strip()
        if not HEADER_NAME_RE.fullmatch(name):
            _fail("header.name.invalid")
        if name in parsed:
            if name in CRITICAL_HEADERS or parsed[name] != value:
                _fail("header.duplicate_ambiguous")
            continue
        parsed[name] = value
    content_length = parsed.get("content-length")
    if content_length is not None:
        if not content_length.isdecimal() or len(content_length) > 8:
            _fail("content_length.invalid")
        if int(content_length) > MAX_BODY_BYTES:
            _fail("body.too_large", 413)
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("json.duplicate_key")
        result[key] = value
    return result


def _validate_json_tree(value: Any) -> Any:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES:
            _fail("json.too_many_nodes", 413)
        if depth > MAX_DEPTH:
            _fail("json.too_deep", 413)
        if isinstance(item, str):
            if len(item) > MAX_STRING or any(
                ord(char) < 0x20 and char not in "\t\n\r" for char in item
            ):
                _fail("json.string.invalid")
        elif isinstance(item, list):
            if len(item) > MAX_COLLECTION:
                _fail("json.collection.too_large", 413)
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            if len(item) > MAX_COLLECTION:
                _fail("json.collection.too_large", 413)
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 128:
                    _fail("json.key.invalid")
                visit(child, depth + 1)
        elif isinstance(item, float) and not math.isfinite(item):
            _fail("json.number.non_finite")

    visit(value, 0)
    return value


def parse_json_body(raw: bytes) -> Any:
    if len(raw) > MAX_BODY_BYTES:
        _fail("body.too_large", 413)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("body.invalid_utf8")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: _fail("json.number.non_finite"),
        )
    except ProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError):
        _fail("json.invalid")
    return _validate_json_tree(value)


def _b64url_json(segment: str, label: str) -> dict[str, Any]:
    if not segment or len(segment) > 8_192 or "=" in segment:
        _fail(f"jwt.{label}.invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", segment):
        _fail(f"jwt.{label}.invalid")
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, base64.binascii.Error):
        _fail(f"jwt.{label}.invalid")
    value = parse_json_body(raw)
    if not isinstance(value, dict):
        _fail(f"jwt.{label}.invalid")
    return value


def parse_jwt_envelope(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    if len(token) > 16_384 or any(ord(char) > 127 for char in token):
        _fail("jwt.invalid", 401)
    parts = token.split(".")
    if len(parts) != 3:
        _fail("jwt.invalid", 401)
    header = _b64url_json(parts[0], "header")
    claims = _b64url_json(parts[1], "claims")
    alg = header.get("alg")
    if alg not in {"RS256", "ES256"} or "crit" in header or "jku" in header:
        _fail("jwt.algorithm_rejected", 401)
    if not isinstance(header.get("kid"), str) or not TOKEN_RE.fullmatch(header["kid"]):
        _fail("jwt.kid.invalid", 401)
    if not parts[2] or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[2]):
        _fail("jwt.signature.invalid", 401)
    try:
        signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    except (ValueError, base64.binascii.Error):
        _fail("jwt.signature.invalid", 401)
    expected = 64 if alg == "ES256" else 256
    if len(signature) != expected:
        _fail("jwt.signature.invalid", 401)
    for claim in ("iss", "aud", "sub", "exp"):
        if claim not in claims:
            _fail("jwt.claims.incomplete", 401)
    if isinstance(claims["exp"], bool) or not isinstance(claims["exp"], int):
        _fail("jwt.exp.invalid", 401)
    return header, claims, signature


def parse_webhook(
    headers: list[tuple[bytes, bytes]], body: bytes, *, now: int
) -> tuple[str, int, dict[str, Any]]:
    parsed = parse_headers(headers)
    signature = parsed.get("x-webhook-signature", "")
    timestamp = parsed.get("x-webhook-timestamp", "")
    if not timestamp.isdecimal() or len(timestamp) > 12:
        _fail("webhook.timestamp.invalid", 401)
    stamp = int(timestamp)
    if abs(now - stamp) > 300:
        _fail("webhook.timestamp.expired", 401)
    if not signature.startswith("sha256=") or not HEX64_RE.fullmatch(signature[7:]):
        _fail("webhook.signature.invalid", 401)
    event = parse_json_body(body)
    if not isinstance(event, dict):
        _fail("webhook.event.invalid")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not TOKEN_RE.fullmatch(event_id):
        _fail("webhook.event_id.invalid")
    return signature, stamp, event


def parse_opaque_token(value: str, *, kind: str) -> str:
    if any(ord(char) > 127 for char in value) or not TOKEN_RE.fullmatch(value):
        _fail(f"{kind}.invalid")
    return value


def parse_request_target(raw: bytes) -> tuple[str, tuple[tuple[str, str], ...]]:
    target = _decode_ascii(raw, "target.invalid")
    if len(target) > 4_096 or "\\" in target or "#" in target:
        _fail("target.invalid")
    split = urlsplit(target)
    if split.scheme or split.netloc or not split.path.startswith("/"):
        _fail("target.invalid")
    try:
        decoded_path = unquote_to_bytes(split.path).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("target.invalid_encoding")
    if "%2f" in split.path.lower() or "%5c" in split.path.lower():
        _fail("target.encoded_separator")
    normalized = posixpath.normpath(decoded_path)
    if decoded_path != normalized and not (
        decoded_path.endswith("/") and decoded_path[:-1] == normalized
    ):
        _fail("target.noncanonical")
    if any(ord(char) < 0x20 for char in decoded_path):
        _fail("target.invalid")
    try:
        query = parse_qsl(
            split.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=64,
        )
    except ValueError:
        _fail("query.invalid")
    seen: dict[str, str] = {}
    for key, value in query:
        if len(key) > 128 or len(value) > 1_024:
            _fail("query.too_large")
        if key in seen and (key in CRITICAL_QUERY or seen[key] != value):
            _fail("query.duplicate_ambiguous")
        seen[key] = value
    return decoded_path, tuple(query)


def parse_partner_envelope(raw: bytes) -> dict[str, Any]:
    value = parse_json_body(raw)
    if not isinstance(value, dict):
        _fail("partner.envelope.invalid")
    allowed = {"event_id", "order_id", "branch_id", "sequence", "status"}
    if set(value) - allowed:
        _fail("partner.envelope.unknown_field")
    for field in ("event_id", "order_id", "branch_id", "status"):
        item = value.get(field)
        if not isinstance(item, str) or not TOKEN_RE.fullmatch(item):
            _fail(f"partner.{field}.invalid")
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence < 2**63:
        _fail("partner.sequence.invalid")
    return value


def parse_import_row(fields: list[str]) -> tuple[str, ...]:
    if len(fields) > 32:
        _fail("import.too_many_fields")
    clean = []
    for field in fields:
        if len(field) > 1_024 or "\x00" in field or "\r" in field or "\n" in field:
            _fail("import.field.invalid")
        if field[:1] in {"=", "+", "-", "@"}:
            _fail("import.formula_rejected")
        clean.append(field)
    return tuple(clean)


def deterministic_cases(seed: int, count: int) -> list[tuple[str, Callable[[], Any]]]:
    rng = random.Random(seed)
    cases: list[tuple[str, Callable[[], Any]]] = []
    atoms = [b"{", b"[]", b'{"a":1,"a":2}', b'{"n":NaN}', b"\xff", b"null"]
    for index in range(count):
        choice = rng.randrange(6)
        if choice == 0:
            raw = rng.choice(atoms)
            cases.append((f"json-{index}", lambda raw=raw: parse_json_body(raw)))
        elif choice == 1:
            size = rng.randrange(1, 80)
            value = bytes(rng.randrange(256) for _ in range(size))
            cases.append(
                (f"header-{index}", lambda value=value: parse_headers([(b"x-test", value)]))
            )
        elif choice == 2:
            token = "".join(chr(rng.randrange(32, 128)) for _ in range(rng.randrange(1, 80)))
            cases.append((f"jwt-{index}", lambda token=token: parse_jwt_envelope(token)))
        elif choice == 3:
            target = bytes(rng.randrange(32, 128) for _ in range(rng.randrange(1, 80)))
            cases.append((f"target-{index}", lambda target=target: parse_request_target(target)))
        elif choice == 4:
            token = "".join(chr(rng.randrange(32, 128)) for _ in range(rng.randrange(1, 140)))
            cases.append(
                (
                    f"token-{index}",
                    lambda token=token: parse_opaque_token(token, kind="nonce"),
                )
            )
        else:
            fields = ["=" + str(rng.randrange(100)), "ok"]
            cases.append((f"import-{index}", lambda fields=fields: parse_import_row(fields)))
    return cases


def run_deterministic_fuzz(seed: int, count: int = 500) -> FuzzResult:
    if not 1 <= count <= 50_000:
        _fail("fuzz.count.invalid")
    accepted = rejected = 0
    failures: list[str] = []
    for case_id, operation in deterministic_cases(seed, count):
        try:
            operation()
            accepted += 1
        except ProtocolError:
            rejected += 1
        except Exception as exc:  # pragma: no cover - the invariant under test
            failures.append(f"{case_id}:{type(exc).__name__}")
    return FuzzResult(seed, count, accepted, rejected, tuple(failures))


def public_error(error: ProtocolError) -> dict[str, Any]:
    """Return a stable, bounded error without input echo or stack details."""
    return {"status": error.status, "error": error.code[:64]}
