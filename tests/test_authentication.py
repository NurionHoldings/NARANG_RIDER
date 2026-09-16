from __future__ import annotations

import base64
import json

import pytest

from narang_rider.api_contract import VerifiedPrincipal
from narang_rider.application import CommandCapability
from narang_rider.authentication import (
    AuthenticationRejected,
    AuthErrorCode,
    OidcJwtVerifier,
    OidcPolicy,
    PrincipalKind,
    PrincipalRecord,
    TransportAuthVerifier,
)

NOW = 2_000_000_000


def encode(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def token(*, header: dict[str, object] | None = None, claims: dict[str, object] | None = None) -> str:
    default_claims = {
        "iss": "https://identity.narang.example",
        "aud": "narang-api",
        "sub": "user-1",
        "exp": NOW + 300,
        "nbf": NOW - 10,
        "iat": NOW - 10,
        "jti": "token-1",
        "role": "platform_owner",
        "capabilities": ["anything"],
    }
    default_claims.update(claims or {})
    return "Bearer " + ".".join(
        (encode(header or {"alg": "RS256", "kid": "key-1"}), encode(default_claims), encode("sig"))
    )


class Keys:
    def __init__(self) -> None:
        self.values: dict[str, object] = {"key-1": object()}
        self.after_refresh: dict[str, object] = {}
        self.refreshes = 0

    def get(self, issuer: str, key_id: str) -> object | None:
        assert issuer == "https://identity.narang.example"
        return self.values.get(key_id)

    def refresh(self, issuer: str) -> None:
        self.refreshes += 1
        self.values = dict(self.after_refresh)


class Signatures:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.algorithms: list[str] = []

    def verify(self, *, signing_input: bytes, signature: bytes, key: object, algorithm: str) -> bool:
        assert signing_input and signature and key
        self.algorithms.append(algorithm)
        return self.valid


class Principals:
    def __init__(self, record: PrincipalRecord | None) -> None:
        self.record = record
        self.lookups: list[tuple[str, PrincipalKind]] = []

    def find_active(self, subject: str, kind: PrincipalKind) -> PrincipalRecord | None:
        self.lookups.append((subject, kind))
        return self.record


class Replay:
    def __init__(self) -> None:
        self.seen: set[tuple[str, str, str]] = set()

    def consume(self, issuer: str, subject: str, token_id: str, expires_at: int) -> bool:
        assert expires_at > NOW
        key = issuer, subject, token_id
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


def active_principal(*, active: bool = True, branch: str = "branch-a") -> PrincipalRecord:
    return PrincipalRecord(
        subject="user-1",
        actor_id="actor-server-side",
        kind=PrincipalKind.USER,
        active=active,
        branch_id=branch,
        capabilities=frozenset({CommandCapability.INGEST_ORDER}),
    )


def verifier(
    *, keys: Keys | None = None, signatures: Signatures | None = None,
    principals: Principals | None = None, replay: Replay | None = None,
) -> OidcJwtVerifier:
    return OidcJwtVerifier(
        policy=OidcPolicy(
            issuer="https://identity.narang.example",
            audience="narang-api",
            allowed_algorithms=frozenset({"RS256"}),
            clock_skew_seconds=30,
        ),
        keys=keys or Keys(),
        signatures=signatures or Signatures(),
        principals=principals or Principals(active_principal()),
        replay_guard=replay or Replay(),
        clock=lambda: NOW,
    )


def reject(auth: OidcJwtVerifier, value: str, code: AuthErrorCode) -> None:
    with pytest.raises(AuthenticationRejected) as captured:
        auth.verify(value, "branch-a")
    assert captured.value.code == code


def test_claim_roles_are_ignored_and_server_authority_is_loaded() -> None:
    repository = Principals(active_principal())
    result = verifier(principals=repository).verify(token(), "branch-a")
    assert result.actor_id == "actor-server-side"
    assert result.capabilities == frozenset({CommandCapability.INGEST_ORDER})
    assert repository.lookups == [("user-1", PrincipalKind.USER)]


@pytest.mark.parametrize("algorithm", ["none", "HS256", "rs256", ""])
def test_none_and_algorithm_confusion_are_rejected_before_crypto(algorithm: str) -> None:
    signatures = Signatures()
    reject(
        verifier(signatures=signatures),
        token(header={"alg": algorithm, "kid": "key-1"}),
        AuthErrorCode.INVALID_CREDENTIAL,
    )
    assert signatures.algorithms == []


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"exp": NOW - 31}, AuthErrorCode.TOKEN_EXPIRED),
        ({"nbf": NOW + 31}, AuthErrorCode.TOKEN_NOT_ACTIVE),
        ({"iat": NOW + 31}, AuthErrorCode.TOKEN_NOT_ACTIVE),
        ({"iss": "https://attacker.example"}, AuthErrorCode.INVALID_CREDENTIAL),
        ({"aud": "other-api"}, AuthErrorCode.INVALID_CREDENTIAL),
        ({"sub": ""}, AuthErrorCode.INVALID_CREDENTIAL),
    ],
)
def test_time_issuer_audience_and_subject_claims_fail_closed(
    claims: dict[str, object], code: AuthErrorCode
) -> None:
    reject(verifier(), token(claims=claims), code)


def test_unknown_kid_refreshes_once_then_fails_closed() -> None:
    keys = Keys()
    reject(
        verifier(keys=keys),
        token(header={"alg": "RS256", "kid": "missing"}),
        AuthErrorCode.UNKNOWN_SIGNING_KEY,
    )
    assert keys.refreshes == 1


def test_atomic_key_rotation_can_supply_new_key() -> None:
    keys = Keys()
    keys.after_refresh = {"new-key": object()}
    result = verifier(keys=keys).verify(
        token(header={"alg": "RS256", "kid": "new-key"}), "branch-a"
    )
    assert result.actor_id == "actor-server-side"
    assert keys.refreshes == 1


def test_invalid_signature_disabled_principal_and_cross_branch_are_rejected() -> None:
    reject(verifier(signatures=Signatures(False)), token(), AuthErrorCode.INVALID_CREDENTIAL)
    reject(
        verifier(principals=Principals(active_principal(active=False))),
        token(), AuthErrorCode.PRINCIPAL_DISABLED,
    )
    with pytest.raises(AuthenticationRejected) as captured:
        verifier().verify(token(), "branch-b")
    assert captured.value.code == AuthErrorCode.BRANCH_SCOPE_MISMATCH


def test_privileged_token_requires_unique_jti() -> None:
    replay = Replay()
    auth = verifier(replay=replay)
    auth.verify(token(), "branch-a")
    reject(auth, token(), AuthErrorCode.TOKEN_REPLAYED)
    reject(verifier(), token(claims={"jti": ""}), AuthErrorCode.INVALID_CREDENTIAL)


def test_malformed_compact_token_and_bad_policy_are_rejected() -> None:
    reject(verifier(), "Bearer not.a.jwt.with.extra", AuthErrorCode.INVALID_CREDENTIAL)
    with pytest.raises(ValueError):
        OidcPolicy("issuer", "aud", frozenset({"none"}))
    with pytest.raises(ValueError):
        OidcPolicy("issuer", "aud", frozenset({"HS256"}))
    with pytest.raises(ValueError):
        OidcPolicy("issuer", "aud", frozenset({"RS256"}), clock_skew_seconds=121)


def test_service_credentials_are_never_processed_as_user_jwt() -> None:
    class Services:
        def verify_service(self, authorization: str, requested_branch_id: str) -> VerifiedPrincipal:
            assert authorization == "Service workload-proof"
            return VerifiedPrincipal("svc-1", requested_branch_id, frozenset())

    transport = TransportAuthVerifier(verifier(), Services())
    assert transport.verify("Service workload-proof", "branch-a").actor_id == "svc-1"
    with pytest.raises(AuthenticationRejected):
        transport.verify("Basic credential", "branch-a")
