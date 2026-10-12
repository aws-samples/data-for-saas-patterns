"""Step 8: copy an org's existing objects from the V1 shared bucket into its V2 bucket.

AWS: S3 Batch Operations Copy, one job per org, driven by a manifest of the org's prefix.
In production build the manifest from S3 Inventory (filtered by org prefix) instead of
listing, since the shared bucket holds ~6 billion objects. Local MinIO has no Batch
Operations, so the same copy is done directly.

Watch-outs at scale: copied Intelligent-Tiering objects restart in Frequent Access;
Glacier Deep Archive objects must be restored first; Batch Operations copies objects up
to 5 GB (larger ones need multipart copy).
"""
import csv
import io
import time

from botocore.exceptions import ClientError

from tenantmig import settings
from tenantmig.store import ContentStore, org_bucket

DEST_PREFIX = "originals/"


def _source_keys(store: ContentStore, org_id: str) -> list[tuple[str, str]]:
    keys = []
    for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=settings.V1_SHARED_BUCKET, Prefix=f"{org_id}/"):
        keys += [(o["Key"], o["ETag"]) for o in page.get("Contents", [])]
    return keys


def migrate_objects(org_id: str, store: ContentStore | None = None) -> dict:
    store = store or ContentStore()
    dest = org_bucket(org_id)
    store.ensure_bucket(dest)
    keys = _source_keys(store, org_id)
    if not keys:
        return {"org_id": org_id, "source_objects": 0, "mode": "none"}
    if settings.S3_ENDPOINT_URL:
        return _copy_direct(store, org_id, dest, keys)
    return _copy_batch_operations(store, org_id, dest, keys)


def _copy_direct(store, org_id, dest, keys) -> dict:
    copied = skipped = 0
    for key, _ in keys:
        target = DEST_PREFIX + key
        try:
            store.s3.head_object(Bucket=dest, Key=target)
            skipped += 1
            continue
        except ClientError:
            pass
        store.s3.copy_object(Bucket=dest, Key=target, CopySource={"Bucket": settings.V1_SHARED_BUCKET, "Key": key})
        copied += 1
    return {"org_id": org_id, "mode": "direct (local)", "source_objects": len(keys), "copied": copied, "skipped": skipped}


def _copy_batch_operations(store, org_id, dest, keys) -> dict:
    import boto3
    manifest_key = f"_manifests/{org_id}/{int(time.time())}.csv"
    buf = io.StringIO()
    w = csv.writer(buf)
    for key, _ in keys:
        w.writerow([settings.V1_SHARED_BUCKET, key])
    # A manually created Batch Operations manifest can't be SSE-KMS (only S3 Inventory reports can),
    # so the manifest (bucket/key pairs) is written with SSE-S3, overriding the bucket's KMS default.
    # Copied objects still use the cell key.
    put = store.s3.put_object(Bucket=settings.V1_SHARED_BUCKET, Key=manifest_key, Body=buf.getvalue().encode(),
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
            "Location": {"ObjectArn": f"arn:aws:s3:::{settings.V1_SHARED_BUCKET}/{manifest_key}",
                         "ETag": put["ETag"].strip('"')},
        },
        Report={"Enabled": True, "Bucket": f"arn:aws:s3:::{settings.V1_SHARED_BUCKET}",
                "Prefix": f"_reports/{org_id}", "Format": "Report_CSV_20180820", "ReportScope": "FailedTasksOnly"},
    )
    job_id = job["JobId"]
    while True:
        d = s3control.describe_job(AccountId=settings.ACCOUNT_ID, JobId=job_id)["Job"]
        if d["Status"] in ("Complete", "Failed", "Cancelled"):
            break
        time.sleep(10)
    p = d.get("ProgressSummary", {})
    return {"org_id": org_id, "mode": "S3 Batch Operations", "job_id": job_id, "status": d["Status"],
            "source_objects": len(keys), "succeeded": p.get("NumberOfTasksSucceeded"),
            "failed": p.get("NumberOfTasksFailed"), "failure_reasons": d.get("FailureReasons", [])}
