"""
conftest.py — Shared pytest fixtures and test infrastructure for strands_s3_vectors_memory/ unit tests.

- src/ and examples/ are on sys.path via pyproject.toml [tool.pytest.ini_options] pythonpath.
- Sets minimal environment variables so module-level code in agent files does not
  raise KeyError at import time.
- Provides shared mock fixtures for boto3 clients and the strands Agent.

NOTE: Unit tests mock 'strands' in sys.modules at import time. Running unit and
integration tests in the same pytest process causes the mock to leak into the
integration tests. Always run them as separate commands:

    python -m pytest tests/unit/
    python -m pytest tests/integration/
"""

import json
import os

import pytest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Environment variables — set before any module-level code runs
# These are set at collection time (module level) so that imports of agent
# modules during test collection do not raise KeyError.
# ---------------------------------------------------------------------------

os.environ.setdefault("S3_VECTOR_BUCKET_NAME", "test-bucket")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("S3_VECTOR_TVM_ROLE_ARN", "arn:aws:iam::123456789012:role/TestTvmRole")

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_s3vectors_client():
    """Mock boto3 s3vectors client with sensible defaults."""
    client = MagicMock()
    client.put_vectors.return_value = {}
    client.query_vectors.return_value = {"vectors": []}
    return client


@pytest.fixture
def mock_bedrock_client():
    """Mock boto3 bedrock-runtime client returning a 1024-dim embedding."""
    client = MagicMock()
    body_mock = MagicMock()
    body_mock.read.return_value = json.dumps(
        {"embeddings": [{"embedding": [0.1] * 1024}]}
    ).encode()
    client.invoke_model.return_value = {"body": body_mock}
    return client


@pytest.fixture
def mock_sts_client():
    """Mock boto3 STS client returning test credentials from assume_role."""
    client = MagicMock()
    client.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AKIA_TEST",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
        }
    }
    return client


@pytest.fixture
def mock_strands_agent():
    """Mock strands Agent instance."""
    agent = MagicMock()
    agent.return_value = "mocked response"
    agent._session_manager = None
    agent.messages = []
    agent.system_prompt = ""
    agent.name = "test-agent"   # required by init_agent
    return agent
