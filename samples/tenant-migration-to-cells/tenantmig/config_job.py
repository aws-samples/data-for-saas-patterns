"""Per-org config + permissions migration into the V2 management DB, in SQL via postgres_fdw.

- connector_configs (V1 messaging) -> connectors (V2 management). Credentials are NOT
  copied: each connector gets a secret_ref, and credentials move to Secrets Manager
  through a separate, audited path.
- users/roles/role_permissions (V1 auth) -> principals + grants: wildcards expanded and
  explicit denies subtracted, so V2 holds allow-only grants.
"""
import psycopg
from psycopg import sql

from tenantmig import settings

SETUP = """
CREATE SCHEMA IF NOT EXISTS v1_messaging;
CREATE SCHEMA IF NOT EXISTS v1_auth;
"""

# Resource/action catalogue is the set of concrete (non-wildcard) values the org uses.
GRANTS_SQL = """
WITH perms AS (
    SELECT ur.user_id, rp.resource, rp.action, rp.effect
    FROM v1_auth.user_roles ur
    JOIN v1_auth.roles r ON r.id = ur.role_id AND r.org_id = %(org)s
    JOIN v1_auth.role_permissions rp ON rp.role_id = r.id
),
resources AS (SELECT DISTINCT resource FROM perms WHERE resource <> '*'),
actions   AS (SELECT DISTINCT action   FROM perms WHERE action   <> '*'),
universe  AS (SELECT u.id AS user_id, res.resource, act.action
              FROM v1_auth.users u, resources res, actions act WHERE u.org_id = %(org)s),
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
ON CONFLICT DO NOTHING
"""


def _ensure_fdw(conn) -> None:
    conn.execute(SETUP)
    for server, schema, opts in (("v1_messaging_srv", "v1_messaging", settings.FDW_V1_MESSAGING),
                                 ("v1_auth_srv", "v1_auth", settings.FDW_V1_AUTH)):
        exists = conn.execute("SELECT 1 FROM pg_foreign_server WHERE srvname = %s", (server,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE SERVER {} FOREIGN DATA WRAPPER postgres_fdw "
                                 "OPTIONS (host {}, port {}, dbname {}, sslmode {})").format(
                sql.Identifier(server), sql.Literal(opts["host"]), sql.Literal(str(opts["port"])),
                sql.Literal(opts["dbname"]), sql.Literal(opts["sslmode"])))
            conn.execute(sql.SQL("CREATE USER MAPPING FOR CURRENT_USER SERVER {} "
                                 "OPTIONS (user {}, password {})").format(
                sql.Identifier(server), sql.Literal(opts["user"]), sql.Literal(opts["password"])))
            conn.execute(sql.SQL("IMPORT FOREIGN SCHEMA public FROM SERVER {} INTO {}").format(
                sql.Identifier(server), sql.Identifier(schema)))


def migrate_org(org_id: str, inject_bug: bool = False) -> dict:
    """inject_bug=True ignores wildcard denies, to show the diff harness catching it."""
    deny_filter = "AND p.resource <> '*'" if inject_bug else ""
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as conn:
        _ensure_fdw(conn)
        conn.execute("""
            INSERT INTO organisations SELECT id, name, created_at
            FROM v1_messaging.organisations WHERE id = %s ON CONFLICT DO NOTHING""", (org_id,))
        connectors = conn.execute("""
            INSERT INTO connectors (id, org_id, connector_type, settings, secret_ref)
            SELECT id, org_id, connector_type, settings,
                   'tenantmig/' || org_id || '/connector/' || id
            FROM v1_messaging.connector_configs WHERE org_id = %s
            ON CONFLICT DO NOTHING""", (org_id,)).rowcount
        principals = conn.execute("""
            INSERT INTO principals SELECT id, org_id, email
            FROM v1_auth.users WHERE org_id = %s ON CONFLICT DO NOTHING""", (org_id,)).rowcount
        conn.execute("""
            INSERT INTO id_map (entity, old_id, new_id)
            SELECT 'principal', id::text, id::text FROM v1_auth.users WHERE org_id = %s
            ON CONFLICT DO NOTHING""", (org_id,))
        conn.execute("DELETE FROM grants g USING principals p WHERE g.principal_id = p.id AND p.org_id = %s",
                     (org_id,))
        grants = conn.execute(GRANTS_SQL.format(deny_filter=deny_filter), {"org": org_id}).rowcount
    return {"org_id": org_id, "connectors": connectors, "principals": principals, "grants": grants}
