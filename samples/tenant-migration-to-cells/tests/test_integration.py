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
    """Review #7."""
    import psycopg
    from tenantmig import config_job, settings
    config_job.migrate_org(org)
    with psycopg.connect(settings.V2_MANAGEMENT_DSN) as m:
        cols = {r[0] for r in m.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'v1_messaging' AND table_name = 'connector_configs'")}
        assert cols and "credentials" not in cols
    reader = settings.V1_MESSAGING_DSN.replace("postgres:local@", f"tenantmig_reader:{settings.READER_PASSWORD}@")
    with psycopg.connect(reader) as r:
        assert r.execute("SELECT count(id) FROM connector_configs WHERE org_id = %s", (org,)).fetchone()[0] == 3
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            r.execute("SELECT credentials FROM connector_configs LIMIT 1")


def test_reset_refuses_outside_a_sandbox(monkeypatch):
    """Review #9."""
    from tenantmig import cli, settings
    monkeypatch.setattr(settings, "SANDBOX", False)
    with pytest.raises(SystemExit):
        cli.reset()
    with pytest.raises(SystemExit):
        cli.purge_buckets()
