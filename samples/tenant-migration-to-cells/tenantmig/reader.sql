-- Read-only migration role for the V1 databases (run by `tenantmig init`).
-- postgres_fdw in the management database connects as this role, never as the master user.
-- On the messaging database it can't read connector_configs.credentials.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tenantmig_reader') THEN
    CREATE ROLE tenantmig_reader LOGIN;
  END IF;
END $$;
