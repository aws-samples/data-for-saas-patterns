-- Row-level security on the V2 cell messaging database (finding 4).
ALTER TABLE organisations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON organisations;
CREATE POLICY tenant_isolation ON organisations
    USING (id = tenantmig_current_org())
    WITH CHECK (id = tenantmig_current_org());

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON messages;
CREATE POLICY tenant_isolation ON messages
    USING (org_id = tenantmig_current_org())
    WITH CHECK (org_id = tenantmig_current_org());

ALTER TABLE migration.org_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON migration.org_state;
CREATE POLICY tenant_isolation ON migration.org_state
    USING (org_id = tenantmig_current_org())
    WITH CHECK (org_id = tenantmig_current_org());
