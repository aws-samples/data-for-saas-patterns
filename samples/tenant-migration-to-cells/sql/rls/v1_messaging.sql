-- Row-level security on V1 messaging (finding 4). See tenantmig/tenant_scope.py.
-- A session sees a tenant's rows only as that tenant's login role. See identity.sql.
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

ALTER TABLE connector_configs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON connector_configs;
CREATE POLICY tenant_isolation ON connector_configs
    USING (org_id = tenantmig_current_org())
    WITH CHECK (org_id = tenantmig_current_org());

-- converted for every tenant in one statement. It can only see the columns granted to it
-- (no credentials), and it has no write access.
