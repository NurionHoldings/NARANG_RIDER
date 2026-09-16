-- NARANG RIDER PostgreSQL 16 persistence contract.
-- Application transactions must execute: SET LOCAL app.branch_id = $1.

BEGIN;

CREATE TABLE schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE orders (
    branch_id text NOT NULL,
    record_id text NOT NULL,
    payload jsonb NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    source_system text GENERATED ALWAYS AS (payload ->> 'source_system') STORED,
    source_order_id text GENERATED ALWAYS AS (payload ->> 'source_order_id') STORED,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, record_id),
    UNIQUE (branch_id, source_system, source_order_id)
);

CREATE TABLE partner_events (
    branch_id text NOT NULL,
    record_id text NOT NULL,
    payload jsonb NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    source_system text GENERATED ALWAYS AS (payload ->> 'source_system') STORED,
    source_order_id text GENERATED ALWAYS AS (payload ->> 'source_order_id') STORED,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, record_id),
    UNIQUE (branch_id, source_system, source_order_id)
);

CREATE TABLE rider_calls (
    branch_id text NOT NULL,
    record_id text NOT NULL,
    order_id text NOT NULL,
    payload jsonb NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, record_id),
    UNIQUE (branch_id, order_id),
    FOREIGN KEY (branch_id, order_id) REFERENCES orders (branch_id, record_id)
);

CREATE TABLE ledger_transactions (
    branch_id text NOT NULL,
    record_id text NOT NULL,
    payload jsonb NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, record_id)
);

CREATE TABLE ledger_entries (
    branch_id text NOT NULL,
    transaction_id text NOT NULL,
    entry_sequence integer NOT NULL CHECK (entry_sequence >= 0),
    account_code text NOT NULL,
    amount_won bigint NOT NULL CHECK (amount_won <> 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, transaction_id, entry_sequence),
    FOREIGN KEY (branch_id, transaction_id)
        REFERENCES ledger_transactions (branch_id, record_id) ON DELETE RESTRICT
);

CREATE FUNCTION enforce_balanced_ledger_transaction() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM ledger_entries
        WHERE branch_id = NEW.branch_id AND transaction_id = NEW.transaction_id
        OFFSET 1
    ) OR (
        SELECT COALESCE(sum(amount_won), 0) FROM ledger_entries
        WHERE branch_id = NEW.branch_id AND transaction_id = NEW.transaction_id
    ) <> 0 THEN
        RAISE EXCEPTION 'unbalanced ledger transaction' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER balanced_ledger_transaction
AFTER INSERT ON ledger_entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_balanced_ledger_transaction();

CREATE FUNCTION reject_append_only_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only record mutation forbidden' USING ERRCODE = '42501';
END;
$$;

CREATE TABLE outbox_messages (
    branch_id text NOT NULL,
    record_id text NOT NULL,
    payload jsonb NOT NULL,
    version bigint NOT NULL CHECK (version > 0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_owner text,
    lease_until timestamptz,
    delivered_at timestamptz,
    dead_lettered_at timestamptz,
    failure_code text,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, record_id)
);

CREATE INDEX outbox_delivery_idx
    ON outbox_messages (branch_id, available_at, created_at)
    WHERE delivered_at IS NULL AND dead_lettered_at IS NULL;

CREATE INDEX outbox_stream_order_idx
    ON outbox_messages (
        branch_id,
        (payload ->> 'partner_id'),
        (payload ->> 'stream_id'),
        ((payload ->> 'sequence')::bigint)
    )
    WHERE delivered_at IS NULL;

CREATE TABLE outbox_review_events (
    branch_id text NOT NULL,
    message_id text NOT NULL,
    event_sequence bigint GENERATED ALWAYS AS IDENTITY,
    partner_id text NOT NULL,
    reason_code text NOT NULL,
    review_required boolean NOT NULL DEFAULT true CHECK (review_required),
    financial_adjustment_allowed boolean NOT NULL DEFAULT false
        CHECK (NOT financial_adjustment_allowed),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, message_id, event_sequence),
    FOREIGN KEY (branch_id, message_id)
        REFERENCES outbox_messages (branch_id, record_id) ON DELETE RESTRICT
);

CREATE TABLE idempotency_records (
    branch_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest text NOT NULL CHECK (length(payload_digest) = 64),
    commit_id text NOT NULL,
    audit_hash text NOT NULL CHECK (length(audit_hash) = 64),
    record_refs jsonb NOT NULL,
    versions jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, idempotency_key)
);

CREATE TABLE audit_receipts (
    branch_id text NOT NULL,
    commit_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest text NOT NULL CHECK (length(payload_digest) = 64),
    audit_hash text NOT NULL CHECK (length(audit_hash) = 64),
    record_refs jsonb NOT NULL,
    versions jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (branch_id, commit_id),
    UNIQUE (branch_id, idempotency_key),
    FOREIGN KEY (branch_id, idempotency_key)
        REFERENCES idempotency_records (branch_id, idempotency_key)
);

CREATE TRIGGER immutable_ledger_transactions
BEFORE UPDATE OR DELETE ON ledger_transactions
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER immutable_ledger_entries
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER immutable_idempotency_records
BEFORE UPDATE OR DELETE ON idempotency_records
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
CREATE TRIGGER immutable_audit_receipts
BEFORE UPDATE OR DELETE ON audit_receipts
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

INSERT INTO schema_migrations (version) VALUES (1);

DO $rls$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'orders', 'partner_events', 'rider_calls', 'ledger_transactions',
        'ledger_entries', 'outbox_messages', 'outbox_review_events',
        'idempotency_records', 'audit_receipts'
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

COMMIT;
