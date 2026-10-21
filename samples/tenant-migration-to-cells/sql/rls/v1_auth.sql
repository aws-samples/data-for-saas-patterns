-- Row-level security on V1 auth (finding 4). Role permissions and user roles have no org_id,
-- so they're scoped through the users or roles row they belong to.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON users;
CREATE POLICY tenant_isolation ON users
    USING (org_id = tenantmig_current_org())
    WITH CHECK (org_id = tenantmig_current_org());

ALTER TABLE roles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON roles;
CREATE POLICY tenant_isolation ON roles
    USING (org_id = tenantmig_current_org())
    WITH CHECK (org_id = tenantmig_current_org());

ALTER TABLE user_roles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON user_roles;
CREATE POLICY tenant_isolation ON user_roles
    USING (EXISTS (SELECT 1 FROM users u WHERE u.id = user_roles.user_id))
    WITH CHECK (EXISTS (SELECT 1 FROM users u WHERE u.id = user_roles.user_id));

ALTER TABLE role_permissions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON role_permissions;
CREATE POLICY tenant_isolation ON role_permissions
    USING (EXISTS (SELECT 1 FROM roles r WHERE r.id = role_permissions.role_id))
    WITH CHECK (EXISTS (SELECT 1 FROM roles r WHERE r.id = role_permissions.role_id));
