"""tenantmig: run the per-organisation V1 -> V2 migration steps."""
import argparse
import json
import signal

import psycopg

from tenantmig import (config_job, content_job, cutover, object_job, perm_diff, placement, reconcile,
                       settings, synthetic)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _on_sigterm(signum, frame):
    raise SystemExit(f"terminated by signal {signum}")


def wake(attempts: int = 12, delay: float = 10.0) -> None:
    """Aurora Serverless v2 auto-pauses when idle; the first connection resumes it. Retry until all answer."""
    import time
    for name, dsn in (("v1-messaging", settings.V1_MESSAGING_DSN), ("v1-auth", settings.V1_AUTH_DSN),
                      ("v2-cell-messaging", settings.V2_CELL_MESSAGING_DSN), ("v2-management", settings.V2_MANAGEMENT_DSN)):
        for i in range(attempts):
            try:
                with psycopg.connect(dsn, connect_timeout=20) as c:
                    c.execute("SELECT 1")
                break
            except psycopg.OperationalError as e:
                if i == attempts - 1:
                    raise
                print(f"waiting for {name} to resume ({i + 1}/{attempts}): {str(e).splitlines()[0][:120]}", flush=True)
                time.sleep(delay)


def _orgs() -> list[str]:
    with psycopg.connect(settings.V1_MESSAGING_DSN) as conn:
        return [str(r[0]) for r in conn.execute("SELECT id FROM organisations ORDER BY name")]


def init_schemas() -> list[str]:
    """Create the sample's schemas, catalogue and read-only role.

    This WRITES TO THE V1 DATABASES: it creates the catalogue tables, creates the
    `tenantmig_reader` role and sets its password, and grants it SELECT. That is why it is
    guarded like the destructive commands. A production team applies its own schema and
    creates the reader role from `tenantmig/reader.sql` by hand instead.
    """
    _require_sandbox("create schemas and the reader role (this writes to V1)")
    import os
    from pathlib import Path
    sql_dir = Path(os.getenv("SQL_DIR", Path(__file__).resolve().parent.parent / "sql"))
    targets = [
        (settings.V1_MESSAGING_DSN, "messaging", "v1/messaging.sql", "messages"),
        (settings.V1_AUTH_DSN, "auth", "v1/auth.sql", "users"),
        (settings.V2_CELL_MESSAGING_DSN, "messaging", "v2/cell_messaging.sql", "messages"),
        (settings.V2_MANAGEMENT_DSN, "management", "v2/management.sql", "grants"),
    ]
    done = []
    for dsn, db, path, probe in targets:
        with psycopg.connect(dsn) as c:
            if c.execute("SELECT to_regclass(%s)", (f"public.{probe}",)).fetchone()[0] is None:
                c.execute((sql_dir / path).read_text())
                done.append(path)
    _ensure_reader()
    return done


def _ensure_reader() -> None:
    """Read-only migration role on both V1 databases, used by postgres_fdw. No SELECT on credentials."""
    from pathlib import Path
    from psycopg import sql
    role_sql = (Path(__file__).resolve().parent / "reader.sql").read_text()
    grants = {
        settings.V1_MESSAGING_DSN: ["GRANT SELECT ON organisations TO tenantmig_reader",
                                    "GRANT SELECT (id, org_id, connector_type, settings) ON connector_configs TO tenantmig_reader"],
        settings.V1_AUTH_DSN: ["CREATE TABLE IF NOT EXISTS catalogue_resources (name text PRIMARY KEY)",
                               "CREATE TABLE IF NOT EXISTS catalogue_actions (name text PRIMARY KEY)",
                               "GRANT SELECT ON users, roles, user_roles, role_permissions, "
                               "catalogue_resources, catalogue_actions TO tenantmig_reader"],
    }
    for dsn, statements in grants.items():
        with psycopg.connect(dsn) as c:
            c.execute(role_sql)
            c.execute(sql.SQL("ALTER ROLE tenantmig_reader PASSWORD {}").format(sql.Literal(settings.READER_PASSWORD)))
            for s in statements:
                c.execute(s)


def _require_sandbox(what: str) -> None:
    if not settings.SANDBOX:
        raise SystemExit(f"Refusing to {what}: set TENANTMIG_SANDBOX=1 (only in a sandbox account or the local stack).")


def _delete_bucket(store, name: str) -> None:
    for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=name):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            resp = store.s3.delete_objects(Bucket=name, Delete={"Objects": objs})
            if resp.get("Errors"):
                raise RuntimeError(f"could not delete {len(resp['Errors'])} objects in {name}: {resp['Errors'][:3]}")
    store.s3.delete_bucket(Bucket=name)


def purge_buckets() -> list[str]:
    """Delete every bucket this sample created (per-tenant, V1 shared, migration). Sandbox only."""
    _require_sandbox("delete buckets")
    from tenantmig.store import ContentStore
    store = ContentStore()
    names = [b["Name"] for b in store.s3.list_buckets().get("Buckets", [])
             if b["Name"].startswith(settings.ORG_BUCKET_PREFIX)
             or b["Name"] in (settings.V1_SHARED_BUCKET, settings.MIGRATION_BUCKET)]
    for n in names:
        _delete_bucket(store, n)
    return names


def reset() -> None:
    """Empty the V2 targets and delete per-tenant buckets, so a demo can be re-run. Sandbox only."""
    _require_sandbox("reset the V2 targets")
    from tenantmig.store import ContentStore
    with psycopg.connect(settings.V2_CELL_MESSAGING_DSN) as c:
        c.execute("TRUNCATE messages, organisations, migration.org_state CASCADE")
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as c:
        c.execute(cutover.PLACEMENT_DDL)
        c.execute("TRUNCATE grants, principals, connectors, id_map, organisations, "
                  "tenant_placement, tenant_placement_log CASCADE")
    store = ContentStore()
    for b in store.s3.list_buckets().get("Buckets", []):
        name = b["Name"]
        if name.startswith(settings.ORG_BUCKET_PREFIX) and name not in (settings.V1_SHARED_BUCKET,
                                                                       settings.MIGRATION_BUCKET):
            _delete_bucket(store, name)


def demo(args) -> None:
    """End to end for one org: Clone (bulk, delta, config, objects), verify, then Flip and Redirect.

    Resets the sandbox first, so it only runs where TENANTMIG_SANDBOX=1.
    """
    init_schemas()
    reset()
    org = synthetic.generate(orgs=args.orgs, messages_per_org=args.messages)[0]
    print(f"\n== Synthetic V1 data generated. Migrating org {org}\n")

    print("== Step 1-3: bulk pass (content -> S3, rows -> cell messaging DB)")
    _print(vars(content_job.migrate_org(org)))

    print("\n== V1 kept receiving messages for this org while the bulk pass ran...")
    synthetic.add_new_messages(org, count=50, age_seconds=300)
    print("== Step 4: delta pass (same job, picks up only new rows older than the safety margin)")
    _print(vars(content_job.migrate_org(org)))

    print("\n== Step 5: config + permissions -> management DB" + (" (with injected bug)" if args.inject_bug else ""))
    _print(config_job.migrate_org(org, inject_bug=args.inject_bug))

    print("\n== Step 6: permissions diff harness")
    d = perm_diff.diff_org(org)
    _print({**d, "disagreements": d["disagreements"][:10], "disagreement_count": len(d["disagreements"])})
    blocked = len(d["disagreements"])

    print("\n== Step 7: existing shared-bucket objects -> org bucket")
    _print(object_job.migrate_objects(org))

    print("\n== Step 8: reconciliation")
    r = reconcile.reconcile_org(org)
    _print(r)
    if not r["passed"] or blocked:
        reasons = (["reconciliation failed"] if not r["passed"] else []) + \
                  ([f"{blocked} permission disagreements"] if blocked else [])
        raise SystemExit(f"NOT READY TO CUT OVER: {', '.join(reasons)}.")
    print("\nREADY TO CUT OVER: reconciliation passed and permissions agree.")

    print("\n== V1 receives a few more messages before the freeze...")
    synthetic.add_new_messages(org, count=10, seed=123)
    print(f"== Flip + Redirect: freeze writes, drain {settings.PLACEMENT_CACHE_TTL_SECONDS}s, final delta, "
          "re-verify, route the tenant to its cell")
    result = cutover.cutover(org)
    _print(result)
    _print({k: v for k, v in cutover.where(org).items() if k != "history"})
    if not result["cut_over"]:
        raise SystemExit("CUTOVER ROLLED BACK: the tenant stays on V1.")
    print(f"\nCUT OVER: tenant {org} is now served by {result['cell']}. "
          f"After your retention window: tenantmig forget {org} --confirm")


def main() -> None:
    p = argparse.ArgumentParser(prog="tenantmig", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="replace V1 with synthetic data (TENANTMIG_SANDBOX=1 only)")
    g.add_argument("--orgs", type=int, default=3)
    g.add_argument("--messages", type=int, default=2_000)

    sub.add_parser("orgs", help="list V1 organisations")
    sub.add_parser("reset", help="empty V2 targets and delete per-tenant buckets (TENANTMIG_SANDBOX=1 only)")
    sub.add_parser("purge-buckets", help="delete every bucket the sample created, before cdk destroy (sandbox only)")
    sub.add_parser("init", help="create schemas, catalogue and reader role; writes to V1 (sandbox only)")
    for name in ("migrate-content", "migrate-config", "migrate-objects", "diff-permissions", "reconcile"):
        sp = sub.add_parser(name)
        sp.add_argument("org_id")
        if name == "migrate-config":
            sp.add_argument("--inject-bug", action="store_true")
        if name == "migrate-content":
            sp.add_argument("--repair", action="store_true",
                            help="only copy V1 rows the cell is missing (anti-join), e.g. after reconcile fails")

    co = sub.add_parser("cutover", help="Flip + Redirect: freeze, final delta, verify, route to the cell")
    co.add_argument("org_id")
    co.add_argument("--cell", default=None,
                    help="cell label to record (the rows go to V2_CELL_MESSAGING_DSN regardless)")
    co.add_argument("--drain-seconds", type=int, default=None,
                    help="wait after freezing (default PLACEMENT_CACHE_TTL_SECONDS): your router's cache TTL")
    w = sub.add_parser("where", help="show where the router sends a tenant, with history")
    w.add_argument("org_id")
    f = sub.add_parser("forget", help="Forget: delete the tenant's V1 copy (dry run unless --confirm)")
    f.add_argument("org_id")
    f.add_argument("--confirm", action="store_true")

    d = sub.add_parser("demo", help="end-to-end run for one org")
    d.add_argument("--orgs", type=int, default=3)
    d.add_argument("--messages", type=int, default=2_000)
    d.add_argument("--inject-bug", action="store_true")

    a = p.parse_args()
    # A stopped Fargate task gets SIGTERM. Without this it dies silently and a cutover in
    # flight would leave the tenant frozen; as SystemExit, cutover's handler unfreezes first.
    signal.signal(signal.SIGTERM, _on_sigterm)
    if getattr(a, "org_id", None):
        a.org_id = placement.normalise_org_id(a.org_id)
    wake()
    if a.cmd == "generate":
        _print(synthetic.generate(orgs=a.orgs, messages_per_org=a.messages))
    elif a.cmd == "orgs":
        _print(_orgs())
    elif a.cmd == "init":
        _print({"applied": init_schemas()})
    elif a.cmd == "reset":
        reset()
        print("V2 targets emptied and per-tenant buckets deleted.")
    elif a.cmd == "purge-buckets":
        _print({"deleted_buckets": purge_buckets()})
    elif a.cmd == "migrate-content":
        _print(vars(content_job.migrate_org(a.org_id, repair=a.repair)))
    elif a.cmd == "migrate-config":
        _print(config_job.migrate_org(a.org_id, inject_bug=a.inject_bug))
    elif a.cmd == "migrate-objects":
        _print(object_job.migrate_objects(a.org_id))
    elif a.cmd == "diff-permissions":
        _print(perm_diff.diff_org(a.org_id))
    elif a.cmd == "reconcile":
        _print(reconcile.reconcile_org(a.org_id))
    elif a.cmd == "cutover":
        r = cutover.cutover(a.org_id, cell=a.cell, drain_seconds=a.drain_seconds)
        _print(r)
        if not r["cut_over"]:
            raise SystemExit(1)
    elif a.cmd == "where":
        _print(cutover.where(a.org_id))
    elif a.cmd == "forget":
        _print(cutover.forget(a.org_id, confirm=a.confirm))
    elif a.cmd == "demo":
        demo(a)


if __name__ == "__main__":
    main()
