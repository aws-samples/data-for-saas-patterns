"""Flip, Redirect and Forget for one tenant.

`tenant_placement` in the management database is the routing source of truth: the request
router reads it to decide whether a tenant is served by V1 or by a cell. (A DynamoDB table,
as in the sharding reference architecture, works equally well.)

    v1 ──cutover──▶ frozen ──verified──▶ v2 ──forget──▶ (V1 copy deleted)
                      │
                      └──verification failed──▶ v1   (V1 was authoritative throughout)
"""
import time
from datetime import datetime, timezone

import psycopg

from tenantmig import content_job, config_job, object_job, perm_diff, placement, reconcile, settings
from tenantmig.store import ContentStore

PLACEMENT_DDL = placement.DDL
_ensure = placement.ensure
_set = placement.set_status


def where(org_id: str) -> dict:
    """What the router would do for this tenant."""
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as c:
        _ensure(c)
        row = c.execute("SELECT status, cell, updated_at FROM tenant_placement WHERE org_id = %s", (org_id,)).fetchone()
        log = c.execute("SELECT status, cell, note, at FROM tenant_placement_log WHERE org_id = %s ORDER BY id",
                        (org_id,)).fetchall()
    status, cell, updated = row if row else ("v1", None, None)
    route = {"v1": "V1 platform", "frozen": "V1 platform, writes paused", "v2": f"cell {cell}"}[status]
    out = {"org_id": org_id, "status": status, "cell": cell, "routes_to": route, "updated_at": updated}
    if status == "frozen" and updated is not None:
        # Alarm on this in production: a tenant stuck frozen has its writes paused.
        out["frozen_for_seconds"] = int((datetime.now(timezone.utc) - updated).total_seconds())
    out["history"] = [dict(status=s, cell=cl, note=n, at=t) for s, cl, n, t in log]
    return out


def _missing_secrets(org_id: str) -> list[str]:
    """Check that every connector's secret_ref actually resolves in Secrets Manager.

    `forget` deletes the V1 `credentials` column for good, so a connector whose secret was
    never created would be unrecoverable. Skipped locally (no Secrets Manager) and when
    CHECK_CONNECTOR_SECRETS is off.
    """
    if not settings.CHECK_CONNECTOR_SECRETS:
        return []
    import boto3
    from botocore.exceptions import ClientError
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as conn:
        refs = [r[0] for r in conn.execute("SELECT secret_ref FROM connectors WHERE org_id = %s", (org_id,))]
    sm = boto3.client("secretsmanager", region_name=settings.S3_REGION)
    missing = []
    for ref in refs:
        try:
            sm.describe_secret(SecretId=ref)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("ResourceNotFoundException", "AccessDeniedException"):
                missing.append(ref)
            else:
                raise
    return missing


def cutover(org_id: str, cell: str | None = None, drain_seconds: int | None = None) -> dict:
    """Flip and Redirect: freeze writes, drain, final delta, verify, then route the tenant to its cell.

    - Only a tenant on `v1` (or `frozen`, left by a crashed run) can be cut over, and only one
      cutover per tenant runs at a time (advisory lock).
    - After freezing, waits `drain_seconds` (default PLACEMENT_CACHE_TTL_SECONDS). A router
      that caches tenant_placement can keep sending writes to V1 until its cache refreshes.
      **Waiting bounds the routing decision, not the commit**: a request that was already in
      flight, or that is retried, can still commit after the wait, and any writer that never
      reads tenant_placement (connector ingestion, queue consumers, batch jobs) is not stopped
      by this at all. For those, block the tenant's writes in V1 itself and pass
      `--drain-seconds 0`. See GUIDE.md section 5.6.
    - The V1 copy stays authoritative until the last step. If verification fails, or the
      process is interrupted, the tenant goes back to `v1`. Nothing has to be undone in V1.
      A hard kill (SIGKILL, or the host dying) can still leave it `frozen`: re-running
      `cutover` from `frozen` is safe, and `where` reports how long it has been frozen.
    """
    # `cell` is the label recorded in tenant_placement, which is what the router reads. The
    # rows always go to the database at V2_CELL_MESSAGING_DSN: point that at the cell you mean.
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
            missing_secrets = _missing_secrets(org_id)
        except BaseException as e:
            # BaseException, not Exception: SystemExit and KeyboardInterrupt (and the
            # SystemExit the CLI raises on SIGTERM) must unfreeze the tenant too, or it is
            # left with writes paused. Programming errors are re-raised after the rollback.
            _set(mgmt, org_id, "v1", None, f"rolled back: {type(e).__name__}: {e}"[:500])
            if not isinstance(e, Exception):
                raise
            return {"org_id": org_id, "cut_over": False, "status": "v1",
                    "reasons": [f"{type(e).__name__}: {e}"]}

        problems = (["reconciliation failed"] if not r["passed"] else []) + \
                   ([f"{len(d['disagreements'])} permission disagreements"] if d["disagreements"] else []) + \
                   ([f"{len(missing_secrets)} connector secrets missing: {missing_secrets[:3]}"]
                    if missing_secrets else [])
        if problems:
            _set(mgmt, org_id, "v1", None, "rolled back: " + ", ".join(problems))
            return {"org_id": org_id, "cut_over": False, "status": "v1", "reasons": problems,
                    "final_delta_rows": final.rows, "backfilled_rows": final.backfilled}

        _set(mgmt, org_id, "v2", cell, f"verified and redirected to {cell}")
    return {"org_id": org_id, "cut_over": True, "status": "v2", "cell": cell, "drained_seconds": drain,
            "connector_secrets_checked": settings.CHECK_CONNECTOR_SECRETS,
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
