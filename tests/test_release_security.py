import json
from pathlib import Path

import pytest

from narang_rider.release_security import (
    Environment,
    EnvironmentConfig,
    ReleaseManifest,
    ReleaseRejected,
    migration_inventory,
    synthetic_backup_restore,
    validate_log_event,
    validate_manifest_json,
)


def production(**overrides):
    values = {
        "environment": Environment.PROD,
        "database_config_ref": "config://prod/db",
        "jwks_config_ref": "config://prod/jwks",
        "webhook_secret_ref": "config://prod/webhook",
        "allowed_origins": ("https://rider.example.invalid",),
    }
    values.update(overrides)
    return EnvironmentConfig(**values)


@pytest.mark.parametrize("field", ["database_config_ref", "jwks_config_ref", "webhook_secret_ref"])
def test_missing_production_config_fails_closed(field):
    with pytest.raises(ReleaseRejected):
        production(**{field: ""}).validate()


def test_debug_wildcard_cors_and_deployment_enable_are_rejected():
    with pytest.raises(ReleaseRejected):
        production(debug=True).validate()
    with pytest.raises(ReleaseRejected):
        production(allowed_origins=("*",)).validate()
    with pytest.raises(ReleaseRejected):
        production(deployment_enabled=True).validate()


def test_secrets_are_rejected_from_logs_and_release_manifest():
    with pytest.raises(ReleaseRejected):
        validate_log_event({"event": "startup", "token": "leak"})
    with pytest.raises(ReleaseRejected):
        validate_manifest_json(json.dumps({"commit": "abc", "client_secret": "leak"}))


def test_release_requires_evidence_two_approvers_and_no_downgrade():
    manifest = ReleaseManifest("a" * 40, 5, "v1", "0.1.0", "sha256:" + "b" * 64,
                               "c" * 40, True, True, ("operator_1", "reviewer_1"))
    manifest.validate(5)
    with pytest.raises(ReleaseRejected):
        ReleaseManifest(**{**manifest.__dict__, "migration_version": 4}).validate(5)
    with pytest.raises(ReleaseRejected):
        ReleaseManifest(**{**manifest.__dict__, "approver_ids": ("operator_1",)}).validate(5)
    with pytest.raises(ReleaseRejected):
        ReleaseManifest(**{**manifest.__dict__, "security_ci_passed": False}).validate(5)


def test_migrations_have_contiguous_up_down_pairs():
    assert migration_inventory(Path("migrations")) == (1, 2, 3, 4, 5)


def test_synthetic_restore_is_deterministic_and_rejects_pii():
    result = synthetic_backup_restore({"orders": [{"id": "synthetic-1", "state": "RECEIVED"}]})
    assert result["synthetic"] and result["record_count"] == 1
    with pytest.raises(ReleaseRejected):
        synthetic_backup_restore({"phone": "010-0000-0000"})
