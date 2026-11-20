# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
conftest.py — Shared fixtures and import stubs for agentcore_memory unit tests.

The session manager imports `strands` and `bedrock_agentcore`. To keep the unit
tests dependency-free (no live AWS, no heavy SDK install required), we install
lightweight stub modules into sys.modules before those imports run.

src/ and examples/ are on sys.path via pyproject.toml [tool.pytest.ini_options].
"""

import enum
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
    agent_pkg = types.ModuleType("strands.agent")
    state_mod = types.ModuleType("strands.agent.state")
    types_pkg = types.ModuleType("strands.types")
    session_mod = types.ModuleType("strands.types.session")

    class BeforeInvocationEvent:  # marker type used as a hook key
        pass

    class HookRegistry:
        def add_callback(self, *args, **kwargs):
            pass

    class AgentState:
        def __init__(self, initial=None):
            self._state = dict(initial or {})

        def get(self, key=None):
            return dict(self._state) if key is None else self._state.get(key)

        def set(self, key, value):
            self._state[key] = value

    class SessionAgent:
        """Snapshot of the attributes the session manager restores."""

        def __init__(self, agent_id, state, conversation_manager_state, internal_state):
            self.agent_id = agent_id
            self.state = state
            self.conversation_manager_state = conversation_manager_state
            self._internal_state = internal_state

        @classmethod
        def from_agent(cls, agent):
            return cls(
                agent.agent_id,
                agent.state.get(),
                agent.conversation_manager.get_state(),
                {"model_state": agent._model_state},
            )

        def initialize_internal_state(self, agent):
            agent._model_state = self._internal_state["model_state"]

    hooks.BeforeInvocationEvent = BeforeInvocationEvent
    registry.HookRegistry = HookRegistry
    hooks.registry = registry
    strands.hooks = hooks
    state_mod.AgentState = AgentState
    session_mod.SessionAgent = SessionAgent

    sys.modules["strands"] = strands
    sys.modules["strands.hooks"] = hooks
    sys.modules["strands.hooks.registry"] = registry
    sys.modules["strands.agent"] = agent_pkg
    sys.modules["strands.agent.state"] = state_mod
    sys.modules["strands.types"] = types_pkg
    sys.modules["strands.types.session"] = session_mod


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

    class PersistenceMode(str, enum.Enum):
        FULL = "FULL"
        NONE = "NONE"

    class AgentCoreMemoryConfig:
        def __init__(self, memory_id=None, actor_id=None, session_id=None,
                     retrieval_config=None, persistence_mode=PersistenceMode.FULL, **kwargs):
            self.memory_id        = memory_id
            self.actor_id         = actor_id
            self.session_id       = session_id
            self.retrieval_config = retrieval_config
            self.persistence_mode = persistence_mode

    class AgentCoreMemorySessionManager:
        """Minimal stand-in for the real base class."""

        def __init__(self, agentcore_memory_config=None, region_name=None,
                     boto_session=None, **kwargs):
            self.config         = agentcore_memory_config
            self.region_name    = region_name
            self.boto_session   = boto_session
            self.persistence_mode = getattr(agentcore_memory_config, "persistence_mode", None)
            self.session_id     = getattr(agentcore_memory_config, "session_id", None)
            self.memory_client  = MagicMock()
            self.converter      = MagicMock()
            self._latest_agent_message = {}
            self._last_synced_internal_state = {}
            self._held_records = {}

        def register_hooks(self, registry, **kwargs):
            pass

        def initialize(self, agent, **kwargs):
            self._latest_agent_message[agent.agent_id] = None

        def read_agent(self, session_id, agent_id, **kwargs):
            return None

        def read_session(self, session_id, **kwargs):
            return None

        def create_agent(self, session_id, session_agent, **kwargs):
            self.created_agents = getattr(self, "created_agents", []) + [(session_id, session_agent)]

        def _fix_broken_tool_use(self, messages):
            return messages

        def list_messages(self, session_id, agent_id, limit=None, offset=0, **kwargs):
            return []

    config.RetrievalConfig = RetrievalConfig
    config.PersistenceMode = PersistenceMode
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
            "SecretAccessKey": "secret",  # pragma: allowlist secret — test fixture, not a credential
            "SessionToken": "token",
        }
    }
    return client
