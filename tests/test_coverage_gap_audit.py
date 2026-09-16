"""Targeted evidence for fail-closed branches found by the #054 audit."""

from __future__ import annotations

import base64
import json

import pytest

from narang_rider.application import CommandCapability
from narang_rider.authentication import (
    AuthenticationRejected,
    AuthErrorCode,
    OidcJwtVerifier,
    OidcPolicy,
    PrincipalKind,
    PrincipalRecord,
)
from narang_rider.ledger import LedgerAccount, LedgerEntry, LedgerTransaction
from narang_rider.money import Money
from narang_rider.persistence import (
    AtomicityViolation,
    InMemoryPersistence,
    RecordKind,
    SensitiveDataRejected,
    canonical_payload_digest,
)

NOW = 2_000_000_000


def _encode(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class _Keys:
    def get(self, issuer: str, key_id: str) -> object | None:
        return object() if key_id == "key-1" else None

    def refresh(self, issuer: str) -> None:
        return None


class _Signatures:
    def verify(self, **kwargs: object) -> bool:
        return True


class _Principals:
    def __init__(self, capabilities: frozenset[CommandCapability]) -> None:
        self.capabilities = capabilities

    def find_active(self, subject: str, kind: PrincipalKind) -> PrincipalRecord | None:
        return PrincipalRecord(subject, "actor-1", kind, True, "branch-a", self.capabilities)


class _Replay:
    def consume(self, issuer: str, subject: str, token_id: str, expires_at: int) -> bool:
        return True


def _jwt(claims: dict[str, object], *, payload_segment: str | None = None) -> str:
    complete = {
        "iss": "issuer",
        "aud": "audience",
        "sub": "subject",
        "exp": NOW + 60,
        "nbf": NOW - 1,
        "iat": NOW - 1,
    }
    complete.update(claims)
    return "Bearer " + ".".join(
        (
            _encode({"alg": "RS256", "kid": "key-1"}),
            payload_segment or _encode(complete),
            _encode("sig"),
        )
    )


def _verifier(capabilities: frozenset[CommandCapability] = frozenset()) -> OidcJwtVerifier:
    return OidcJwtVerifier(
        policy=OidcPolicy("issuer", "audience", frozenset({"RS256"})),
        keys=_Keys(),
        signatures=_Signatures(),
        principals=_Principals(capabilities),
        replay_guard=_Replay(),
        clock=lambda: NOW,
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": ["audience", 7]},
        {"exp": True},
        {"nbf": False},
        {"iat": True},
    ],
)
def test_auth_rejects_mixed_audience_and_boolean_numeric_dates(claims: dict[str, object]) -> None:
    with pytest.raises(AuthenticationRejected) as captured:
        _verifier().verify(_jwt(claims), "branch-a")
    assert captured.value.code is AuthErrorCode.INVALID_CREDENTIAL


def test_auth_rejects_oversized_or_non_object_claim_segments() -> None:
    with pytest.raises(AuthenticationRejected) as oversized:
        _verifier().verify(_jwt({}, payload_segment="a" * 16_385), "branch-a")
    assert oversized.value.code is AuthErrorCode.INVALID_CREDENTIAL

    with pytest.raises(AuthenticationRejected) as non_object:
        _verifier().verify(_jwt({}, payload_segment=_encode(["not", "claims"])), "branch-a")
    assert non_object.value.code is AuthErrorCode.INVALID_CREDENTIAL


def test_unprivileged_token_does_not_require_replay_identifier() -> None:
    principal = _verifier().verify(_jwt({}), "branch-a")
    assert principal.actor_id == "actor-1"
    assert principal.capabilities == frozenset()


def test_ledger_rejects_missing_policy_provenance_even_when_balanced() -> None:
    entries = (
        LedgerEntry(LedgerAccount.CUSTOMER_RECEIVABLE, Money(100), Money(0)),
        LedgerEntry(LedgerAccount.RIDER_PAYABLE, Money(0), Money(100)),
    )
    with pytest.raises(ValueError, match="LEDGER_TRANSACTION_IDENTITY_REQUIRED"):
        LedgerTransaction("tx-1", "order-1", " ", entries)


def test_persistence_rejects_nested_tuple_pii_before_staging() -> None:
    store = InMemoryPersistence()
    unit = store.begin(branch_id="branch-a", idempotency_key="key-1", payload_digest="digest")
    with pytest.raises(SensitiveDataRejected, match=r"payload\.items\[0\]\.phone"):
        unit.put(
            RecordKind.ORDER,
            "order-1",
            {"branch_id": "branch-a", "items": ({"phone": "010-0000-0000"},)},
            expected_version=0,
        )
    with pytest.raises(Exception, match="empty transaction cannot commit"):
        unit.commit()


def test_financial_outbox_cannot_commit_without_ledger_counterpart() -> None:
    payload = {"branch_id": "branch-a", "requires_ledger": True}
    store = InMemoryPersistence()
    unit = store.begin(
        branch_id="branch-a",
        idempotency_key="key-financial",
        payload_digest=canonical_payload_digest(payload),
    )
    unit.put(RecordKind.OUTBOX_MESSAGE, "outbox-1", payload, expected_version=0)
    with pytest.raises(AtomicityViolation, match="financial outbox requires"):
        unit.commit()
