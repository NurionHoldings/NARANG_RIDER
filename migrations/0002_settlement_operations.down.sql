BEGIN;
DROP TABLE IF EXISTS payout_callback_inbox;
DROP TABLE IF EXISTS payout_instructions;
DROP TABLE IF EXISTS settlement_disputes;
DROP TABLE IF EXISTS settlement_lines;
DROP TABLE IF EXISTS settlement_statements;
DELETE FROM schema_migrations WHERE version = 2;
COMMIT;
