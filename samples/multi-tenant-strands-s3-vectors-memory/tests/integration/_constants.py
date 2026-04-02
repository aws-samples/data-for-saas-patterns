"""
Shared constants for integration tests.

Both conftest.py and the test files import from here so that RUN_ID,
BUCKET_NAME, etc. are defined exactly once per process.
"""

import os
import uuid

RUN_ID: str = uuid.uuid4().hex[:8]

BUCKET_NAME: str | None = os.environ.get("S3_VECTOR_BUCKET_NAME")
TVM_ROLE_ARN: str | None = os.environ.get("S3_VECTOR_TVM_ROLE_ARN")
AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID: str = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
