-- #042 privacy rights and retention; metadata/references only, PostgreSQL 16.
CREATE TABLE privacy_rights_requests (
  branch_id text NOT NULL,
  request_id uuid NOT NULL,
  subject_id text NOT NULL,
  right_type text NOT NULL CHECK (right_type IN ('access','correction','deletion','restriction','export','objection')),
  identity_proof_ref text NOT NULL CHECK (identity_proof_ref LIKE 'vault://identity/%'),
  state text NOT NULL,
  idempotency_digest text NOT NULL,
  due_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (branch_id, request_id),
  UNIQUE (branch_id, subject_id, idempotency_digest)
);
CREATE TABLE privacy_legal_holds (
  branch_id text NOT NULL,
  hold_id uuid NOT NULL,
  subject_id text NOT NULL,
  scoped_record_ids jsonb NOT NULL CHECK (jsonb_array_length(scoped_record_ids) BETWEEN 1 AND 500),
  reason_ref text NOT NULL CHECK (reason_ref LIKE 'legal://%'),
  expires_at timestamptz NOT NULL,
  approver_one text NOT NULL,
  approver_two text NOT NULL CHECK (approver_one <> approver_two),
  PRIMARY KEY (branch_id, hold_id)
);
CREATE TABLE privacy_lifecycle_outbox (
  branch_id text NOT NULL,
  intent_id text NOT NULL,
  request_id uuid,
  action_type text NOT NULL,
  record_ref text NOT NULL,
  vault_ref text CHECK (vault_ref IS NULL OR vault_ref LIKE 'vault://%'),
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  PRIMARY KEY (branch_id, intent_id)
);
ALTER TABLE privacy_rights_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE privacy_rights_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE privacy_legal_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE privacy_legal_holds FORCE ROW LEVEL SECURITY;
ALTER TABLE privacy_lifecycle_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE privacy_lifecycle_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY privacy_request_branch_scope ON privacy_rights_requests
  USING (branch_id = current_setting('narang.branch_id', true))
  WITH CHECK (branch_id = current_setting('narang.branch_id', true));
CREATE POLICY privacy_hold_branch_scope ON privacy_legal_holds
  USING (branch_id = current_setting('narang.branch_id', true))
  WITH CHECK (branch_id = current_setting('narang.branch_id', true));
CREATE POLICY privacy_outbox_branch_scope ON privacy_lifecycle_outbox
  USING (branch_id = current_setting('narang.branch_id', true))
  WITH CHECK (branch_id = current_setting('narang.branch_id', true));
REVOKE DELETE, TRUNCATE ON privacy_rights_requests, privacy_legal_holds, privacy_lifecycle_outbox FROM PUBLIC;
