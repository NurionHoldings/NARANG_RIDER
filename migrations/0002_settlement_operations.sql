BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE settlement_statements (
    branch_id text NOT NULL,
    statement_id text NOT NULL,
    period_id text NOT NULL,
    owner_type text NOT NULL CHECK (owner_type IN ('RIDER', 'MERCHANT')),
    owner_id text NOT NULL,
    version bigint NOT NULL CHECK (version = 1),
    gross_won bigint NOT NULL CHECK (gross_won >= 0),
    deductions_won bigint NOT NULL CHECK (deductions_won >= 0),
    net_won bigint NOT NULL CHECK (net_won >= 0 AND net_won = gross_won - deductions_won),
    cutoff_at timestamptz NOT NULL,
    dispute_deadline timestamptz NOT NULL CHECK (dispute_deadline > cutoff_at),
    destination_vault_ref text NOT NULL CHECK (destination_vault_ref LIKE 'vault:%'),
    content_hash text NOT NULL CHECK (length(content_hash) = 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, statement_id)
);

CREATE TABLE settlement_lines (
    branch_id text NOT NULL,
    statement_id text NOT NULL,
    line_id text NOT NULL,
    order_id text NOT NULL,
    ledger_transaction_id text NOT NULL,
    quote_id text NOT NULL,
    cancellation_id text,
    gross_won bigint NOT NULL CHECK (gross_won >= 0),
    deduction_won bigint NOT NULL CHECK (deduction_won >= 0),
    deduction_code text,
    PRIMARY KEY (branch_id, statement_id, line_id),
    FOREIGN KEY (branch_id, statement_id)
        REFERENCES settlement_statements (branch_id, statement_id) ON DELETE RESTRICT,
    CHECK ((deduction_won = 0 AND deduction_code IS NULL) OR
           (deduction_won > 0 AND deduction_code IN
            ('WITHHOLDING_TAX', 'COURT_ORDER', 'VOLUNTARY_INSURANCE')))
);

CREATE TABLE settlement_disputes (
    branch_id text NOT NULL,
    dispute_id text NOT NULL,
    statement_id text NOT NULL,
    line_id text NOT NULL,
    disputed_won bigint NOT NULL CHECK (disputed_won > 0),
    opened_by text NOT NULL,
    opened_at timestamptz NOT NULL,
    PRIMARY KEY (branch_id, dispute_id),
    FOREIGN KEY (branch_id, statement_id, line_id)
        REFERENCES settlement_lines (branch_id, statement_id, line_id) ON DELETE RESTRICT
);

CREATE TABLE payout_instructions (
    branch_id text NOT NULL,
    instruction_id text NOT NULL,
    statement_id text NOT NULL,
    statement_version bigint NOT NULL CHECK (statement_version = 1),
    amount_won bigint NOT NULL CHECK (amount_won >= 0),
    destination_vault_ref text NOT NULL CHECK (destination_vault_ref LIKE 'vault:%'),
    requested_by text NOT NULL,
    approver_one text,
    approver_two text,
    executor_id text,
    status text NOT NULL,
    provider_reference text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, instruction_id),
    UNIQUE (branch_id, statement_id),
    FOREIGN KEY (branch_id, statement_id)
        REFERENCES settlement_statements (branch_id, statement_id) ON DELETE RESTRICT,
    CHECK (approver_one IS NULL OR approver_one <> requested_by),
    CHECK (approver_two IS NULL OR
           (approver_two <> requested_by AND approver_two <> approver_one)),
    CHECK (executor_id IS NULL OR
           (executor_id <> requested_by AND executor_id IS DISTINCT FROM approver_one
            AND executor_id IS DISTINCT FROM approver_two))
);

CREATE TABLE payout_callback_inbox (
    branch_id text NOT NULL,
    event_id text NOT NULL,
    instruction_id text NOT NULL,
    event_sequence bigint NOT NULL CHECK (event_sequence > 0),
    payload_hash text NOT NULL CHECK (length(payload_hash) = 64),
    review_required boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, event_id),
    UNIQUE (branch_id, instruction_id, event_sequence),
    FOREIGN KEY (branch_id, instruction_id)
        REFERENCES payout_instructions (branch_id, instruction_id) ON DELETE RESTRICT
);

DO $rls$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'settlement_statements', 'settlement_lines', 'settlement_disputes',
        'payout_instructions', 'payout_callback_inbox'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY branch_isolation ON %I USING '
            '(branch_id = current_setting(''app.branch_id'', true)) WITH CHECK '
            '(branch_id = current_setting(''app.branch_id'', true))', table_name
        );
    END LOOP;
END
$rls$;

INSERT INTO schema_migrations (version) VALUES (2);
COMMIT;
