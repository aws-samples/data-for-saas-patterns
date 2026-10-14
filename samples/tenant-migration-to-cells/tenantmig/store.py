"""Per-organisation content store on S3, written with conditional PUTs."""
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from tenantmig import settings


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"},
                      max_pool_connections=settings.S3_WORKERS + 4),
    )


def org_bucket(org_id: str) -> str:
    return f"{settings.ORG_BUCKET_PREFIX}{org_id}".lower()


class ContentStore:
    def __init__(self, client=None):
        self.s3 = client or s3_client()

    def _owner(self) -> dict:
        """ExpectedBucketOwner, so a bucket someone else pre-created under a guessable name
        fails the call instead of silently receiving a tenant's content. MinIO ignores it."""
        return {"ExpectedBucketOwner": settings.EXPECTED_BUCKET_OWNER} if settings.EXPECTED_BUCKET_OWNER else {}

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self.s3.head_bucket(Bucket=bucket, **self._owner())
            return
        except ClientError as e:
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (403, 301):
                # The bucket exists and is not ours (or is in another Region). Creating it would
                # fail anyway; writing to it would hand a tenant's content to its owner.
                raise RuntimeError(
                    f"bucket {bucket} exists but is not owned by {settings.EXPECTED_BUCKET_OWNER} "
                    f"(HTTP {status}). Choose a different ORG_BUCKET_PREFIX.") from e
        kwargs = {"Bucket": bucket}
        if settings.S3_REGION != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.S3_REGION}
        try:
            self.s3.create_bucket(**kwargs)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in ("BucketAlreadyOwnedByYou",):
                raise
        if settings.CELL_KMS_KEY_ARN:
            self.s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [{
                "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms",
                                                       "KMSMasterKeyID": settings.CELL_KMS_KEY_ARN},
                "BucketKeyEnabled": True}]}, **self._owner())
        if not settings.S3_ENDPOINT_URL:
            self.s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True}, **self._owner())

    def put_if_absent(self, bucket: str, key: str, body: bytes, attempts: int = 5) -> bool:
        """Write the object only if the key does not exist. Returns True if written.

        Content-addressable keys make this the dedupe: identical content is stored once, and a
        re-run never stores a second copy.

        412 PreconditionFailed means the object is already there: done, nothing to write.
        409 ConditionalRequestConflict is different. It means a concurrent delete raced this
        write, and the object may now be absent, so S3 asks you to retry the PUT. Treating 409
        as "already there" would let a row commit pointing at content that does not exist.
        """
        for attempt in range(attempts):
            try:
                self.s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", **self._owner())
                return True
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code")
                status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code == "PreconditionFailed" or status == 412:
                    return False
                if code == "ConditionalRequestConflict" or status == 409:
                    if attempt == attempts - 1:
                        raise
                    time.sleep(0.1 * 2 ** attempt)
                    continue
                raise
        raise AssertionError("unreachable")

    def list_keys(self, bucket: str, prefix: str = "") -> set[str]:
        keys: set[str] = set()
        for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=prefix, **self._owner()):
            keys.update(o["Key"] for o in page.get("Contents", []))
        return keys

    def get_text(self, bucket: str, key: str) -> str:
        return self.s3.get_object(Bucket=bucket, Key=key, **self._owner())["Body"].read().decode("utf-8")
