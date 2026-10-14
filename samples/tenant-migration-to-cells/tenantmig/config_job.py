"""Per-org config + permissions migration into the V2 management DB, in SQL via postgres_fdw.

- connector_configs (V1 messaging) -> connectors (V2 management). Credentials are NOT
  copied: each connector gets a secret_ref, and credentials move to Secrets Manager
  through a separate, audited path. The foreign table doesn't even declare the
  credentials column, and the fdw connects as a read-only role with no SELECT on it.
- users/roles/role_permissions (V1 auth) -> principals + grants: wildcards are expanded
  against the application catalogue (catalogue_resources, catalogue_actions) and explicit
  denies are subtracted, so V2 holds allow-only grants.
- Every run converges V2 on V1 for the tenant: changed rows are updated, and rows deleted
  in V1 are deleted in V2. Grants are rebuilt from scratch.
"""
import psycopg
from psycopg import sql

from tenantmig import settings

# Only these tables (and columns) are visible through the foreign-data wrapper.
FOREIGN_TABLES = {
    "v1_messaging": {
        "organisations": "id uuid, name text, created_at timestamptz",
        "connector_configs": "id bigint, org_id uuid, connector_type text, settings jsonb",
    },
    "v1_auth": {
        "users": "id bigint, org_id uuid, email text",
        "roles": "id bigint, org_id uuid, name text",
        "user_roles": "user_id bigint, role_id bigint",
        "role_permissions": "role_id bigint, resource text, action text, effect text",
        "catalogue_resources": "name text",
        "catalogue_actions": "name text",
    },
}
SERVERS = {"v1_messaging": ("v1_messaging_srv", settings.FDW_V1_MESSAGING),
           "v1_auth": ("v1_auth_srv", settings.FDW_V1_AUTH)}

GRANTS_SQL = """
WITH perms AS (
    SELECT ur.user_id, rp.resource, rp.action, rp.effect
    FROM v1_auth.user_roles ur
    JOIN v1_auth.roles r ON r.id = ur.role_id AND r.org_id = %(org)s
    JOIN v1_auth.role_permissions rp ON rp.role_id = r.id
),
universe AS (SELECT u.id AS user_id, res.name AS resource, act.name AS action
             FROM v1_auth.users u, v1_auth.catalogue_resources res, v1_auth.catalogue_actions act
             WHERE u.org_id = %(org)s),
decided AS (
    SELECT un.user_id, un.resource, un.action,
           bool_or(p.effect = 'allow') AS any_allow,
           bool_or(p.effect = 'deny' {deny_filter}) AS any_deny
    FROM universe un
    JOIN perms p ON p.user_id = un.user_id
                AND p.resource IN (un.resource, '*')
                AND p.action   IN (un.action, '*')
    GROUP BY 1, 2, 3
)
INSERT INTO grants (principal_id, resource, action)
SELECT user_id, resource, action FROM decided
WHERE any_allow AND NOT coalesce(any_deny, false)
"""


def _ensure_fdw(conn) -> None:
    for schema, tables in FOREIGN_TABLES.items():
        server, opts = SERVERS[schema]
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        if not conn.execute("SELECT 1 FROM pg_foreign_server WHERE srvname = %s", (server,)).fetchone():
            conn.execute(sql.SQL("CREATE SERVER {} FOREIGN DATA WRAPPER postgres_fdw "
                                 "OPTIONS (host {}, port {}, dbname {}, sslmode {})").format(
                sql.Identifier(server), sql.Literal(opts["host"]), sql.Literal(str(opts["port"])),
                sql.Literal(opts["dbname"]), sql.Literal(opts["sslmode"])))
            conn.execute(sql.SQL("CREATE USER MAPPING FOR CURRENT_USER SERVER {} "
                                 "OPTIONS (user {}, password {})").format(
                sql.Identifier(server), sql.Literal(opts["reader_user"]), sql.Literal(opts["reader_password"])))
        for table, columns in tables.items():
            exists = conn.execute(
                "SELECT 1 FROM pg_foreign_table ft JOIN pg_class c ON c.oid = ft.ftrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s",
                (schema, table)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE FOREIGN TABLE {}.{} ({}) SERVER {} "
                                     "OPTIONS (schema_name 'public', table_name {})").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.SQL(columns),
                    sql.Identifier(server), sql.Literal(table)))


def migrate_org(org_id: str, inject_bug: bool = False) -> dict:
    """inject_bug=True ignores wildcard denies, to show the diff harness catching it."""
    deny_filter = "AND p.resource <> '*'" if inject_bug else ""
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as conn:
        _ensure_fdw(conn)
        conn.execute("""
            INSERT INTO organisations SELECT id, name, created_at
            FROM v1_messaging.organisations WHERE id = %s
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name""", (org_id,))

        # Connectors: upsert, then delete the ones V1 no longer has.
        connectors = conn.execute("""
            INSERT INTO connectors (id, org_id, connector_type, settings, secret_ref)
            SELECT id, org_id, connector_type, settings, 'tenantmig/' || org_id || '/connector/' || id
            FROM v1_messaging.connector_configs WHERE org_id = %s
            ON CONFLICT (id) DO UPDATE SET connector_type = EXCLUDED.connector_type,
                                           settings = EXCLUDED.settings""", (org_id,)).rowcount
        connectors_removed = conn.execute("""
            DELETE FROM connectors c WHERE c.org_id = %s AND NOT EXISTS
              (SELECT 1 FROM v1_messaging.connector_configs v WHERE v.org_id = %s AND v.id = c.id)""",
                                          (org_id, org_id)).rowcount

        # Principals: grants depend on them, so clear the tenant's grants first.
        conn.execute("DELETE FROM grants g USING principals p WHERE g.principal_id = p.id AND p.org_id = %s",
                     (org_id,))
        principals = conn.execute("""
            INSERT INTO principals SELECT id, org_id, email FROM v1_auth.users WHERE org_id = %s
            ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email""", (org_id,)).rowcount
        principals_removed = conn.execute("""
            DELETE FROM principals p WHERE p.org_id = %s AND NOT EXISTS
              (SELECT 1 FROM v1_auth.users u WHERE u.org_id = %s AND u.id = p.id)""",
                                          (org_id, org_id)).rowcount
        conn.execute("""
            INSERT INTO id_map (entity, old_id, new_id)
            SELECT 'principal', id::text, id::text FROM v1_auth.users WHERE org_id = %s
            ON CONFLICT DO NOTHING""", (org_id,))
        grants = conn.execute(GRANTS_SQL.format(deny_filter=deny_filter), {"org": org_id}).rowcount
    return {"org_id": org_id, "connectors": connectors, "connectors_removed": connectors_removed,
            "principals": principals, "principals_removed": principals_removed, "grants": grants}
