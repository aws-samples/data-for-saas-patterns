"""Per-organisation content store on S3, written with conditional PUTs."""
import base64
import hashlib
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

    def owner_kwargs(self) -> dict:
        """ExpectedBucketOwner, so a bucket someone else pre-created under a guessable name
        fails the call instead of silently receiving a tenant's content. MinIO ignores it."""
        return {"ExpectedBucketOwner": settings.EXPECTED_BUCKET_OWNER} if settings.EXPECTED_BUCKET_OWNER else {}

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self.s3.head_bucket(Bucket=bucket, **self.owner_kwargs())
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
                "BucketKeyEnabled": True}]}, **self.owner_kwargs())
        if not settings.S3_ENDPOINT_URL:
            self.s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True}, **self.owner_kwargs())

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
                self.s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", **self.owner_kwargs())
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
                Bucket=bucket, Prefix=prefix, **self.owner_kwargs()):
            keys.update(o["Key"] for o in page.get("Contents", []))
        return keys

    def list_objects(self, bucket: str, prefix: str = "") -> dict[str, tuple[int, str]]:
        """Key -> (size, ETag) for every object under the prefix."""
        found = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=prefix, **self.owner_kwargs()):
            for o in page.get("Contents", []):
                found[o["Key"]] = (o["Size"], o["ETag"].strip('"'))
        return found

    def same_content(self, a_bucket: str, a_key: str, a: tuple[int, str],
                     b_bucket: str, b_key: str, b: tuple[int, str]) -> bool:
        """Are these two objects byte-for-byte the same? `a` and `b` are (size, ETag) from list_objects.

        The ETag is the MD5 of the body only for single-part objects encrypted with S3-managed keys.
        Multipart objects and KMS-encrypted objects don't have one: a copy of a multipart object,
        or a copy into a KMS bucket, has a different ETag even when the bytes match (both verified
        on S3). So unless both sides are plain single-part objects, compare a full-object SHA-256,
        read from the stored checksum when there is one and otherwise computed from the body.
        """
        if a[0] != b[0]:
            return False
        if "-" not in a[1] and "-" not in b[1] and not (self._kms(a_bucket, a_key) or self._kms(b_bucket, b_key)):
            return a[1] == b[1]
        return self.sha256(a_bucket, a_key) == self.sha256(b_bucket, b_key)

    def _kms(self, bucket: str, key: str) -> bool:
        head = self.s3.head_object(Bucket=bucket, Key=key, **self.owner_kwargs())
        return head.get("ServerSideEncryption") == "aws:kms"

    def sha256(self, bucket: str, key: str) -> str:
        head = self.s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED", **self.owner_kwargs())
        if head.get("ChecksumType") == "FULL_OBJECT" and head.get("ChecksumSHA256"):
            return base64.b64decode(head["ChecksumSHA256"]).hex()
        digest = hashlib.sha256()
        for chunk in self.s3.get_object(Bucket=bucket, Key=key, **self.owner_kwargs())["Body"].iter_chunks(1 << 20):
            digest.update(chunk)
        return digest.hexdigest()

    def get_text(self, bucket: str, key: str) -> str:
        return self.s3.get_object(Bucket=bucket, Key=key, **self.owner_kwargs())["Body"].read().decode("utf-8")
