BEGIN;
DROP TABLE IF EXISTS rider_incident_audit;
DROP TABLE IF EXISTS rider_incident_cases;
DROP TABLE IF EXISTS rider_coverage_snapshots;
DO $down$ BEGIN
 IF to_regclass('schema_migrations') IS NOT NULL THEN
  EXECUTE 'DELETE FROM schema_migrations WHERE version=4';
 END IF;
END $down$;
COMMIT;
