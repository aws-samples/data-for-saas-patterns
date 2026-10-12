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
    result = cutover.cutover(org)
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
    result = cutover.cutover(org)
    assert not result["cut_over"] and cutover.where(org)["status"] == "v1"


def test_forget_refuses_before_cutover(org):
    from tenantmig import cutover
    with pytest.raises(SystemExit):
        cutover.forget(org)
