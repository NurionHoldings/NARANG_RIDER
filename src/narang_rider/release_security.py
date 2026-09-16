"""Fail-closed release readiness, configuration, and rollback contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class ReleaseRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class EnvironmentConfig:
    environment: Environment
    database_config_ref: str
    jwks_config_ref: str
    webhook_secret_ref: str
    allowed_origins: tuple[str, ...]
    debug: bool = False
    deployment_enabled: bool = False

    def validate(self) -> None:
        for value in (self.database_config_ref, self.jwks_config_ref, self.webhook_secret_ref):
            if not value.startswith("config://") or any(marker in value.lower() for marker in ("password=", "secret=", "token=")):
                raise ReleaseRejected("configuration must use an opaque config reference")
        if "*" in self.allowed_origins:
            raise ReleaseRejected("wildcard CORS is forbidden")
        if self.environment is Environment.PROD:
            if self.debug:
                raise ReleaseRejected("debug is forbidden in production")
            if not self.allowed_origins:
                raise ReleaseRejected("production origins are required")
            if self.deployment_enabled:
                raise ReleaseRejected("deployment stays disabled until an external approval gate")


@dataclass(frozen=True)
class ReleaseManifest:
    commit_sha: str
    migration_version: int
    api_version: str
    frontend_version: str
    artifact_digest: str
    rollback_target_sha: str
    security_ci_passed: bool
    postgres_restore_passed: bool
    approver_ids: tuple[str, ...] = ()

    def validate(self, previous_migration_version: int) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit_sha):
            raise ReleaseRejected("invalid commit")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.artifact_digest):
            raise ReleaseRejected("artifact digest required")
        if self.migration_version < previous_migration_version:
            raise ReleaseRejected("migration downgrade hazard")
        if not self.security_ci_passed or not self.postgres_restore_passed:
            raise ReleaseRejected("release evidence incomplete")
        if len(set(self.approver_ids)) < 2:
            raise ReleaseRejected("independent release approvals required")
        if self.commit_sha == self.rollback_target_sha:
            raise ReleaseRejected("rollback target must be the previous verified release")


FORBIDDEN_LOG_KEYS = {"authorization", "cookie", "password", "secret", "token", "medical", "phone", "address"}


def validate_log_event(event: Mapping[str, object]) -> None:
    if FORBIDDEN_LOG_KEYS & {key.lower() for key in event}:
        raise ReleaseRejected("sensitive value is forbidden in logs")


def validate_manifest_json(document: str) -> None:
    parsed = json.loads(document)
    serialized = json.dumps(parsed).lower()
    if any(marker in serialized for marker in ("password", "private_key", "access_token", "client_secret")):
        raise ReleaseRejected("secret-like field in release manifest")


def migration_inventory(directory: Path) -> tuple[int, ...]:
    up = {int(path.name[:4]) for path in directory.glob("[0-9][0-9][0-9][0-9]_*.sql") if ".down." not in path.name}
    down = {int(path.name[:4]) for path in directory.glob("[0-9][0-9][0-9][0-9]_*.down.sql")}
    if up != down or up != set(range(1, max(up, default=0) + 1)):
        raise ReleaseRejected("migration drift detected")
    return tuple(sorted(up))


def synthetic_backup_restore(source: Mapping[str, object]) -> dict[str, object]:
    """Synthetic-only deterministic restore proof; never accepts PII-shaped fields."""
    serialized = json.dumps(source, sort_keys=True, separators=(",", ":"))
    if any(key in serialized.lower() for key in ("phone", "address", "medical", "email")):
        raise ReleaseRejected("restore proof must use synthetic non-PII data")
    restored = json.loads(serialized)
    before = hashlib.sha256(serialized.encode()).hexdigest()
    after = hashlib.sha256(json.dumps(restored, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if before != after:
        raise ReleaseRejected("restore checksum mismatch")
    return {"checksum": after, "record_count": len(restored), "synthetic": True}

