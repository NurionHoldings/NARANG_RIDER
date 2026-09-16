-- #043 support casework metadata and references only, PostgreSQL 16.
CREATE TABLE support_cases (
  branch_id text NOT NULL,
  case_id uuid NOT NULL,
  owner_id text NOT NULL,
  case_type text NOT NULL,
  severity text NOT NULL,
  state text NOT NULL,
  resource_ref text NOT NULL CHECK (resource_ref LIKE 'resource://%'),
  assigned_agent_id text,
  version bigint NOT NULL DEFAULT 1,
  sla_due_at timestamptz NOT NULL,
  opened_at timestamptz NOT NULL DEFAULT now(),
  human_rationale_ref text,
  rights_notice_ref text,
  PRIMARY KEY (branch_id, case_id)
);
CREATE TABLE support_case_events (
  branch_id text NOT NULL,
  case_id uuid NOT NULL,
  sequence bigint NOT NULL,
  event_type text NOT NULL,
  actor_id text NOT NULL,
  detail_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (branch_id, case_id, sequence),
  FOREIGN KEY (branch_id, case_id) REFERENCES support_cases (branch_id, case_id)
);
CREATE TABLE support_case_outbox (
  branch_id text NOT NULL,
  outbox_id text NOT NULL,
  case_id uuid NOT NULL,
  event_type text NOT NULL,
  payload_ref text NOT NULL,
  delivered_at timestamptz,
  PRIMARY KEY (branch_id, outbox_id)
);
ALTER TABLE support_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_cases FORCE ROW LEVEL SECURITY;
ALTER TABLE support_case_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_case_events FORCE ROW LEVEL SECURITY;
ALTER TABLE support_case_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_case_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY support_cases_branch ON support_cases
  USING (branch_id = current_setting('narang.branch_id', true))
  WITH CHECK (branch_id = current_setting('narang.branch_id', true));
CREATE POLICY support_events_branch ON support_case_events
  USING (branch_id = current_setting('narang.branch_id', true))
  WITH CHECK (branch_id = current_setting('narang.branch_id', true));
CREATE POLICY support_outbox_branch ON support_case_outbox
  USING (branch_id = current_setting('narang.branch_id', true))
  WITH CHECK (branch_id = current_setting('narang.branch_id', true));
REVOKE UPDATE, DELETE, TRUNCATE ON support_case_events FROM PUBLIC;
REVOKE DELETE, TRUNCATE ON support_cases, support_case_outbox FROM PUBLIC;
