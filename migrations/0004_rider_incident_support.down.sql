BEGIN;
DROP TABLE IF EXISTS rider_incident_audit;
DROP TABLE IF EXISTS rider_incident_cases;
DROP TABLE IF EXISTS rider_coverage_snapshots;
DELETE FROM schema_migrations WHERE version=4;
COMMIT;
