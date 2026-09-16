BEGIN;
DROP TABLE IF EXISTS notification_audit;
DROP TABLE IF EXISTS notification_callback_receipts;
DROP TABLE IF EXISTS notification_outbox;
DROP TABLE IF EXISTS notification_preferences;
DO $down$ BEGIN IF to_regclass('schema_migrations') IS NOT NULL THEN EXECUTE 'DELETE FROM schema_migrations WHERE version=5'; END IF; END $down$;
COMMIT;
