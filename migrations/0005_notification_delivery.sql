BEGIN;
CREATE TABLE notification_preferences (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), recipient_id text NOT NULL,
 contact_vault_ref text, channels jsonb NOT NULL, purposes jsonb NOT NULL,
 quiet_start time, quiet_end time, locale text NOT NULL DEFAULT 'ko-KR', version bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(branch_id,recipient_id), CHECK(contact_vault_ref IS NULL OR contact_vault_ref LIKE 'vault://%')
);
CREATE TABLE notification_outbox (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), notice_id text NOT NULL,
 recipient_id text NOT NULL, event text NOT NULL, event_id text NOT NULL, sequence bigint NOT NULL,
 channel text NOT NULL CHECK(channel IN ('IN_APP','PUSH','SMS','EMAIL')),
 template_version integer NOT NULL, lockscreen_text text NOT NULL, deep_link text NOT NULL,
 contact_vault_ref text, state text NOT NULL, attempts integer NOT NULL DEFAULT 0,
 provider_ref text, acknowledgement_limits_rights boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(branch_id,notice_id),
 UNIQUE(branch_id,recipient_id,event,event_id,channel), UNIQUE(branch_id,recipient_id,sequence),
 CHECK(contact_vault_ref IS NULL OR contact_vault_ref LIKE 'vault://%'),
 CHECK(acknowledgement_limits_rights=false)
);
CREATE TABLE notification_callback_receipts (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), callback_id text NOT NULL,
 notice_id text NOT NULL, received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(branch_id,callback_id), FOREIGN KEY(branch_id,notice_id) REFERENCES notification_outbox(branch_id,notice_id)
);
CREATE TABLE notification_audit (
 branch_id text NOT NULL REFERENCES control_branches(branch_id), sequence bigint GENERATED ALWAYS AS IDENTITY,
 action text NOT NULL, subject_id text NOT NULL, actor_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(branch_id,sequence)
);
DO $rls$ DECLARE n text; BEGIN FOREACH n IN ARRAY ARRAY[
 'notification_preferences','notification_outbox','notification_callback_receipts','notification_audit'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',n);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',n);
 EXECUTE format('CREATE POLICY branch_isolation ON %I USING (branch_id=current_setting(''app.branch_id'',true)) WITH CHECK (branch_id=current_setting(''app.branch_id'',true))',n);
 END LOOP; END $rls$;
INSERT INTO schema_migrations(version) VALUES(5);
COMMIT;
