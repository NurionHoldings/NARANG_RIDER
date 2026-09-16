from pathlib import Path


def test_incident_migration_has_rls_vault_and_immutable_coverage_guards():
    sql = Path("migrations/0004_rider_incident_support.sql").read_text()
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "UNIQUE(branch_id,assignment_id)" in sql
    assert "narrative_vault_ref LIKE 'vault://%'" in sql
    assert "medical_vault_ref IS NULL" in sql
    assert "UNIQUE(branch_id,rider_id,idempotency_key)" in sql


def test_down_migration_is_ordered_and_scoped():
    sql = Path("migrations/0004_rider_incident_support.down.sql").read_text()
    assert sql.index("rider_incident_audit") < sql.index("rider_incident_cases")
    assert "DELETE FROM schema_migrations WHERE version=4" in sql
