from __future__ import annotations

import json
from pathlib import Path

import pytest

from narang_rider.migration_safety import (
    MigrationRejected,
    authorize_down,
    load_inventory,
    point_in_time_marker,
    recovery_invariants,
)

ROOT = Path(__file__).parents[1]


def test_numbered_inventory_order_checksums_and_transaction_contracts() -> None:
    records = load_inventory(ROOT, ROOT / "migrations/manifest.json")
    assert tuple(record.version for record in records) == (1, 2, 3, 4, 5)


def test_all_current_downs_are_forward_only_and_fail_closed() -> None:
    records = load_inventory(ROOT, ROOT / "migrations/manifest.json")
    for record in records:
        with pytest.raises(MigrationRejected, match="forward-only"):
            authorize_down(
                record,
                environment="prod",
                approver_ids=("operator", "independent-reviewer"),
                backup_evidence_sha256="a" * 64,
            )


def test_checksum_tamper_and_duplicate_order_fail() -> None:
    manifest = json.loads((ROOT / "migrations/manifest.json").read_text())
    manifest["migrations"][0]["up_sha256"] = "0" * 64
    path = ROOT / "build/test-migration-manifest.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(manifest))
    with pytest.raises(MigrationRejected, match="checksum"):
        load_inventory(ROOT, path)


def test_synthetic_restore_replay_and_pitr_marker() -> None:
    snapshot = {
        "ledger_balance": 0,
        "counts": {"orders": 2, "ledger_entries": 4},
        "digests": {"ledger": "synthetic-ledger-digest"},
        "tenant_visibility": True,
        "outbox_states": {"pending": 1, "delivered": 1},
    }
    assert recovery_invariants(snapshot, json.loads(json.dumps(snapshot)))["verdict"] == "PASS"
    assert len(point_in_time_marker(migration_version=5, wal_lsn="synthetic/0", snapshot_sha256="a" * 64)) == 64


def test_restore_rejects_financial_drift_cross_tenant_failure_and_pii() -> None:
    baseline = {
        "ledger_balance": 0,
        "counts": {},
        "digests": {},
        "tenant_visibility": True,
        "outbox_states": {},
    }
    for changed in (
        {**baseline, "ledger_balance": 1},
        {**baseline, "tenant_visibility": False},
        {**baseline, "phone": "synthetic"},
    ):
        with pytest.raises(MigrationRejected):
            recovery_invariants(baseline, changed)
