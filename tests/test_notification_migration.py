from pathlib import Path


def test_notification_schema_enforces_privacy_idempotency_and_rights():
    sql = Path("migrations/0005_notification_delivery.sql").read_text()
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "UNIQUE(branch_id,recipient_id,event,event_id,channel)" in sql
    assert "contact_vault_ref LIKE 'vault://%'" in sql
    assert "CHECK(acknowledgement_limits_rights=false)" in sql
    assert "PRIMARY KEY(branch_id,callback_id)" in sql
