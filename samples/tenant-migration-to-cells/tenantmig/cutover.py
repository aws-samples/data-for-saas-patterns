"""Flip, Redirect and Forget for one tenant.

`tenant_placement` in the management database is the routing source of truth: the request
router reads it to decide whether a tenant is served by V1 or by a cell. (A DynamoDB table,
as in the sharding reference architecture, works equally well.)

    v1 ──cutover──▶ frozen ──verified──▶ v2 ──forget──▶ (V1 copy deleted)
                      │
                      └──verification failed──▶ v1   (V1 was authoritative throughout)
"""
import psycopg

from tenantmig import content_job, config_job, object_job, perm_diff, reconcile, settings
from tenantmig.store import ContentStore


PLACEMENT_DDL = (__import__("pathlib").Path(__file__).resolve().parent / "placement.sql").read_text()


def _ensure(conn) -> None:
    conn.execute(PLACEMENT_DDL)


def _set(conn, org_id: str, status: str, cell: str | None, note: str) -> None:
    conn.execute("""
        INSERT INTO tenant_placement (org_id, status, cell) VALUES (%s, %s, %s)
        ON CONFLICT (org_id) DO UPDATE SET status = EXCLUDED.status, cell = EXCLUDED.cell, updated_at = now()""",
                 (org_id, status, cell))
    conn.execute("INSERT INTO tenant_placement_log (org_id, status, cell, note) VALUES (%s, %s, %s, %s)",
                 (org_id, status, cell, note))
    conn.commit()


def where(org_id: str) -> dict:
    """What the router would do for this tenant."""
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as c:
        _ensure(c)
        row = c.execute("SELECT status, cell, updated_at FROM tenant_placement WHERE org_id = %s", (org_id,)).fetchone()
        log = c.execute("SELECT status, cell, note, at FROM tenant_placement_log WHERE org_id = %s ORDER BY id",
                        (org_id,)).fetchall()
    status, cell, updated = row if row else ("v1", None, None)
    route = {"v1": "V1 platform", "frozen": "V1 platform, writes paused", "v2": f"cell {cell}"}[status]
    return {"org_id": org_id, "status": status, "cell": cell, "routes_to": route, "updated_at": updated,
            "history": [dict(status=s, cell=cl, note=n, at=t) for s, cl, n, t in log]}


def cutover(org_id: str, cell: str | None = None) -> dict:
    """Flip and Redirect: freeze writes, final delta, verify, then route the tenant to its cell.

    The V1 copy stays authoritative until the last step, so a failed verification is rolled
    back by setting the tenant back to `v1`. Nothing has to be undone in V1.
    """
    cell = cell or settings.CELL_ID
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as mgmt:
        _ensure(mgmt)
        _set(mgmt, org_id, "frozen", None, "write freeze: router stops V1 writes for this tenant")

        final = content_job.migrate_org(org_id)                 # catch everything written before the freeze
        config = config_job.migrate_org(org_id)                 # idempotent re-run picks up late config changes
        r = reconcile.reconcile_org(org_id)
        objects = None
        if not r["all_shared_objects_copied"]:
            objects = object_job.migrate_objects(org_id)
            r = reconcile.reconcile_org(org_id)
        d = perm_diff.diff_org(org_id)

        problems = (["reconciliation failed"] if not r["passed"] else []) + \
                   ([f"{len(d['disagreements'])} permission disagreements"] if d["disagreements"] else [])
        if problems:
            _set(mgmt, org_id, "v1", None, "rolled back: " + ", ".join(problems))
            return {"org_id": org_id, "cut_over": False, "status": "v1", "reasons": problems,
                    "final_delta_rows": final.rows}

        _set(mgmt, org_id, "v2", cell, f"verified and redirected to {cell}")
    return {"org_id": org_id, "cut_over": True, "status": "v2", "cell": cell,
            "final_delta_rows": final.rows, "grants": config["grants"],
            "objects_recopied": bool(objects), "reconciled_rows": r["v2_rows"]}


def forget(org_id: str, confirm: bool = False, store: ContentStore | None = None) -> dict:
    """Forget: delete the tenant's V1 copy. Only after cutover, and only after your retention window.

    Without confirm=True this is a dry run that reports what would be deleted.
    """
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as mgmt:
        _ensure(mgmt)
        row = mgmt.execute("SELECT status FROM tenant_placement WHERE org_id = %s", (org_id,)).fetchone()
    if not row or row[0] != "v2":
        raise SystemExit(f"Refusing to forget {org_id}: it is not cut over (status {row[0] if row else 'v1'}).")

    store = store or ContentStore()
    keys = []
    for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=settings.V1_SHARED_BUCKET, Prefix=f"{org_id}/"):
        keys += [o["Key"] for o in page.get("Contents", [])]

    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg, psycopg.connect(settings.V1_AUTH_DSN) as auth:
        counts = {
            "messages": msg.execute("SELECT count(*) FROM messages WHERE org_id = %s", (org_id,)).fetchone()[0],
            "connector_configs": msg.execute("SELECT count(*) FROM connector_configs WHERE org_id = %s", (org_id,)).fetchone()[0],
            "users": auth.execute("SELECT count(*) FROM users WHERE org_id = %s", (org_id,)).fetchone()[0],
            "roles": auth.execute("SELECT count(*) FROM roles WHERE org_id = %s", (org_id,)).fetchone()[0],
            "shared_bucket_objects": len(keys),
        }
        if not confirm:
            return {"org_id": org_id, "dry_run": True, "would_delete": counts}

        auth.execute("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE org_id = %s)", (org_id,))
        auth.execute("DELETE FROM role_permissions WHERE role_id IN (SELECT id FROM roles WHERE org_id = %s)", (org_id,))
        auth.execute("DELETE FROM roles WHERE org_id = %s", (org_id,))
        auth.execute("DELETE FROM users WHERE org_id = %s", (org_id,))
        msg.execute("DELETE FROM messages WHERE org_id = %s", (org_id,))
        msg.execute("DELETE FROM connector_configs WHERE org_id = %s", (org_id,))
        msg.execute("DELETE FROM organisations WHERE id = %s", (org_id,))
    for i in range(0, len(keys), 1000):
        store.s3.delete_objects(Bucket=settings.V1_SHARED_BUCKET,
                                Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})

    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as mgmt:
        mgmt.execute("INSERT INTO tenant_placement_log (org_id, status, cell, note) "
                     "SELECT org_id, status, cell, 'V1 copy deleted (forget)' FROM tenant_placement WHERE org_id = %s",
                     (org_id,))
    return {"org_id": org_id, "dry_run": False, "deleted": counts}
