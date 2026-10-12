"""Per-organisation content store on S3, written with conditional PUTs."""
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

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self.s3.head_bucket(Bucket=bucket)
        except ClientError:
            kwargs = {"Bucket": bucket}
            if settings.S3_REGION != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.S3_REGION}
            self.s3.create_bucket(**kwargs)
            if settings.CELL_KMS_KEY_ARN:
                self.s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [{
                    "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms",
                                                           "KMSMasterKeyID": settings.CELL_KMS_KEY_ARN},
                    "BucketKeyEnabled": True}]})
            if not settings.S3_ENDPOINT_URL:
                self.s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True, "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True, "RestrictPublicBuckets": True})

    def put_if_absent(self, bucket: str, key: str, body: bytes) -> bool:
        """Write the object only if the key does not exist. Returns True if written.

        Content-addressable keys make this the dedupe: identical content is stored once,
        and a re-run never rewrites (or re-pays for) objects that already exist.
        """
        try:
            self.s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*")
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412):
                return False
            raise

    def list_keys(self, bucket: str) -> set[str]:
        keys: set[str] = set()
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            keys.update(o["Key"] for o in page.get("Contents", []))
        return keys

    def get_text(self, bucket: str, key: str) -> str:
        return self.s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
