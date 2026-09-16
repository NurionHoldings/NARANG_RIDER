BEGIN;
DROP TABLE IF EXISTS control_audit_outbox;
DROP TABLE IF EXISTS control_commands;
DROP TABLE IF EXISTS control_policies;
DROP TABLE IF EXISTS control_branches;
DO $down$ BEGIN IF to_regclass('schema_migrations') IS NOT NULL THEN DELETE FROM schema_migrations WHERE version=3; END IF; END $down$;
COMMIT;
