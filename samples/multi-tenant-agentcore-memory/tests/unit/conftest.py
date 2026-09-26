"""
conftest.py — Shared fixtures and import stubs for agentcore_memory unit tests.

The session manager imports `strands` and `bedrock_agentcore`. To keep the unit
tests dependency-free (no live AWS, no heavy SDK install required), we install
lightweight stub modules into sys.modules before those imports run.

src/ and examples/ are on sys.path via pyproject.toml [tool.pytest.ini_options].
"""

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Environment — set before any module-level code runs
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AGENTCORE_MEMORY_ID", "mem-test-000")
os.environ.setdefault("AGENTCORE_TVM_ROLE_ARN", "arn:aws:iam::123456789012:role/TestTvmRole")


# ---------------------------------------------------------------------------
# Stub external SDKs so agentcore_memory_session_manager imports cleanly.
# ---------------------------------------------------------------------------
def _install_strands_stub():
    if "strands.hooks" in sys.modules:
        return

    strands = types.ModuleType("strands")
    hooks = types.ModuleType("strands.hooks")
    registry = types.ModuleType("strands.hooks.registry")

    class BeforeInvocationEvent:  # marker type used as a hook key
        pass

    class HookRegistry:
        def add_callback(self, *args, **kwargs):
            pass

    hooks.BeforeInvocationEvent = BeforeInvocationEvent
    registry.HookRegistry = HookRegistry
    hooks.registry = registry
    strands.hooks = hooks

    sys.modules["strands"] = strands
    sys.modules["strands.hooks"] = hooks
    sys.modules["strands.hooks.registry"] = registry


def _install_bedrock_agentcore_stub():
    if "bedrock_agentcore.memory.integrations.strands.session_manager" in sys.modules:
        return

    base = types.ModuleType("bedrock_agentcore")
    memory = types.ModuleType("bedrock_agentcore.memory")
    integrations = types.ModuleType("bedrock_agentcore.memory.integrations")
    strands_int = types.ModuleType("bedrock_agentcore.memory.integrations.strands")
    config = types.ModuleType("bedrock_agentcore.memory.integrations.strands.config")
    sm = types.ModuleType("bedrock_agentcore.memory.integrations.strands.session_manager")

    class RetrievalConfig:
        def __init__(self, top_k=10, relevance_score=0.2, **kwargs):
            self.top_k = top_k
            self.relevance_score = relevance_score

    class AgentCoreMemoryConfig:
        def __init__(self, memory_id=None, actor_id=None, session_id=None,
                     retrieval_config=None, **kwargs):
            self.memory_id        = memory_id
            self.actor_id         = actor_id
            self.session_id       = session_id
            self.retrieval_config = retrieval_config

    class AgentCoreMemorySessionManager:
        """Minimal stand-in for the real base class."""

        def __init__(self, agentcore_memory_config=None, region_name=None, **kwargs):
            self.config         = agentcore_memory_config
            self.region_name    = region_name
            self.session_id     = getattr(agentcore_memory_config, "session_id", None)
            self.memory_client  = MagicMock()

        def register_hooks(self, registry, **kwargs):
            pass

    config.RetrievalConfig = RetrievalConfig
    sm.AgentCoreMemoryConfig = AgentCoreMemoryConfig
    sm.AgentCoreMemorySessionManager = AgentCoreMemorySessionManager

    sys.modules["bedrock_agentcore"] = base
    sys.modules["bedrock_agentcore.memory"] = memory
    sys.modules["bedrock_agentcore.memory.integrations"] = integrations
    sys.modules["bedrock_agentcore.memory.integrations.strands"] = strands_int
    sys.modules["bedrock_agentcore.memory.integrations.strands.config"] = config
    sys.modules["bedrock_agentcore.memory.integrations.strands.session_manager"] = sm


_install_strands_stub()
_install_bedrock_agentcore_stub()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
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
