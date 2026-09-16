"""Fail-closed migration classification and recovery evidence contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class MigrationSafety(StrEnum):
    REVERSIBLE = "reversible"
    FORWARD_ONLY = "forward-only"
    DATA_LOSS = "data-loss"


class MigrationRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationRecord:
    version: int
    up: str
    up_sha256: str
    down: str
    down_sha256: str
    down_safety: MigrationSafety
    protects: tuple[str, ...]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_inventory(root: Path, manifest_path: Path) -> tuple[MigrationRecord, ...]:
    payload = json.loads(manifest_path.read_text())
    records = tuple(
        MigrationRecord(
            version=item["version"],
            up=item["up"],
            up_sha256=item["up_sha256"],
            down=item["down"],
            down_sha256=item["down_sha256"],
            down_safety=MigrationSafety(item["down_safety"]),
            protects=tuple(item["protects"]),
        )
        for item in payload["migrations"]
    )
    if tuple(item.version for item in records) != tuple(range(1, len(records) + 1)):
        raise MigrationRejected("migration order must be contiguous and unique")
    for item in records:
        if _digest(root / item.up) != item.up_sha256 or _digest(root / item.down) != item.down_sha256:
            raise MigrationRejected(f"migration checksum mismatch: {item.version}")
        sql = (root / item.up).read_text()
        for contract in ("BEGIN;", "COMMIT;", "lock_timeout", "statement_timeout"):
            if contract not in sql:
                raise MigrationRejected(f"migration {item.version} lacks {contract}")
    return records


def authorize_down(
    record: MigrationRecord,
    *,
    environment: str,
    approver_ids: tuple[str, ...] = (),
    backup_evidence_sha256: str | None = None,
) -> None:
    if record.down_safety is MigrationSafety.DATA_LOSS:
        raise MigrationRejected("data-loss migration down is never an operational rollback")
    if record.down_safety is MigrationSafety.FORWARD_ONLY:
        raise MigrationRejected("forward-only migration requires forward repair, not down")
    if environment == "prod":
        if len(set(approver_ids)) < 2:
            raise MigrationRejected("production down requires two independent approvers")
        if backup_evidence_sha256 is None or len(backup_evidence_sha256) != 64:
            raise MigrationRejected("production down requires verified backup evidence")


def point_in_time_marker(*, migration_version: int, wal_lsn: str, snapshot_sha256: str) -> str:
    if migration_version < 1 or not wal_lsn or len(snapshot_sha256) != 64:
        raise MigrationRejected("invalid point-in-time recovery marker")
    body = f"synthetic|v{migration_version}|{wal_lsn}|{snapshot_sha256}"
    return hashlib.sha256(body.encode()).hexdigest()


def recovery_invariants(snapshot: dict[str, object], restored: dict[str, object]) -> dict[str, object]:
    """Compare synthetic aggregates only; raw personal or production data is forbidden."""
    forbidden = {"phone", "address", "email", "medical", "name"}
    serialized = json.dumps({"before": snapshot, "after": restored}, sort_keys=True)
    if any(f'"{key}"' in serialized.lower() for key in forbidden):
        raise MigrationRejected("recovery drill accepts synthetic non-PII aggregates only")
    required = {"ledger_balance", "counts", "digests", "tenant_visibility", "outbox_states"}
    if set(snapshot) != required or snapshot != restored:
        raise MigrationRejected("restore/replay invariant mismatch")
    if snapshot["ledger_balance"] != 0 or snapshot["tenant_visibility"] is not True:
        raise MigrationRejected("financial balance or tenant isolation failed")
    checksum = hashlib.sha256(json.dumps(restored, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"synthetic": True, "verdict": "PASS", "checksum": checksum, "invariants": sorted(required)}
