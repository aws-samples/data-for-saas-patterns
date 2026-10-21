-- The tenant a session belongs to, taken from its login role: tenant_, migrator_ or control_ followed
-- by the 32 hex digits of the org ID. The policies compare rows with this, so a session can't choose
-- a tenant. Any other role, including the owner and the group roles, gets NULL, which matches no row.
CREATE OR REPLACE FUNCTION tenantmig_current_org() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN current_user ~ '^(tenant|migrator|control)_[0-9a-f]{32}$' THEN
        regexp_replace(substring(current_user from '[0-9a-f]{32}$'),
                       '^(.{8})(.{4})(.{4})(.{4})(.{12})$', '\1-\2-\3-\4-\5')::uuid
    END
$$;
