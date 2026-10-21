-- Row-level security on the V2 management database (finding 4). The tenant, migrator and control
-- logins of one tenant all see only that tenant's rows (see identity.sql).
ALTER TABLE organisations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON organisations;
CREATE POLICY tenant_isolation ON organisations
    USING (id = tenantmig_current_org())
    WITH CHECK (id = tenantmig_current_org());

ALTER TABLE connectors ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON connectors;
CREATE POLICY tenant_isolation ON connectors
    USING (org_id = tenantmig_current_org())
    WITH CHECK (org_id = tenantmig_current_org());

ALTER TABLE principals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON principals;
CREATE POLICY tenant_isolation ON principals
    USING (org_id = tenantmig_current_org())
    WITH CHECK (org_id = tenantmig_current_org());

ALTER TABLE grants ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON grants;
CREATE POLICY tenant_isolation ON grants
    USING (EXISTS (SELECT 1 FROM principals p WHERE p.id = grants.principal_id))
    WITH CHECK (EXISTS (SELECT 1 FROM principals p WHERE p.id = grants.principal_id));

ALTER TABLE id_map ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON id_map;
CREATE POLICY tenant_isolation ON id_map
    USING (org_id = tenantmig_current_org())
    WITH CHECK (org_id = tenantmig_current_org());


ALTER TABLE tenant_placement ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenant_placement;
CREATE POLICY tenant_isolation ON tenant_placement
    USING (org_id = tenantmig_current_org()) WITH CHECK (org_id = tenantmig_current_org());

ALTER TABLE tenant_placement_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenant_placement_log;
CREATE POLICY tenant_isolation ON tenant_placement_log
    USING (org_id = tenantmig_current_org()) WITH CHECK (org_id = tenantmig_current_org());
