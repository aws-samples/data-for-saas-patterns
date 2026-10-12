"""tenantmig: run the per-organisation V1 -> V2 migration steps."""
import argparse
import json

import psycopg

from tenantmig import config_job, content_job, cutover, object_job, perm_diff, reconcile, settings, synthetic


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


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
    """Apply sql/ to each database that doesn't have its schema yet (used in AWS; local-up does this locally)."""
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
    return done


def reset() -> None:
    """Empty the V2 targets and delete org buckets, so a demo can be re-run from scratch."""
    from tenantmig.store import ContentStore
    with psycopg.connect(settings.V2_CELL_MESSAGING_DSN) as c:
        c.execute("TRUNCATE messages, organisations, migration.org_state CASCADE")
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as c:
        c.execute(cutover.PLACEMENT_DDL)
        c.execute("TRUNCATE grants, principals, connectors, id_map, organisations, "
                  "tenant_placement, tenant_placement_log CASCADE")
    store = ContentStore()
    for b in store.s3.list_buckets().get("Buckets", []):
        if b["Name"].startswith(settings.ORG_BUCKET_PREFIX) and b["Name"] != settings.V1_SHARED_BUCKET:
            for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=b["Name"]):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objs:
                    store.s3.delete_objects(Bucket=b["Name"], Delete={"Objects": objs})
            store.s3.delete_bucket(Bucket=b["Name"])


def demo(args) -> None:
    """End to end for one org: Clone (bulk, delta, config, objects), verify, then Flip and Redirect."""
    reset()
    org = synthetic.generate(orgs=args.orgs, messages_per_org=args.messages)[0]
    print(f"\n== Synthetic V1 data generated. Migrating org {org}\n")

    print("== Step 1-3: bulk pass (content -> S3, rows -> cell messaging DB)")
    _print(vars(content_job.migrate_org(org)))

    print("\n== V1 keeps receiving messages for this org mid-migration...")
    synthetic.add_new_messages(org, count=50)
    print("== Step 4: delta pass (same job, picks up only new rows)")
    _print(vars(content_job.migrate_org(org)))

    print("\n== Step 5: config + permissions -> management DB" + (" (with injected bug)" if args.inject_bug else ""))
    _print(config_job.migrate_org(org, inject_bug=args.inject_bug))

    print("\n== Step 6: permissions diff harness")
    d = perm_diff.diff_org(org)
    _print({**d, "disagreements": d["disagreements"][:10], "disagreement_count": len(d["disagreements"])})
    blocked = len(d["disagreements"])

    print("\n== Step 8: existing shared-bucket objects -> org bucket")
    _print(object_job.migrate_objects(org))

    print("\n== Step 9: reconciliation")
    r = reconcile.reconcile_org(org)
    _print(r)
    if not r["passed"] or blocked:
        reasons = (["reconciliation failed"] if not r["passed"] else []) + \
                  ([f"{blocked} permission disagreements"] if blocked else [])
        raise SystemExit(f"NOT READY TO CUT OVER: {', '.join(reasons)}.")
    print("\nREADY TO CUT OVER: reconciliation passed and permissions agree.")

    print("\n== V1 receives a few more messages before the freeze...")
    synthetic.add_new_messages(org, count=10, seed=123)
    print("== Flip + Redirect: freeze writes, final delta, re-verify, route the tenant to its cell")
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

    g = sub.add_parser("generate", help="create synthetic V1 data")
    g.add_argument("--orgs", type=int, default=3)
    g.add_argument("--messages", type=int, default=2_000)

    sub.add_parser("orgs", help="list V1 organisations")
    sub.add_parser("reset", help="empty V2 targets and delete org buckets (sandbox only)")
    sub.add_parser("init", help="create schemas in empty databases (AWS sandbox)")
    for name in ("migrate-content", "migrate-config", "migrate-objects", "diff-permissions", "reconcile"):
        sp = sub.add_parser(name)
        sp.add_argument("org_id")
        if name == "migrate-config":
            sp.add_argument("--inject-bug", action="store_true")

    co = sub.add_parser("cutover", help="Flip + Redirect: freeze, final delta, verify, route to the cell")
    co.add_argument("org_id")
    co.add_argument("--cell", default=None)
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
    wake()
    if a.cmd == "generate":
        _print(synthetic.generate(orgs=a.orgs, messages_per_org=a.messages))
    elif a.cmd == "orgs":
        _print(_orgs())
    elif a.cmd == "init":
        _print({"applied": init_schemas()})
    elif a.cmd == "reset":
        reset()
        print("V2 targets emptied and org buckets deleted.")
    elif a.cmd == "migrate-content":
        _print(vars(content_job.migrate_org(a.org_id)))
    elif a.cmd == "migrate-config":
        _print(config_job.migrate_org(a.org_id, inject_bug=a.inject_bug))
    elif a.cmd == "migrate-objects":
        _print(object_job.migrate_objects(a.org_id))
    elif a.cmd == "diff-permissions":
        _print(perm_diff.diff_org(a.org_id))
    elif a.cmd == "reconcile":
        _print(reconcile.reconcile_org(a.org_id))
    elif a.cmd == "cutover":
        r = cutover.cutover(a.org_id, cell=a.cell)
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
