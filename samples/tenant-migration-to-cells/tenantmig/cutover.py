"""Flip, Redirect and Forget for one tenant.

`tenant_placement` in the management database is the routing source of truth: the request
router reads it to decide whether a tenant is served by V1 or by a cell. (A DynamoDB table,
as in the sharding reference architecture, works equally well.)

    v1 ──cutover──▶ frozen ──verified──▶ v2 ──forget──▶ (V1 copy deleted)
                      │
                      └──verification failed──▶ v1   (V1 was authoritative throughout)
"""
import time

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


def cutover(org_id: str, cell: str | None = None, drain_seconds: int | None = None) -> dict:
    """Flip and Redirect: freeze writes, drain, final delta, verify, then route the tenant to its cell.

    - Only a tenant on `v1` (or `frozen`, left by a crashed run) can be cut over, and only one
      cutover per tenant runs at a time (advisory lock).
    - After freezing, waits `drain_seconds` (default PLACEMENT_CACHE_TTL_SECONDS): routers that
      cache tenant_placement may accept V1 writes until their cache refreshes, and those writes
      must land before the final pass. Set it to your router's cache TTL plus a margin, or to 0
      if writes are blocked on the V1 side (for example by revoking the tenant's write role).
    - The V1 copy stays authoritative until the last step. If verification fails, or any step
      raises, the tenant goes back to `v1`. Nothing has to be undone in V1.
    """
    cell = cell or settings.CELL_ID
    drain = settings.PLACEMENT_CACHE_TTL_SECONDS if drain_seconds is None else drain_seconds
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as mgmt:
        _ensure(mgmt)
        if not mgmt.execute("SELECT pg_try_advisory_lock(hashtext('cutover:' || %s))", (org_id,)).fetchone()[0]:
            raise SystemExit(f"Another cutover is already running for {org_id}.")
        row = mgmt.execute("SELECT status FROM tenant_placement WHERE org_id = %s", (org_id,)).fetchone()
        status = row[0] if row else "v1"
        mgmt.commit()
        if status not in ("v1", "frozen"):
            raise SystemExit(f"Refusing to cut over {org_id}: it is already {status}. "
                             "Re-running cutover on a live V2 tenant would route it back to V1.")

        _set(mgmt, org_id, "frozen", None, f"write freeze; draining {drain}s for router caches")
        try:
            time.sleep(drain)
            final = content_job.migrate_org(org_id, final=True)    # no safety margin, plus anti-join
            config = config_job.migrate_org(org_id)                # converges config and permissions on V1
            r = reconcile.reconcile_org(org_id)
            objects = None
            if not r["all_shared_objects_copied"]:
                objects = object_job.migrate_objects(org_id)
                r = reconcile.reconcile_org(org_id)
            d = perm_diff.diff_org(org_id)
        except Exception as e:                                     # any failure: back to V1
            _set(mgmt, org_id, "v1", None, f"rolled back: {type(e).__name__}: {e}"[:500])
            return {"org_id": org_id, "cut_over": False, "status": "v1",
                    "reasons": [f"{type(e).__name__}: {e}"]}

        problems = (["reconciliation failed"] if not r["passed"] else []) + \
                   ([f"{len(d['disagreements'])} permission disagreements"] if d["disagreements"] else [])
        if problems:
            _set(mgmt, org_id, "v1", None, "rolled back: " + ", ".join(problems))
            return {"org_id": org_id, "cut_over": False, "status": "v1", "reasons": problems,
                    "final_delta_rows": final.rows, "backfilled_rows": final.backfilled}

        _set(mgmt, org_id, "v2", cell, f"verified and redirected to {cell}")
    return {"org_id": org_id, "cut_over": True, "status": "v2", "cell": cell, "drained_seconds": drain,
            "final_delta_rows": final.rows, "backfilled_rows": final.backfilled, "grants": config["grants"],
            "objects_recopied": bool(objects), "reconciled_rows": r["v2_rows"]}


FORGET_CHUNK = 10_000


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
        auth.commit()
        # Large tenants: delete messages in committed chunks rather than one huge transaction.
        while msg.execute("DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE org_id = %s LIMIT %s)",
                          (org_id, FORGET_CHUNK)).rowcount:
            msg.commit()
        msg.execute("DELETE FROM connector_configs WHERE org_id = %s", (org_id,))
        msg.execute("DELETE FROM organisations WHERE id = %s", (org_id,))
    for i in range(0, len(keys), 1000):
        resp = store.s3.delete_objects(Bucket=settings.V1_SHARED_BUCKET,
                                       Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
        if resp.get("Errors"):
            raise RuntimeError(f"forget: {len(resp['Errors'])} objects not deleted (re-run forget): {resp['Errors'][:3]}")

    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as mgmt:
        mgmt.execute("INSERT INTO tenant_placement_log (org_id, status, cell, note) "
                     "SELECT org_id, status, cell, 'V1 copy deleted (forget)' FROM tenant_placement WHERE org_id = %s",
                     (org_id,))
    return {"org_id": org_id, "dry_run": False, "deleted": counts}
