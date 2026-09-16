#!/bin/sh
set -eu
for file in /migrations/[0-9][0-9][0-9][0-9]_*.sql; do
  case "$file" in *.down.sql) continue ;; esac
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$file"
done
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE IF NOT EXISTS local_stack_seed (
  seed_id text PRIMARY KEY,
  payload jsonb NOT NULL CHECK (payload ? 'config_ref')
);
INSERT INTO local_stack_seed(seed_id, payload) VALUES
 ('hq-synthetic', '{"kind":"HQ","config_ref":"config://seed/hq"}'),
 ('regional-synthetic', '{"kind":"REGIONAL","config_ref":"config://seed/regional"}'),
 ('local-synthetic', '{"kind":"LOCAL","config_ref":"config://seed/local"}'),
 ('merchant-synthetic', '{"kind":"MERCHANT","config_ref":"config://seed/merchant"}'),
 ('rider-synthetic', '{"kind":"RIDER","config_ref":"config://seed/rider"}'),
 ('customer-synthetic', '{"kind":"CUSTOMER","config_ref":"config://seed/customer"}')
ON CONFLICT DO NOTHING;
SQL
