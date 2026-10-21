"""Tenant placement: the routing source of truth, and the guard every write step uses.

`tenant_placement` in the management database says which platform serves each tenant. The
request router reads it. (A DynamoDB table, as in the sharding reference architecture, works
equally well.)

    v1 ──cutover──▶ frozen ──verified──▶ v2 ──forget──▶ (V1 copy deleted)
                      │
                      └──failed or interrupted──▶ v1   (V1 was authoritative throughout)

Every step that writes into V2 is wrapped in `@guarded`. Once a tenant is `v2` it is live in its
cell, and a migration step would overwrite live data with stale V1 data (or, after `forget`,
with nothing at all).
"""
import contextvars
import functools
import pathlib

import psycopg

from tenantmig import identities, settings

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


# Set by cutover while it holds the tenant's lock, so the steps it calls don't try to take it again.
HELD_BY_CUTOVER = contextvars.ContextVar("held_by_cutover", default=False)
LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('cutover:' || %s))"


def _check(status: str, cell: str | None, org_id: str, what: str) -> str:
    if status not in MIGRATABLE:
        raise SystemExit(
            f"Refusing to {what} for {org_id}: it is {status}"
            + (f" in {cell}" if cell else "")
            + ". The tenant is live in its cell, so this would overwrite live data with V1 data. "
              "Migrate it back to v1 deliberately if that is really what you want.")
    return status


def require_migratable(org_id: str, what: str) -> str:
    """Refuse to run a migration step against a tenant that is already live in its cell."""
    with psycopg.connect(identities.control_dsn(settings.V2_MANAGEMENT_DSN, org_id)) as conn:
        status, cell = get_status(conn, org_id)
    return _check(status, cell, org_id, what)


def guarded(what: str):
    """Run a write step only while the tenant is migratable, and hold its cutover lock for the whole step.

    Checking the status once and then writing is a race: a cutover can start, freeze and redirect
    the tenant in between. Taking the same advisory lock cutover holds closes that window, and
    cutover itself is refused while a step is running. A step that starts after a crashed
    cutover finds the tenant `frozen`, which is still migratable, and proceeds.
    """
    def wrap(fn):
        @functools.wraps(fn)
        def inner(org_id, *args, **kwargs):
            if HELD_BY_CUTOVER.get():
                require_migratable(org_id, what)
                return fn(org_id, *args, **kwargs)
            with psycopg.connect(identities.control_dsn(settings.V2_MANAGEMENT_DSN, org_id)) as conn:
                if not conn.execute(LOCK_SQL, (org_id,)).fetchone()[0]:
                    raise SystemExit(f"Refusing to {what} for {org_id}: a cutover or another step is already running for it.")
                status, cell = get_status(conn, org_id)
                conn.commit()                 # end the transaction; the session lock survives it
                _check(status, cell, org_id, what)
                return fn(org_id, *args, **kwargs)     # the lock is released when the connection closes
        return inner
    return wrap
