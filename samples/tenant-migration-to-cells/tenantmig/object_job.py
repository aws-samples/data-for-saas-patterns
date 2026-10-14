"""Step 7: copy an org's existing objects from the V1 shared bucket into its V2 bucket.

AWS: S3 Batch Operations Copy, one job per org, driven by a manifest of the org's prefix.
In production build the manifest from S3 Inventory (filtered by the org prefix) rather than
by listing: a shared bucket in a large estate can hold billions of objects, and listing them
all is slow and expensive. Local MinIO has no Batch Operations, so the copy is done directly.

Only objects the destination is missing go into the manifest, so a re-run (and the re-check
during cutover) costs one LIST rather than a full re-copy.

Watch-outs at scale:
- A copy lands in the destination bucket's default storage class unless the job sets one.
  If you do set S3 Intelligent-Tiering, monitoring starts again from the Frequent Access
  tier, so objects that had aged into an archive tier are billed at Frequent Access rates
  until they age down again.
- Objects in S3 Glacier Flexible Retrieval or Deep Archive, and in the Intelligent-Tiering
  archive tiers, must be restored before they can be copied.
- Batch Operations copies objects up to 5 GB; larger ones need a multipart copy.
"""
import csv
import io
import time
from urllib.parse import quote

from botocore.exceptions import ClientError

from tenantmig import placement, settings
from tenantmig.store import ContentStore, org_bucket

DEST_PREFIX = "originals/"


def _source_keys(store: ContentStore, org_id: str) -> list[tuple[str, str]]:
    keys = []
    for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=settings.V1_SHARED_BUCKET, Prefix=f"{org_id}/"):
        keys += [(o["Key"], o["ETag"]) for o in page.get("Contents", [])]
    return keys


def migrate_objects(org_id: str, store: ContentStore | None = None) -> dict:
    placement.require_migratable(org_id, "copy shared-bucket objects")
    store = store or ContentStore()
    dest = org_bucket(org_id)
    store.ensure_bucket(dest)
    keys = _source_keys(store, org_id)
    if not keys:
        return {"org_id": org_id, "source_objects": 0, "mode": "none"}
    # Copy only what the destination lacks: one LIST instead of re-copying everything.
    already = store.list_keys(dest, prefix=DEST_PREFIX)
    todo = [(k, e) for k, e in keys if DEST_PREFIX + k not in already]
    if not todo:
        return {"org_id": org_id, "mode": "up to date", "source_objects": len(keys),
                "already_present": len(keys), "copied": 0}
    if settings.S3_ENDPOINT_URL:
        return _copy_direct(store, org_id, dest, todo, len(keys))
    return _copy_batch_operations(store, org_id, dest, todo, len(keys))


def _copy_direct(store, org_id, dest, todo, source_total) -> dict:
    copied = 0
    for key, _ in todo:
        store.s3.copy_object(Bucket=dest, Key=DEST_PREFIX + key,
                             CopySource={"Bucket": settings.V1_SHARED_BUCKET, "Key": key})
        copied += 1
    return {"org_id": org_id, "mode": "direct (local)", "source_objects": source_total,
            "already_present": source_total - len(todo), "copied": copied}


def _copy_batch_operations(store, org_id, dest, todo, source_total) -> dict:
    import boto3
    manifest_key = f"_manifests/{org_id}/{int(time.time())}.csv"
    buf = io.StringIO()
    w = csv.writer(buf)
    for key, _ in todo:
        # Batch Operations requires URL-encoded object keys in a CSV manifest. Without this,
        # keys containing a space, +, % or any non-ASCII character fail their task.
        w.writerow([settings.V1_SHARED_BUCKET, quote(key, safe="")])
    # A manually created Batch Operations manifest can't be SSE-KMS (only S3 Inventory reports can),
    # so the manifest (bucket/key pairs) is written with SSE-S3, overriding the bucket's KMS default.
    # Copied objects still use the cell key.
    # Manifests and reports live in a separate migration bucket, not in the V1 shared bucket.
    store.ensure_bucket(settings.MIGRATION_BUCKET)
    put = store.s3.put_object(Bucket=settings.MIGRATION_BUCKET, Key=manifest_key, Body=buf.getvalue().encode(),
                              ServerSideEncryption="AES256")

    s3control = boto3.client("s3control", region_name=settings.S3_REGION)
    job = s3control.create_job(
        AccountId=settings.ACCOUNT_ID,
        ConfirmationRequired=False,
        Priority=10,
        RoleArn=settings.BATCH_OPS_ROLE_ARN,
        Description=f"tenantmig copy org {org_id}",
        Operation={"S3PutObjectCopy": {
            "TargetResource": f"arn:aws:s3:::{dest}",
            "TargetKeyPrefix": DEST_PREFIX.rstrip("/"),
            # No SSE override: copies inherit the org bucket's default (the cell KMS key) and
            # keep their original metadata. Naming the key here also requires replacing metadata.
        }},
        Manifest={
            "Spec": {"Format": "S3BatchOperations_CSV_20180820", "Fields": ["Bucket", "Key"]},
            "Location": {"ObjectArn": f"arn:aws:s3:::{settings.MIGRATION_BUCKET}/{manifest_key}",
                         "ETag": put["ETag"].strip('"')},
        },
        Report={"Enabled": True, "Bucket": f"arn:aws:s3:::{settings.MIGRATION_BUCKET}",
                "Prefix": f"_reports/{org_id}", "Format": "Report_CSV_20180820", "ReportScope": "FailedTasksOnly"},
    )
    job_id = job["JobId"]
    deadline = time.monotonic() + settings.BATCH_TIMEOUT_SECONDS
    while True:
        d = s3control.describe_job(AccountId=settings.ACCOUNT_ID, JobId=job_id)["Job"]
        if d["Status"] in ("Complete", "Failed", "Cancelled"):
            break
        if time.monotonic() > deadline:
            # Cancel it: an abandoned job keeps copying (and billing) after we stop looking.
            try:
                s3control.update_job_status(AccountId=settings.ACCOUNT_ID, JobId=job_id,
                                            RequestedJobStatus="Cancelled",
                                            StatusUpdateReason="tenantmig timeout")
            except ClientError:
                pass                  # already terminal, or not cancellable: report the timeout
            raise TimeoutError(f"Batch Operations job {job_id} still {d['Status']} after "
                               f"{settings.BATCH_TIMEOUT_SECONDS}s (cancellation requested)")
        time.sleep(10)
    p = d.get("ProgressSummary", {})
    result = {"org_id": org_id, "mode": "S3 Batch Operations", "job_id": job_id, "status": d["Status"],
              "source_objects": source_total, "already_present": source_total - len(todo),
              "copied": p.get("NumberOfTasksSucceeded"), "succeeded": p.get("NumberOfTasksSucceeded"),
              "failed": p.get("NumberOfTasksFailed"), "failure_reasons": d.get("FailureReasons", [])}
    if d["Status"] != "Complete" or (p.get("NumberOfTasksFailed") or 0) > 0:
        raise RuntimeError(f"Batch Operations copy failed for {org_id}: {result}. Failed tasks are listed under "
                           f"s3://{settings.MIGRATION_BUCKET}/_reports/{org_id}/")
    return result
