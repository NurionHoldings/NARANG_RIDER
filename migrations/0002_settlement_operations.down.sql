BEGIN;
DROP TABLE IF EXISTS payout_callback_inbox;
DROP TABLE IF EXISTS payout_instructions;
DROP TABLE IF EXISTS settlement_disputes;
DROP TABLE IF EXISTS settlement_lines;
DROP TABLE IF EXISTS settlement_statements;
DO $down$
BEGIN
    IF to_regclass('schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE version = 2;
    END IF;
END
$down$;
COMMIT;
