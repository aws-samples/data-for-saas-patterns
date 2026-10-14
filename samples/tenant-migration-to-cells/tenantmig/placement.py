"""Tenant placement: the routing source of truth, and the guard every write step uses.

`tenant_placement` in the management database says which platform serves each tenant. The
request router reads it. (A DynamoDB table, as in the sharding reference architecture, works
equally well.)

    v1 ──cutover──▶ frozen ──verified──▶ v2 ──forget──▶ (V1 copy deleted)
                      │
                      └──failed or interrupted──▶ v1   (V1 was authoritative throughout)

Every step that writes into V2 calls `require_migratable` first. Once a tenant is `v2` it is
live in its cell, and a migration step would overwrite live data with stale V1 data (or, after
`forget`, with nothing at all).
"""
import pathlib

import psycopg

from tenantmig import settings

DDL = (pathlib.Path(__file__).resolve().parent / "placement.sql").read_text()

MIGRATABLE = ("v1", "frozen")       # frozen means a previous cutover was interrupted


def normalise_org_id(org_id: str) -> str:
    """Canonical lowercase UUID form.

    PostgreSQL compares uuid values regardless of the text case, but S3 keys and bucket names
    are case-sensitive and bucket names must be lowercase. An uppercase id on the command line
    would otherwise look at the wrong S3 prefix and report "nothing to copy".
    """
    import uuid
    try:
        return str(uuid.UUID(str(org_id)))
    except (ValueError, AttributeError, TypeError):
        return str(org_id)


def ensure(conn) -> None:
    conn.execute(DDL)


def get_status(conn, org_id: str) -> tuple[str, str | None]:
    ensure(conn)
    row = conn.execute("SELECT status, cell FROM tenant_placement WHERE org_id = %s", (org_id,)).fetchone()
    return row if row else ("v1", None)


def set_status(conn, org_id: str, status: str, cell: str | None, note: str) -> None:
    conn.execute("""
        INSERT INTO tenant_placement (org_id, status, cell) VALUES (%s, %s, %s)
        ON CONFLICT (org_id) DO UPDATE SET status = EXCLUDED.status, cell = EXCLUDED.cell, updated_at = now()""",
                 (org_id, status, cell))
    conn.execute("INSERT INTO tenant_placement_log (org_id, status, cell, note) VALUES (%s, %s, %s, %s)",
                 (org_id, status, cell, note))
    conn.commit()


def require_migratable(org_id: str, what: str) -> str:
    """Refuse to run a migration step against a tenant that is already live in its cell."""
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as conn:
        status, cell = get_status(conn, org_id)
    if status not in MIGRATABLE:
        raise SystemExit(
            f"Refusing to {what} for {org_id}: it is {status}"
            + (f" in {cell}" if cell else "")
            + ". The tenant is live in its cell, so this would overwrite live data with V1 data. "
              "Migrate it back to v1 deliberately if that is really what you want.")
    return status
