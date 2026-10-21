"""Integration tests against the local stack. Run with:

    ./scripts/local-up.sh && source .local/env
    TENANTMIG_INTEGRATION=1 python -m pytest tests/test_integration.py
"""
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("TENANTMIG_INTEGRATION") != "1",
                                reason="needs the local stack (set TENANTMIG_INTEGRATION=1)")


@pytest.fixture()
def org():
    from tenantmig import cli, synthetic
    cli.init_schemas()
    cli.reset()
    return synthetic.generate(orgs=2, messages_per_org=600, objects_per_org=20)[0]


def _setup_fdw():
    """The admin step that builds the migrator's fdw objects. Config runs only read them."""
    import psycopg
    from tenantmig import config_job, settings
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as c:
        config_job.setup_fdw(c)


def _migrate_all(org_id):
    from tenantmig import config_job, content_job, object_job
    content_job.migrate_org(org_id)
    config_job.migrate_org(org_id)
    object_job.migrate_objects(org_id)


def test_crash_mid_pass_reruns_cleanly(org):
    from tenantmig import content_job, object_job, reconcile
    from tenantmig.store import ContentStore

    class Crashing(ContentStore):
        puts = 0

        def put_if_absent(self, b, k, body):
            Crashing.puts += 1
            if Crashing.puts > 300:
                raise ConnectionError("simulated crash")
            return super().put_if_absent(b, k, body)

    with pytest.raises(ConnectionError):
        content_job.migrate_org(org, batch_size=100, store=Crashing())
    rerun = content_job.migrate_org(org, batch_size=100)
    assert 0 < rerun.rows < 600                      # resumed from the last committed batch
    object_job.migrate_objects(org)
    assert reconcile.reconcile_org(org)["passed"]


def test_rerun_is_a_no_op(org):
    from tenantmig import content_job
    _migrate_all(org)
    again = content_job.migrate_org(org)
    assert (again.rows, again.objects_written) == (0, 0)


def test_injected_permission_bug_is_caught(org):
    from tenantmig import config_job, content_job, perm_diff
    content_job.migrate_org(org)
    config_job.migrate_org(org, inject_bug=True)
    assert perm_diff.diff_org(org)["disagreements"]
    config_job.migrate_org(org)
    assert not perm_diff.diff_org(org)["disagreements"]


def test_cutover_redirects_then_forget_removes_v1_copy(org):
    from tenantmig import cutover, synthetic
    _migrate_all(org)
    synthetic.add_new_messages(org, count=5, seed=5)
    result = cutover.cutover(org, drain_seconds=0)
    assert result["cut_over"] and result["final_delta_rows"] == 5
    assert cutover.where(org)["status"] == "v2"
    assert cutover.forget(org)["dry_run"]
    assert cutover.forget(org, confirm=True)["deleted"]["messages"] == 605


def test_cutover_rolls_back_when_an_object_is_missing(org):
    from tenantmig import cutover
    from tenantmig.store import ContentStore, org_bucket
    _migrate_all(org)
    store = ContentStore()
    key = next(k for k in store.list_keys(org_bucket(org)) if not k.startswith("originals/"))
    store.s3.delete_object(Bucket=org_bucket(org), Key=key)
    result = cutover.cutover(org, drain_seconds=0)
    assert not result["cut_over"] and cutover.where(org)["status"] == "v1"


def test_forget_refuses_before_cutover(org):
    from tenantmig import cutover
    with pytest.raises(SystemExit):
        cutover.forget(org)


def _v1(dsn_name):
    import psycopg
    from tenantmig import settings
    return psycopg.connect(getattr(settings, dsn_name))


def test_wildcard_admin_keeps_access_to_unnamed_resources(org):
    """Review #1: no rule names `connector` or `approve`; an admin (*/*) must still get them."""
    from tenantmig import config_job, perm_diff, settings, synthetic
    import psycopg
    config_job.migrate_org(org)
    with _v1("V1_AUTH_DSN") as auth:
        named = {r[0] for r in auth.execute("SELECT resource FROM role_permissions")} | \
                {r[0] for r in auth.execute("SELECT action FROM role_permissions")}
        admins = [r[0] for r in auth.execute(
            "SELECT ur.user_id FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
            "WHERE r.org_id = %s AND r.name = 'admin'", (org,))]
    assert "connector" not in named and "approve" not in named and admins
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        assert m.execute("SELECT 1 FROM grants WHERE principal_id = %s AND resource = 'connector' "
                         "AND action = 'approve'", (admins[0],)).fetchone()
    d = perm_diff.diff_org(org)
    assert not d["disagreements"]
    assert d["decisions_checked"] == 25 * len(synthetic.RESOURCES) * len(synthetic.ACTIONS)


def test_harness_catches_a_transform_that_drops_unnamed_resources(org, monkeypatch):
    from tenantmig import config_job, perm_diff
    # Simulate the old blind spot: expand wildcards only against resources some rule names.
    monkeypatch.setattr(config_job, "GRANTS_SQL", config_job.GRANTS_SQL.replace(
        "FROM v1_auth.users u, v1_auth.catalogue_resources res",
        "FROM v1_auth.users u, (SELECT DISTINCT resource AS name FROM v1_auth.role_permissions "
        "WHERE resource <> '*') res"))
    config_job.migrate_org(org)
    assert any(x["resource"] == "connector" for x in perm_diff.diff_org(org)["disagreements"])


def test_cutover_rolls_back_on_any_exception(org, monkeypatch):
    """Review #2."""
    from tenantmig import cutover, object_job
    _migrate_all(org)
    def boom(*a, **k):
        raise ConnectionError("S3 unavailable")
    monkeypatch.setattr(object_job, "migrate_objects", boom)
    monkeypatch.setattr(cutover.reconcile, "reconcile_org",
                        lambda o: {"all_shared_objects_copied": False, "passed": False})
    result = cutover.cutover(org, drain_seconds=0)
    assert not result["cut_over"] and "ConnectionError" in result["reasons"][0]
    assert cutover.where(org)["status"] == "v1"


def test_second_cutover_is_refused(org):
    """Review #3: re-running cutover on a live V2 tenant must not route it back to V1."""
    from tenantmig import cutover
    _migrate_all(org)
    assert cutover.cutover(org, drain_seconds=0)["cut_over"]
    with pytest.raises(SystemExit):
        cutover.cutover(org, drain_seconds=0)
    assert cutover.where(org)["status"] == "v2"


def test_late_config_changes_and_deletions_converge(org):
    """Review #5."""
    from tenantmig import config_job, settings
    import psycopg
    config_job.migrate_org(org)
    with _v1("V1_MESSAGING_DSN") as msg, _v1("V1_AUTH_DSN") as auth:
        cid = msg.execute("SELECT id FROM connector_configs WHERE org_id = %s ORDER BY id LIMIT 1", (org,)).fetchone()[0]
        msg.execute("UPDATE connector_configs SET settings = '{\"poll_seconds\": 5}' WHERE id = %s", (cid,))
        u1, u2 = [r[0] for r in auth.execute("SELECT id FROM users WHERE org_id = %s ORDER BY id LIMIT 2", (org,))]
        auth.execute("UPDATE users SET email = 'changed@example.com' WHERE id = %s", (u1,))
        auth.execute("DELETE FROM user_roles WHERE user_id = %s", (u2,))
        auth.execute("DELETE FROM users WHERE id = %s", (u2,))
    r = config_job.migrate_org(org)
    assert r["principals_removed"] == 1
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        assert m.execute("SELECT settings->>'poll_seconds' FROM connectors WHERE id = %s", (cid,)).fetchone()[0] == "5"
        assert m.execute("SELECT email FROM principals WHERE id = %s", (u1,)).fetchone()[0] == "changed@example.com"
        assert m.execute("SELECT 1 FROM principals WHERE id = %s", (u2,)).fetchone() is None


def test_out_of_order_commit_is_repaired(org):
    """Review #6: a lower id that commits after the watermark passed it."""
    from tenantmig import content_job, object_job, reconcile
    with _v1("V1_MESSAGING_DSN") as msg:
        late = msg.execute("SELECT id, org_id, channel, sender, sent_at, created_at, content FROM messages "
                           "WHERE org_id = %s ORDER BY id OFFSET 100 LIMIT 1", (org,)).fetchone()
        msg.execute("DELETE FROM messages WHERE id = %s", (late[0],))
    content_job.migrate_org(org)
    object_job.migrate_objects(org)
    with _v1("V1_MESSAGING_DSN") as msg:            # the in-flight transaction finally commits
        msg.execute("INSERT INTO messages VALUES (%s, %s, %s, %s, %s, %s, %s)", late)
    assert content_job.migrate_org(org).rows == 0  # the watermark is already past it
    r = reconcile.reconcile_org(org)
    assert not r["passed"] and r["mismatched_id_ranges"]
    assert content_job.migrate_org(org, repair=True).backfilled == 1
    assert reconcile.reconcile_org(org)["passed"]


def test_credentials_are_not_reachable_through_the_fdw(org):
    """Review #7: the migrator's foreign tables and the tenant's own login both lack `credentials`."""
    import psycopg
    from tenantmig import config_job, identities, settings
    config_job.migrate_org(org)
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        cols = {r[0] for r in m.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'v1_messaging' AND table_name = 'connector_configs'")}
        assert cols and "credentials" not in cols
    with psycopg.connect(identities.tenant_dsn(settings.V1_MESSAGING_DSN, org)) as t:
        assert t.execute("SELECT count(id) FROM connector_configs WHERE org_id = %s", (org,)).fetchone()[0] == 3
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            t.execute("SELECT credentials FROM connector_configs LIMIT 1")


def test_reset_refuses_outside_a_sandbox(monkeypatch):
    """Review #9."""
    from tenantmig import cli, settings
    monkeypatch.setattr(settings, "SANDBOX", False)
    with pytest.raises(SystemExit):
        cli.reset()
    with pytest.raises(SystemExit):
        cli.purge_buckets()


# --- regressions from the independent review (14 Oct) ---

def test_migrate_steps_refuse_on_a_live_tenant(org):
    """B2: a migration step must not overwrite (or empty) a tenant that is live in its cell."""
    from tenantmig import config_job, content_job, cutover, object_job
    _migrate_all(org)
    assert cutover.cutover(org, drain_seconds=0)["cut_over"]
    cutover.forget(org, confirm=True)                      # V1 copy is gone
    for call in (lambda: config_job.migrate_org(org),
                 lambda: content_job.migrate_org(org),
                 lambda: object_job.migrate_objects(org)):
        with pytest.raises(SystemExit):
            call()
    import psycopg
    from tenantmig import settings
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        assert m.execute("SELECT count(*) FROM principals WHERE org_id = %s", (org,)).fetchone()[0] == 25
        assert m.execute("SELECT count(*) FROM grants g JOIN principals p ON p.id = g.principal_id "
                         "WHERE p.org_id = %s", (org,)).fetchone()[0] > 0


def test_generate_and_init_refuse_outside_a_sandbox(monkeypatch):
    """B3: the most destructive command (TRUNCATE on V1) was unguarded."""
    from tenantmig import cli, settings, synthetic
    monkeypatch.setattr(settings, "SANDBOX", False)
    with pytest.raises(SystemExit):
        synthetic.generate(orgs=1, messages_per_org=1)
    with pytest.raises(SystemExit):
        cli.init_schemas()


def test_margin_does_not_skip_a_lower_id_committed_late(org):
    """S1: the watermark must not advance past a row still inside the safety margin."""
    import psycopg
    from tenantmig import content_job, settings
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        top = msg.execute("SELECT max(id) FROM messages").fetchone()[0]   # ids are global
        # 101 is brand new (inside the margin); 102 is old (outside it) but has a higher id.
        msg.execute("INSERT INTO messages VALUES (%s, %s, 'email', 'a@example.com', now(), now(), 'fresh')",
                    (top + 1, org))
        msg.execute("INSERT INTO messages VALUES (%s, %s, 'email', 'b@example.com', now(), "
                    "now() - interval '1 hour', 'older')", (top + 2, org))
    first = content_job.migrate_org(org)
    assert first.held_back >= 1                            # the fresh row was held back
    assert first.watermark < top + 1                       # and the watermark stopped below it
    with psycopg.connect(settings.V2_CELL_MESSAGING_DSN) as cell:
        have = {r[0] for r in cell.execute("SELECT id FROM messages WHERE org_id = %s AND id > %s",
                                           (org, top))}
    assert have == set()                                   # neither row copied yet: correct
    # Once the fresh row ages out of the margin, a plain delta pass picks up both.
    import tenantmig.settings as st
    old = st.DELTA_SAFETY_SECONDS
    try:
        st.DELTA_SAFETY_SECONDS = 0
        content_job.migrate_org(org)
    finally:
        st.DELTA_SAFETY_SECONDS = old
    with psycopg.connect(settings.V2_CELL_MESSAGING_DSN) as cell:
        have = {r[0] for r in cell.execute("SELECT id FROM messages WHERE org_id = %s AND id > %s",
                                           (org, top))}
    assert have == {top + 1, top + 2}


def test_repair_fixes_deleted_and_edited_rows(org):
    """S4: repair has to converge, not only backfill."""
    import psycopg
    from tenantmig import content_job, object_job, reconcile, settings
    _migrate_all(org)
    assert reconcile.reconcile_org(org)["passed"]
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        gone = msg.execute("SELECT id FROM messages WHERE org_id = %s ORDER BY id LIMIT 1", (org,)).fetchone()[0]
        edited = msg.execute("SELECT id FROM messages WHERE org_id = %s ORDER BY id OFFSET 5 LIMIT 1",
                             (org,)).fetchone()[0]
        msg.execute("DELETE FROM messages WHERE id = %s", (gone,))
        msg.execute("UPDATE messages SET sender = 'edited@example.com', content = 'edited body' WHERE id = %s",
                    (edited,))
    assert not reconcile.reconcile_org(org)["passed"]
    r = content_job.migrate_org(org, repair=True)
    assert r.deleted_extras == 1 and r.recopied == 1
    object_job.migrate_objects(org)
    assert reconcile.reconcile_org(org)["passed"]
    with psycopg.connect(settings.V2_CELL_MESSAGING_DSN) as cell:
        assert cell.execute("SELECT count(*) FROM messages WHERE id = %s", (gone,)).fetchone()[0] == 0
        assert cell.execute("SELECT sender FROM messages WHERE id = %s", (edited,)).fetchone()[0] \
            == "edited@example.com"


def test_409_is_retried_not_treated_as_present(org):
    """S11: a 409 means the object may be absent, so the PUT must be retried."""
    from botocore.exceptions import ClientError
    from tenantmig.store import ContentStore

    class Conflicting(ContentStore):
        calls = 0

        def __init__(self):
            super().__init__()

        def _put(self, **kw):
            Conflicting.calls += 1
            if Conflicting.calls == 1:
                raise ClientError({"Error": {"Code": "ConditionalRequestConflict"},
                                   "ResponseMetadata": {"HTTPStatusCode": 409}}, "PutObject")
            return {}

    store = Conflicting()
    store.s3.put_object = store._put
    assert store.put_if_absent("any-bucket", "k", b"body") is True   # retried, then written
    assert Conflicting.calls == 2

    class Exists(ContentStore):
        def __init__(self):
            super().__init__()

        def _put(self, **kw):
            raise ClientError({"Error": {"Code": "PreconditionFailed"},
                               "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")

    e = Exists()
    e.s3.put_object = e._put
    assert e.put_if_absent("any-bucket", "k", b"body") is False      # 412: already there


def test_uncatalogued_v1_rule_is_reported(org):
    """S12: a V1 rule naming something outside the catalogue was dropped silently."""
    import psycopg
    from tenantmig import config_job, perm_diff, settings
    with psycopg.connect(settings.V1_AUTH_DSN) as auth:
        role = auth.execute("SELECT id FROM roles WHERE org_id = %s ORDER BY id LIMIT 1", (org,)).fetchone()[0]
        auth.execute("INSERT INTO role_permissions VALUES (%s, 'legacy_reports', 'read', 'allow')", (role,))
    config_job.migrate_org(org)
    d = perm_diff.diff_org(org)
    assert "resource: legacy_reports" in d["uncatalogued"]
    assert any(x["resource"] == "legacy_reports" for x in d["disagreements"])


def test_parallel_config_jobs_do_not_race_on_fdw_setup(org):
    """S6: the fdw setup used to check-then-create without a lock. Now it's an admin step, and
    several runs of it at once must still converge."""
    from concurrent.futures import ThreadPoolExecutor
    import psycopg
    from tenantmig import cli, config_job, identities, settings, synthetic
    cli.init_schemas()
    cli.reset()
    orgs = synthetic.generate(orgs=4, messages_per_org=20, objects_per_org=2)
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:       # start from no fdw objects
        m.execute("DROP SCHEMA IF EXISTS v1_messaging CASCADE")
        m.execute("DROP SCHEMA IF EXISTS v1_auth CASCADE")
        m.execute("DROP SERVER IF EXISTS v1_messaging_srv CASCADE")
        m.execute("DROP SERVER IF EXISTS v1_auth_srv CASCADE")
    def setup(_):
        with psycopg.connect(settings.V2_MANAGEMENT_DSN) as c:
            config_job.setup_fdw(c)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(setup, range(4)))
    for o in orgs:
        identities.provision_tenant(o)          # the per-tenant mappings went with the servers
    results = [config_job.migrate_org(o) for o in orgs]
    assert all(r["grants"] > 0 for r in results)


def test_rotated_tenant_password_is_picked_up(org):
    """S7: a rotated password reaches the tenant's login and the migrator's fdw mapping."""
    import psycopg
    from tenantmig import config_job, identities, settings
    bundle = identities._passwords(org, create=False)
    identities._put_bundle(org, {**bundle, "tenant": "rotated-" + bundle["tenant"]})
    identities.provision_tenant(org)
    with psycopg.connect(identities.tenant_dsn(settings.V1_MESSAGING_DSN, org)) as t:
        assert t.execute("SELECT 1").fetchone()[0] == 1
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        opts = m.execute("SELECT umoptions::text FROM pg_user_mappings WHERE srvname = 'v1_auth_srv' AND usename = %s",
                         (identities.migrator_role(org),)).fetchone()[0]
    assert "rotated-" in opts
    assert config_job.migrate_org(org)["grants"] > 0


def test_upgrade_drops_a_foreign_table_that_exposed_credentials(org):
    """S7: an install from an older version kept the wider column list."""
    import psycopg
    from tenantmig import config_job, settings
    _setup_fdw()
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:       # simulate the old definition
        m.execute("DROP FOREIGN TABLE v1_messaging.connector_configs")
        m.execute("CREATE FOREIGN TABLE v1_messaging.connector_configs "
                  "(id bigint, org_id uuid, connector_type text, settings jsonb, credentials text) "
                  "SERVER v1_messaging_srv OPTIONS (schema_name 'public', table_name 'connector_configs')")
    _setup_fdw()
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        cols = {r[0] for r in m.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'v1_messaging' AND table_name = 'connector_configs'")}
    assert "credentials" not in cols


def test_objects_step_only_copies_what_is_missing(org):
    """S9: the freeze re-check used to re-copy every object."""
    from tenantmig import content_job, object_job
    content_job.migrate_org(org)
    first = object_job.migrate_objects(org)
    assert first["copied"] == 20
    second = object_job.migrate_objects(org)
    assert second["copied"] == 0 and second["mode"] == "up to date"


def test_content_only_edit_is_repaired(org):
    """Review finding 1: a V1 content change with unchanged metadata must converge under --repair."""
    import psycopg
    from tenantmig import content_job, object_job, reconcile, settings
    _migrate_all(org)
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        edited = msg.execute("SELECT id FROM messages WHERE org_id = %s ORDER BY id OFFSET 7 LIMIT 1",
                             (org,)).fetchone()[0]
        msg.execute("UPDATE messages SET content = 'content-only edit' WHERE id = %s", (edited,))
    assert not reconcile.reconcile_org(org)["passed"]
    r = content_job.migrate_org(org, repair=True)
    assert r.recopied == 1
    object_job.migrate_objects(org)
    assert reconcile.reconcile_org(org)["passed"]


def test_checksum_does_not_collide_on_delimiters(org):
    """Review finding 8: a '|' that moves a field boundary must change the checksum."""
    import psycopg
    from tenantmig import reconcile, settings
    _migrate_all(org)
    assert reconcile.reconcile_org(org)["passed"]
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        row = msg.execute("SELECT id FROM messages WHERE org_id = %s ORDER BY id LIMIT 1", (org,)).fetchone()[0]
        msg.execute("UPDATE messages SET channel = 'a|b', sender = 'c' WHERE id = %s", (row,))
    with psycopg.connect(settings.V2_CELL_MESSAGING_DSN) as cell:
        cell.execute("UPDATE messages SET channel = 'a', sender = 'b|c' WHERE id = %s", (row,))
        cell.commit()
    r = reconcile.reconcile_org(org)
    assert not r["checksums_match"], "two different rows serialised to the same checksum input"


def test_changed_source_object_is_recopied(org):
    """Review finding 9: an overwritten V1 object keeps its key, so presence alone must not pass."""
    from tenantmig import object_job, reconcile, settings
    from tenantmig.store import ContentStore
    _migrate_all(org)
    store = ContentStore()
    key = next(iter(store.list_objects(settings.V1_SHARED_BUCKET, prefix=f"{org}/")))
    store.s3.put_object(Bucket=settings.V1_SHARED_BUCKET, Key=key, Body=b"changed in place")
    assert not reconcile.reconcile_org(org)["all_shared_objects_copied"]
    result = object_job.migrate_objects(org)
    assert result["copied"] >= 1
    assert reconcile.reconcile_org(org)["all_shared_objects_copied"]


def test_a_step_cannot_run_while_a_cutover_holds_the_tenant(org):
    """Review finding 2: the status check and the write must share the cutover lock."""
    import psycopg
    from tenantmig import content_job, settings
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as held:
        assert held.execute("SELECT pg_try_advisory_lock(hashtext('cutover:' || %s))", (org,)).fetchone()[0]
        with pytest.raises(SystemExit, match="already running"):
            content_job.migrate_org(org)


def test_cutover_refuses_while_a_step_holds_the_tenant(org):
    import psycopg
    from tenantmig import cutover, settings
    _migrate_all(org)
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as held:
        held.execute("SELECT pg_try_advisory_lock(hashtext('cutover:' || %s))", (org,)).fetchone()
        with pytest.raises(SystemExit, match="cutover or migration step"):
            cutover.cutover(org, drain_seconds=0)
    assert cutover.where(org)["status"] == "v1"


def test_tenant_role_cannot_read_or_write_another_tenants_rows(org):
    """Review finding 4: the database filters by tenant, whatever the job's own WHERE clauses say."""
    import psycopg
    from tenantmig import identities, settings
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        other = msg.execute("SELECT id FROM organisations WHERE id <> %s ORDER BY id LIMIT 1", (org,)).fetchone()[0]
        own = msg.execute("SELECT count(*) FROM messages WHERE org_id = %s", (org,)).fetchone()[0]
    with psycopg.connect(identities.tenant_dsn(settings.V1_MESSAGING_DSN, org)) as msg:
        assert msg.execute("SELECT count(*) FROM messages WHERE org_id = %s", (other,)).fetchone()[0] == 0
        assert msg.execute("SELECT count(*) FROM messages").fetchone()[0] == own
        with pytest.raises(psycopg.Error, match="row-level security"):
            msg.execute("INSERT INTO messages (id, org_id, channel, sender, sent_at, created_at, content) "
                        "VALUES (999999999, %s, 'x', 'x', now(), now(), 'x')", (other,))


def test_group_role_with_no_tenant_sees_nothing(org):
    """Fail closed: a role that isn't a tenant, such as the shared group role, sees no rows."""
    import psycopg
    from tenantmig import settings
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        msg.execute("SET ROLE tenantmig_tenant")
        assert msg.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert msg.execute("SELECT count(*) FROM organisations").fetchone()[0] == 0


def test_migrator_and_control_roles_are_confined(org):
    """The duty split: a tenant's migrator can't read V1 data or placement; its control login can't read config."""
    import psycopg
    from tenantmig import identities, settings
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(identities.control_dsn(settings.V2_MANAGEMENT_DSN, org)) as c:
            c.execute("SELECT count(*) FROM connectors")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(identities.migrator_dsn(settings.V2_MANAGEMENT_DSN, org)) as c:
            c.execute("SELECT count(*) FROM tenant_placement")
    with psycopg.connect(identities.control_dsn(settings.V2_MANAGEMENT_DSN, org)) as c:
        assert c.execute("SELECT count(*) FROM tenant_placement").fetchone()[0] >= 0


def test_each_tenants_logins_see_only_that_tenant(org):
    """Per-tenant identities: a tenant's migrator and control logins see another tenant's rows as none."""
    import psycopg
    from tenantmig import identities, settings
    _migrate_all(org)
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        other = msg.execute("SELECT id FROM organisations WHERE id <> %s ORDER BY id LIMIT 1", (org,)).fetchone()[0]
    identities.provision_tenant(other)
    with psycopg.connect(identities.migrator_dsn(settings.V2_MANAGEMENT_DSN, org)) as m:
        assert m.execute("SELECT count(*) FROM connectors WHERE org_id = %s", (other,)).fetchone()[0] == 0
        assert m.execute("SELECT count(*) FROM principals WHERE org_id = %s", (other,)).fetchone()[0] == 0
    with psycopg.connect(identities.control_dsn(settings.V2_MANAGEMENT_DSN, org)) as c:
        assert c.execute("SELECT count(*) FROM tenant_placement WHERE org_id = %s", (other,)).fetchone()[0] == 0


def test_connector_secret_refs_match_the_stored_secret_names(org):
    """The migration records a secret_ref; the synthetic data stores the credential under the same name."""
    import psycopg
    from tenantmig import config_job, identities, settings
    config_job.migrate_org(org)
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        refs = {r[0] for r in m.execute("SELECT secret_ref FROM connectors WHERE org_id = %s", (org,))}
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        ids = [r[0] for r in msg.execute("SELECT id FROM connector_configs WHERE org_id = %s", (org,))]
    assert refs == {identities.connector_secret_name(org, i) for i in ids}


def test_cutover_refuses_when_a_connector_secret_is_missing(org, monkeypatch):
    """Review: a redirect with a missing connector secret means `forget` loses the credential for good."""
    import boto3
    import psycopg
    from botocore.exceptions import ClientError
    from tenantmig import cutover, identities, settings
    _migrate_all(org)
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        ref = m.execute("SELECT secret_ref FROM connectors WHERE org_id = %s ORDER BY id LIMIT 1", (org,)).fetchone()[0]

    class FakeSecrets:
        def describe_secret(self, SecretId):
            if SecretId == ref:
                raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeSecret")
            return {"Name": SecretId}

    real_client = boto3.client
    monkeypatch.setattr(settings, "CHECK_CONNECTOR_SECRETS", True)
    monkeypatch.setattr(boto3, "client", lambda service, **k: FakeSecrets() if service == "secretsmanager"
                        else real_client(service, **k))
    result = cutover.cutover(org, drain_seconds=0)
    assert not result["cut_over"]
    assert any("connector secrets missing" in r for r in result["reasons"])
    assert cutover.where(org)["status"] == "v1"
