"""Fail-closed transport authentication and server-side authorization lookup.

Cryptographic verification is deliberately delegated to an injected, audited
implementation. This module never implements signature algorithms itself.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .api_contract import AuthVerificationError, VerifiedPrincipal
from .application import CommandCapability


class AuthErrorCode(StrEnum):
    INVALID_CREDENTIAL = "INVALID_CREDENTIAL"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_NOT_ACTIVE = "TOKEN_NOT_ACTIVE"
    TOKEN_REPLAYED = "TOKEN_REPLAYED"
    UNKNOWN_SIGNING_KEY = "UNKNOWN_SIGNING_KEY"
    PRINCIPAL_DISABLED = "PRINCIPAL_DISABLED"
    BRANCH_SCOPE_MISMATCH = "BRANCH_SCOPE_MISMATCH"


class AuthenticationRejected(AuthVerificationError):
    """Stable authentication failure without token or key details."""

    def __init__(self, code: AuthErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE = "service"


@dataclass(frozen=True)
class PrincipalRecord:
    subject: str
    actor_id: str
    kind: PrincipalKind
    active: bool
    branch_id: str
    capabilities: frozenset[CommandCapability]


class PrincipalRepository(Protocol):
    def find_active(self, subject: str, kind: PrincipalKind) -> PrincipalRecord | None: ...


class SigningKeyProvider(Protocol):
    """JWKS cache boundary. Refresh must replace keys atomically."""

    def get(self, issuer: str, key_id: str) -> object | None: ...

    def refresh(self, issuer: str) -> None: ...


class SignatureVerifier(Protocol):
    """Audited crypto provider boundary; implementations must reject key/alg confusion."""

    def verify(
        self, *, signing_input: bytes, signature: bytes, key: object, algorithm: str
    ) -> bool: ...


class TokenReplayGuard(Protocol):
    def consume(self, issuer: str, subject: str, token_id: str, expires_at: int) -> bool: ...


@dataclass(frozen=True)
class OidcPolicy:
    issuer: str
    audience: str
    allowed_algorithms: frozenset[str]
    clock_skew_seconds: int = 30
    require_jti_for_privileged: bool = True

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise ValueError("issuer and audience are required")
        asymmetric_algorithms = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
        if not self.allowed_algorithms or not self.allowed_algorithms <= asymmetric_algorithms:
            raise ValueError("a non-empty asymmetric algorithm allowlist is required")
        if not 0 <= self.clock_skew_seconds <= 120:
            raise ValueError("clock skew must be between 0 and 120 seconds")


def _decode_segment(segment: str) -> dict[str, Any]:
    if not segment or len(segment) > 16_384:
        raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
    try:
        padding = "=" * (-len(segment) % 4)
        value = json.loads(base64.urlsafe_b64decode(segment + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL) from error
    if not isinstance(value, dict):
        raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
    return value


def _decode_signature(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except ValueError as error:
        raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL) from error


class OidcJwtVerifier:
    """Validates JWT envelope/claims then reloads authority from server storage."""

    def __init__(
        self,
        *,
        policy: OidcPolicy,
        keys: SigningKeyProvider,
        signatures: SignatureVerifier,
        principals: PrincipalRepository,
        replay_guard: TokenReplayGuard,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._policy = policy
        self._keys = keys
        self._signatures = signatures
        self._principals = principals
        self._replay_guard = replay_guard
        self._clock = clock

    def verify(self, authorization: str, requested_branch_id: str) -> VerifiedPrincipal:
        if not authorization.startswith("Bearer "):
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        compact = authorization[7:]
        parts = compact.split(".")
        if len(parts) != 3:
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        header = _decode_segment(parts[0])
        claims = _decode_segment(parts[1])
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if not isinstance(algorithm, str) or algorithm not in self._policy.allowed_algorithms:
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        if not isinstance(key_id, str) or not key_id:
            raise AuthenticationRejected(AuthErrorCode.UNKNOWN_SIGNING_KEY)
        key = self._keys.get(self._policy.issuer, key_id)
        if key is None:
            self._keys.refresh(self._policy.issuer)
            key = self._keys.get(self._policy.issuer, key_id)
        if key is None:
            raise AuthenticationRejected(AuthErrorCode.UNKNOWN_SIGNING_KEY)
        if not self._signatures.verify(
            signing_input=f"{parts[0]}.{parts[1]}".encode("ascii"),
            signature=_decode_signature(parts[2]),
            key=key,
            algorithm=algorithm,
        ):
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        subject, expires_at = self._validate_claims(claims)
        principal = self._principals.find_active(subject, PrincipalKind.USER)
        if principal is None or not principal.active:
            raise AuthenticationRejected(AuthErrorCode.PRINCIPAL_DISABLED)
        if principal.branch_id != requested_branch_id:
            raise AuthenticationRejected(AuthErrorCode.BRANCH_SCOPE_MISMATCH)
        if self._policy.require_jti_for_privileged and principal.capabilities:
            token_id = claims.get("jti")
            if not isinstance(token_id, str) or not token_id:
                raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
            if not self._replay_guard.consume(
                self._policy.issuer, subject, token_id, expires_at
            ):
                raise AuthenticationRejected(AuthErrorCode.TOKEN_REPLAYED)
        return VerifiedPrincipal(
            actor_id=principal.actor_id,
            branch_id=principal.branch_id,
            capabilities=principal.capabilities,
        )

    def _validate_claims(self, claims: Mapping[str, Any]) -> tuple[str, int]:
        now = int(self._clock())
        skew = self._policy.clock_skew_seconds
        if claims.get("iss") != self._policy.issuer:
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        audience = claims.get("aud")
        if isinstance(audience, str):
            audiences = {audience}
        elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
            audiences = set(audience)
        else:
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        if self._policy.audience not in audiences:
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        exp = claims.get("exp")
        nbf = claims.get("nbf", 0)
        issued_at = claims.get("iat", 0)
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (exp, nbf, issued_at)):
            raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
        if exp < now - skew:
            raise AuthenticationRejected(AuthErrorCode.TOKEN_EXPIRED)
        if nbf > now + skew or issued_at > now + skew:
            raise AuthenticationRejected(AuthErrorCode.TOKEN_NOT_ACTIVE)
        return subject, exp


class ServiceCredentialVerifier(Protocol):
    """Separate mTLS/workload credential boundary; never accepts user JWTs."""

    def verify_service(
        self, authorization: str, requested_branch_id: str
    ) -> VerifiedPrincipal: ...


class TransportAuthVerifier:
    """Routes disjoint user and service credential schemes."""

    def __init__(self, users: OidcJwtVerifier, services: ServiceCredentialVerifier) -> None:
        self._users = users
        self._services = services

    def verify(self, authorization: str, requested_branch_id: str) -> VerifiedPrincipal:
        if authorization.startswith("Bearer "):
            return self._users.verify(authorization, requested_branch_id)
        if authorization.startswith("Service "):
            return self._services.verify_service(authorization, requested_branch_id)
        raise AuthenticationRejected(AuthErrorCode.INVALID_CREDENTIAL)
