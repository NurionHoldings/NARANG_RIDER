BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE rider_coverage_snapshots (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), snapshot_id text NOT NULL,
 assignment_id text NOT NULL, rider_id text NOT NULL, provider_reference text NOT NULL,
 product_reference text NOT NULL, captured_at timestamptz NOT NULL,
 PRIMARY KEY(branch_id,snapshot_id), UNIQUE(branch_id,assignment_id)
);
CREATE TABLE rider_incident_cases (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), case_id text NOT NULL,
 rider_id text NOT NULL, assignment_id text NOT NULL, kind text NOT NULL
 CHECK(kind IN ('EMERGENCY','ACCIDENT','INJURY','VEHICLE','PROPERTY')),
 state text NOT NULL CHECK(state IN ('REPORTED','TRIAGED','HUMAN_ASSIGNED','SUBMITTED','RESOLVED','APPEALED')),
 coarse_zone text NOT NULL, narrative_vault_ref text NOT NULL,
 medical_vault_ref text, coverage_snapshot_id text NOT NULL,
 assigned_human_id text, external_case_ref text, resolution_ref text,
 correction_ref text, appeal_reason_vault_ref text, idempotency_key text NOT NULL,
 payload_digest text NOT NULL, version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(branch_id,case_id), UNIQUE(branch_id,rider_id,idempotency_key),
 UNIQUE(branch_id,rider_id,assignment_id,kind),
 FOREIGN KEY(branch_id,coverage_snapshot_id) REFERENCES rider_coverage_snapshots(branch_id,snapshot_id),
 CHECK(narrative_vault_ref LIKE 'vault://%'),
 CHECK(medical_vault_ref IS NULL OR medical_vault_ref LIKE 'vault://%')
);
CREATE TABLE rider_incident_audit (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), sequence bigint GENERATED ALWAYS AS IDENTITY,
 case_id text NOT NULL, actor_id text NOT NULL, action text NOT NULL,
 sensitive_access boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(branch_id,sequence)
);
CREATE TRIGGER immutable_rider_incident_audit
BEFORE UPDATE OR DELETE ON rider_incident_audit
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
DO $rls$ DECLARE n text; BEGIN FOREACH n IN ARRAY ARRAY[
 'rider_coverage_snapshots','rider_incident_cases','rider_incident_audit'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',n);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',n);
 EXECUTE format('CREATE POLICY branch_isolation ON %I USING (branch_id=current_setting(''app.branch_id'',true)) WITH CHECK (branch_id=current_setting(''app.branch_id'',true))',n);
 END LOOP; END $rls$;
INSERT INTO schema_migrations(version) VALUES(4);
COMMIT;
