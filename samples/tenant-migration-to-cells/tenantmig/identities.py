"""Who connects, and with which role. Every identity belongs to one tenant.

Each tenant has three login roles, and each login can only reach that tenant's rows:

- tenant_<org>:    reads and writes the tenant's data on V1 and in the cell.
- migrator_<org>:  writes the tenant's configuration into the management database. It reads V1
                   configuration through postgres_fdw, as the tenant's own login, so the V1
                   row-level policies apply to it too.
- control_<org>:   writes the tenant's routing row (tenant_placement).

The three passwords sit in one secret per tenant, `tenantmig/<org>`. On AWS, a task role per
tenant can read only that secret, so a job for one tenant never holds another tenant's logins.

Setup (the owner) creates the group roles and their grants. Provisioning creates a tenant's logins.
A step fails if its password is missing. Only provisioning creates one.
"""
import json
import os
import secrets
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql

from tenantmig import settings

GROUP_TENANT = "tenantmig_tenant"
GROUP_MIGRATOR = "tenantmig_migrator"
GROUP_CONTROL = "tenantmig_control"

# Privileges per database, by group role. Anything not listed is denied. Each login is a member of
# its group, and the row-level policies narrow it to one tenant.
GRANTS = {
    "v1_messaging": {GROUP_TENANT: [
        "SELECT, INSERT, UPDATE, DELETE ON organisations, messages",
        "SELECT (id, org_id, connector_type, settings), DELETE ON connector_configs",
    ]},
    "v1_auth": {GROUP_TENANT: [
        "SELECT, INSERT, UPDATE, DELETE ON users, roles, user_roles, role_permissions",
        "SELECT ON catalogue_resources, catalogue_actions",
    ]},
    "v2_cell_messaging": {GROUP_TENANT: [
        "USAGE ON SCHEMA migration",
        "SELECT, INSERT, UPDATE, DELETE ON organisations, messages",
        "SELECT, INSERT, UPDATE, DELETE ON migration.org_state",
    ]},
    "v2_management": {
        GROUP_TENANT: ["SELECT ON organisations, connectors, principals, grants, id_map"],
        GROUP_MIGRATOR: ["SELECT, INSERT, UPDATE, DELETE ON organisations, connectors, principals, grants, id_map"],
        GROUP_CONTROL: ["SELECT, INSERT, UPDATE ON tenant_placement",
                        "SELECT, INSERT ON tenant_placement_log",
                        "USAGE ON SEQUENCE tenant_placement_log_id_seq"],
    },
}


def tenant_role(org_id: str) -> str:
    return "tenant_" + uuid.UUID(str(org_id)).hex


def migrator_role(org_id: str) -> str:
    return "migrator_" + uuid.UUID(str(org_id)).hex


def control_role(org_id: str) -> str:
    return "control_" + uuid.UUID(str(org_id)).hex


# ---- secrets -------------------------------------------------------------------------------

_LOCAL_FILE = Path(__file__).resolve().parent.parent / ".local" / "identities.json"


def secret_name(org_id: str) -> str:
    return f"tenantmig/{uuid.UUID(str(org_id))}"


def connector_secret_name(org_id: str, connector_id: int) -> str:
    """Where a connector's credentials live. The migration writes this name into `secret_ref`; the
    cutover check and the per-tenant task role use the same name."""
    return f"tenantmig/connector/{uuid.UUID(str(org_id))}/{connector_id}"


def put_secret_value(name: str, value: str) -> None:
    """Store a plain string secret: on AWS in Secrets Manager, locally in the same file as the logins."""
    if settings.SECRETS_BACKEND == "aws":
        import boto3
        from botocore.exceptions import ClientError
        sm = boto3.client("secretsmanager", region_name=settings.S3_REGION)
        try:
            sm.create_secret(Name=name, SecretString=value)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ResourceExistsException":
                raise
            sm.put_secret_value(SecretId=name, SecretString=value)
        return
    data = _load_local()
    data[name] = value
    _LOCAL_FILE.parent.mkdir(exist_ok=True)
    _LOCAL_FILE.write_text(json.dumps(data, indent=2))
    _LOCAL_FILE.chmod(0o600)


def _load_local() -> dict:
    return json.loads(_LOCAL_FILE.read_text()) if _LOCAL_FILE.exists() else {}


def _get_bundle(org_id: str) -> dict | None:
    name = secret_name(org_id)
    if settings.SECRETS_BACKEND == "aws":
        import boto3
        from botocore.exceptions import ClientError
        try:
            raw = boto3.client("secretsmanager", region_name=settings.S3_REGION).get_secret_value(SecretId=name)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise
        return json.loads(raw["SecretString"])
    return _load_local().get(name)


def _put_bundle(org_id: str, bundle: dict) -> None:
    name, value = secret_name(org_id), json.dumps(bundle)
    if settings.SECRETS_BACKEND == "aws":
        import boto3
        sm = boto3.client("secretsmanager", region_name=settings.S3_REGION)
        if _get_bundle(org_id) is None:
            sm.create_secret(Name=name, SecretString=value)
        else:
            sm.put_secret_value(SecretId=name, SecretString=value)
        return
    data = _load_local()
    data[name] = bundle
    _LOCAL_FILE.parent.mkdir(exist_ok=True)
    _LOCAL_FILE.write_text(json.dumps(data, indent=2))
    _LOCAL_FILE.chmod(0o600)


def _passwords(org_id: str, create: bool) -> dict:
    bundle = _get_bundle(org_id)
    if bundle is None:
        if not create:
            raise SystemExit(f"No logins for tenant {org_id}. Provision it first (generate creates them).")
        bundle = {k: secrets.token_urlsafe(24) for k in ("tenant", "migrator", "control")}
        _put_bundle(org_id, bundle)
    return bundle


# ---- connection strings -----------------------------------------------------------------------

def as_user(dsn: str, user: str, password: str) -> str:
    """The same database and options, connecting as another role."""
    parts = urlsplit(dsn)
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def tenant_dsn(base_dsn: str, org_id: str) -> str:
    return as_user(base_dsn, tenant_role(org_id), _passwords(org_id, create=False)["tenant"])


def migrator_dsn(base_dsn: str, org_id: str) -> str:
    return as_user(base_dsn, migrator_role(org_id), _passwords(org_id, create=False)["migrator"])


def control_dsn(base_dsn: str, org_id: str) -> str:
    return as_user(base_dsn, control_role(org_id), _passwords(org_id, create=False)["control"])


# ---- setup (admin) ------------------------------------------------------------------------------

def setup_roles(dsns: dict[str, str]) -> None:
    """Admin step: the group roles, and their grants on every database. Idempotent.

    `dsns` maps each database label used in GRANTS to its admin DSN. The objects the grants name
    must already exist (placement and the fdw schemas are created before this runs).
    """
    for label, dsn in dsns.items():
        with psycopg.connect(dsn) as conn:
            for group in GRANTS[label]:
                conn.execute(f"""DO $$ BEGIN CREATE ROLE {group} NOLOGIN;
                                 EXCEPTION WHEN duplicate_object THEN NULL; END $$""")
            for group, statements in GRANTS[label].items():
                for statement in statements:
                    # The statements are constants from GRANTS above, never user input.
                    conn.execute(sql.SQL("GRANT " + statement + " TO {}").format(sql.Identifier(group)))
            conn.commit()


def _create_login(conn, role: str, password: str, group: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    verb = "ALTER ROLE {} LOGIN PASSWORD {}" if exists else "CREATE ROLE {} LOGIN PASSWORD {}"
    conn.execute(sql.SQL(verb).format(sql.Identifier(role), sql.Literal(password)))
    conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(role)))
    conn.commit()


def provision_tenant(org_id: str) -> None:
    """Admin step: create (or re-password) this tenant's three logins. Idempotent.

    The migrator reads V1 through a mapping that uses the tenant's own login, so the mapping
    is refreshed here too. That is what makes a password rotation take effect.
    """
    from tenantmig import config_job
    passwords = _passwords(org_id, create=True)
    for dsn in (settings.V1_MESSAGING_DSN, settings.V1_AUTH_DSN, settings.V2_CELL_MESSAGING_DSN):
        with psycopg.connect(dsn) as conn:
            _create_login(conn, tenant_role(org_id), passwords["tenant"], GROUP_TENANT)
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as conn:
        _create_login(conn, tenant_role(org_id), passwords["tenant"], GROUP_TENANT)
        _create_login(conn, migrator_role(org_id), passwords["migrator"], GROUP_MIGRATOR)
        _create_login(conn, control_role(org_id), passwords["control"], GROUP_CONTROL)
        config_job.map_tenant_for_migrator(conn, org_id, passwords["tenant"])
        conn.commit()
    if settings.SECRETS_BACKEND == "aws":
        from tenantmig import task_roles
        task_roles.ensure_task_role(org_id)
