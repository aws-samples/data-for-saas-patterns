"""Connection settings.

Each database is configured either by a full DSN (`<PREFIX>_DSN`) or by parts
(`<PREFIX>_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_DBNAME`, `_SSLMODE`), which is how the
AWS task receives Aurora credentials from Secrets Manager. Defaults match the local stack.
"""
import os
from urllib.parse import quote


def _dsn(prefix: str, port: int, dbname: str) -> str:
    if os.getenv(f"{prefix}_DSN"):
        return os.environ[f"{prefix}_DSN"]
    if os.getenv(f"{prefix}_HOST"):
        user = quote(os.getenv(f"{prefix}_USER", "postgres"))
        pwd = quote(os.getenv(f"{prefix}_PASSWORD", ""), safe="")
        return (f"postgresql://{user}:{pwd}@{os.environ[f'{prefix}_HOST']}:{os.getenv(f'{prefix}_PORT', '5432')}"
                f"/{os.getenv(f'{prefix}_DBNAME', dbname)}?sslmode={os.getenv(f'{prefix}_SSLMODE', 'require')}")
    return f"postgresql://postgres:local@localhost:{port}/{dbname}"


V1_MESSAGING_DSN = _dsn("V1_MESSAGING", 5415, "messaging")
V1_AUTH_DSN = _dsn("V1_AUTH", 5416, "auth")
V2_CELL_MESSAGING_DSN = _dsn("V2_CELL_MESSAGING", 5418, "messaging")
V2_MANAGEMENT_DSN = _dsn("V2_MANAGEMENT", 5419, "management")


READER_USER = "tenantmig_reader"
READER_PASSWORD = os.getenv("TENANTMIG_READER_PASSWORD", "local-reader")


def _fdw(prefix: str, local_port: int, dbname: str) -> dict:
    """How the V2 management DB reaches a V1 database for postgres_fdw."""
    return dict(
        host=os.getenv(f"FDW_{prefix}_HOST") or os.getenv(f"{prefix}_HOST", "localhost"),
        port=os.getenv(f"FDW_{prefix}_PORT") or os.getenv(f"{prefix}_PORT",
                                                          "5432" if os.getenv(f"{prefix}_HOST") else str(local_port)),
        dbname=os.getenv(f"{prefix}_DBNAME", dbname),
        # The fdw connects as the read-only migration role, never as the master user.
        reader_user=READER_USER,
        reader_password=READER_PASSWORD,
        sslmode=os.getenv(f"{prefix}_SSLMODE", "require" if os.getenv(f"{prefix}_HOST") else "disable"),
    )


FDW_V1_MESSAGING = _fdw("V1_MESSAGING", 5415, "messaging")
FDW_V1_AUTH = _fdw("V1_AUTH", 5416, "auth")

# Set S3_ENDPOINT_URL only for local MinIO; in AWS the task role is used.
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "" if os.getenv("V1_MESSAGING_HOST") else "http://localhost:9000") or None
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "local") if S3_ENDPOINT_URL else None
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "localsecret") if S3_ENDPOINT_URL else None
S3_REGION = os.getenv("AWS_REGION", "us-east-2")
ORG_BUCKET_PREFIX = os.getenv("ORG_BUCKET_PREFIX", "tm-org-")
# Concurrent S3 PUTs per batch. Per-object latency, not bandwidth, dominates small-object copies.
S3_WORKERS = int(os.getenv("S3_WORKERS", "32"))
# V1 shared bucket (one bucket, org UUID prefix) and S3 Batch Operations wiring for step 8.
V1_SHARED_BUCKET = os.getenv("V1_SHARED_BUCKET", "tm-local-v1-shared")
BATCH_OPS_ROLE_ARN = os.getenv("BATCH_OPS_ROLE_ARN")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")
# Migration artefacts (Batch Operations manifests and reports) go to their own bucket.
MIGRATION_BUCKET = os.getenv("MIGRATION_BUCKET", "tm-local-migration")
BATCH_TIMEOUT_SECONDS = int(os.getenv("BATCH_TIMEOUT_SECONDS", "3600"))
# Delta passes only copy rows older than this, so a lower id committing late isn't skipped.
# The final pass at cutover uses no margin and an anti-join (see content_job).
DELTA_SAFETY_SECONDS = int(os.getenv("DELTA_SAFETY_SECONDS", "60"))
# How long the router may serve a cached tenant_placement. Cutover waits this long after
# freezing, so writes accepted before the cache refreshed land before the final pass.
PLACEMENT_CACHE_TTL_SECONDS = int(os.getenv("PLACEMENT_CACHE_TTL_SECONDS", "30"))
# reset and purge-buckets delete data; they only run where this is set (local stack, sandbox).
SANDBOX = os.getenv("TENANTMIG_SANDBOX") == "1"
# The cell this sample migrates tenants into.
CELL_ID = os.getenv("CELL_ID", "cell-1")
# In AWS, per-org buckets are encrypted with this cell KMS key.
CELL_KMS_KEY_ARN = os.getenv("CELL_KMS_KEY_ARN")
