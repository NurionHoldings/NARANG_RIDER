BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE control_branches (
 branch_id text PRIMARY KEY, parent_id text REFERENCES control_branches(branch_id),
 level text NOT NULL CHECK(level IN ('HQ','REGIONAL','LOCAL')),
 service_zone_ids jsonb NOT NULL, version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 active boolean NOT NULL DEFAULT false
);
CREATE TABLE control_policies (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), policy_id text NOT NULL,
 version bigint NOT NULL CHECK(version>0), effective_at timestamptz NOT NULL,
 payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(branch_id, policy_id), UNIQUE(branch_id, version)
);
CREATE TABLE control_commands (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), command_id text NOT NULL,
 target text NOT NULL CHECK(target IN ('INTAKE','DISPATCH','OUTBOX')),
 impact text NOT NULL CHECK(impact IN ('LOCAL','NATIONAL','FINANCIAL','PRIVACY')),
 reason text NOT NULL, ticket_ref text NOT NULL, requested_by text NOT NULL,
 expected_branch_version bigint NOT NULL, approvals jsonb NOT NULL DEFAULT '[]',
 expires_at timestamptz, executed boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(branch_id,command_id)
);
CREATE TABLE control_audit_outbox (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), sequence bigint GENERATED ALWAYS AS IDENTITY,
 action text NOT NULL, subject_id text NOT NULL, actor_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(branch_id,sequence)
);
CREATE TRIGGER immutable_control_audit_outbox
BEFORE UPDATE OR DELETE ON control_audit_outbox
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
DO $rls$ DECLARE n text; BEGIN FOREACH n IN ARRAY ARRAY[
 'control_policies','control_commands','control_audit_outbox'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',n);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',n);
 EXECUTE format('CREATE POLICY branch_isolation ON %I USING (branch_id=current_setting(''app.branch_id'',true)) WITH CHECK (branch_id=current_setting(''app.branch_id'',true))',n);
 END LOOP; END $rls$;
INSERT INTO schema_migrations(version) VALUES(3);
COMMIT;
