"""
Shared fixtures and index lifecycle for integration tests.

All tests in tests/integration/ depend on this conftest. If S3_VECTOR_BUCKET_NAME
is not set the entire module is skipped (Req 1.4).

src/ is on sys.path via pyproject.toml [tool.pytest.ini_options] pythonpath.
"""

import logging
import os

import boto3
import pytest

# ---------------------------------------------------------------------------
# Constants — imported from _constants so test files can share the same values
# ---------------------------------------------------------------------------
from tests.integration._constants import RUN_ID, BUCKET_NAME, TVM_ROLE_ARN, AWS_REGION, BEDROCK_MODEL_ID  # noqa: E402

# ---------------------------------------------------------------------------
# Guard: skip all tests when the required bucket env var is absent (Req 1.4)
# ---------------------------------------------------------------------------
if not BUCKET_NAME:
    pytest.skip(
        "S3_VECTOR_BUCKET_NAME is not set — skipping all integration tests",
        allow_module_level=True,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _create_index(client, bucket: str, name: str) -> None:
    """Create an S3 Vectors index idempotently (Reqs 1.2, 1.7)."""
    try:
        client.create_index(
            vectorBucketName=bucket,
            indexName=name,
            dataType="float32",
            dimension=1024,
            distanceMetric="cosine",
            metadataConfiguration={
                "nonFilterableMetadataKeys": ["content", "stored_at", "conversation_id", "type"]
            },
        )
    except client.exceptions.ConflictException:
        pass  # idempotent — index already exists


def _delete_index_best_effort(client, bucket: str, name: str) -> None:
    """Delete an S3 Vectors index, logging a warning on failure (Req 1.3)."""
    try:
        client.delete_index(vectorBucketName=bucket, indexName=name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to delete index %r from bucket %r: %s", name, bucket, exc)


# ---------------------------------------------------------------------------
# Module-scoped fixtures (Reqs 1.1, 1.6, 10.4)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def s3v_client():
    """Boto3 client for the s3vectors service."""
    return boto3.client("s3vectors", region_name=AWS_REGION)


@pytest.fixture(scope="module")
def bedrock_client():
    """Boto3 client for bedrock-runtime (embeddings)."""
    return boto3.client("bedrock-runtime", region_name=AWS_REGION)


@pytest.fixture(scope="module")
def memory_index(s3v_client):
    """Create the single-tenant 'memory' index; yield its name; delete on teardown."""
    _create_index(s3v_client, BUCKET_NAME, "memory")
    yield "memory"
    _delete_index_best_effort(s3v_client, BUCKET_NAME, "memory")


@pytest.fixture(scope="module")
def tenant_a_index(s3v_client):
    """Create the tenant-A index; yield its name; delete on teardown."""
    index_name = f"memory-tenant-a-{RUN_ID}"
    _create_index(s3v_client, BUCKET_NAME, index_name)
    yield index_name
    _delete_index_best_effort(s3v_client, BUCKET_NAME, index_name)


@pytest.fixture(scope="module")
def tenant_b_index(s3v_client):
    """Create the tenant-B index; yield its name; delete on teardown."""
    index_name = f"memory-tenant-b-{RUN_ID}"
    _create_index(s3v_client, BUCKET_NAME, index_name)
    yield index_name
    _delete_index_best_effort(s3v_client, BUCKET_NAME, index_name)
