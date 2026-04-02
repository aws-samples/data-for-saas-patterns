"""
test_single_tenant_agent.py — Unit tests for single_tenant_agent.invoke.

Covers:
  - TestSingleTenantAgentInvoke (Requirements 16.1, 16.2, 16.3)

Module-level globals (_store, _plugin, _agent) are patched before the module
is reloaded so no real AWS or Strands calls are made.
"""

import sys
import importlib
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Mock strands modules BEFORE any import that might trigger them
# ---------------------------------------------------------------------------

strands_mock = MagicMock()
strands_models_mock = MagicMock()
strands_plugins_mock = MagicMock()
strands_hooks_mock = MagicMock()

# @hook decorator must be a pass-through
strands_plugins_mock.hook = lambda f: f


class FakePlugin:
    def __init__(self):
        pass


strands_mock.Plugin = FakePlugin
strands_mock.Agent = MagicMock()
strands_models_mock.BedrockModel = MagicMock()

sys.modules.setdefault("strands", strands_mock)
sys.modules.setdefault("strands.models", strands_models_mock)
sys.modules.setdefault("strands.plugins", strands_plugins_mock)
sys.modules.setdefault("strands.hooks", strands_hooks_mock)

# ---------------------------------------------------------------------------
# Now safe to import pytest and other test utilities
# ---------------------------------------------------------------------------

import pytest

# ---------------------------------------------------------------------------
# Fixture — reload single_tenant_agent with all dependencies patched
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_module(monkeypatch):
    """
    Reload single_tenant_agent with patched boto3, S3VectorMemoryPlugin,
    and strands.Agent so no real AWS or Bedrock calls are made.
    """
    monkeypatch.setenv("S3_VECTOR_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    # The mock agent instance: calling it returns a string-like object
    mock_agent_instance = MagicMock()
    mock_agent_instance.return_value = "agent response"

    with patch("boto3.client"), \
         patch("strands_s3_vectors_memory.s3_vector_memory_plugin.S3VectorMemoryPlugin"), \
         patch("strands.Agent", return_value=mock_agent_instance), \
         patch("strands.models.BedrockModel"):
        import single_tenant_agent
        importlib.reload(single_tenant_agent)
        yield single_tenant_agent, mock_agent_instance


# ---------------------------------------------------------------------------
# Task 8.1 — TestSingleTenantAgentInvoke
# Requirements: 16.1, 16.2, 16.3
# ---------------------------------------------------------------------------


class TestSingleTenantAgentInvoke:
    """single_tenant_agent.invoke passes correct invocation_state to _agent."""

    def test_invoke_calls_agent_with_message_and_invocation_state(self, agent_module):
        """Req 16.1 — invoke calls _agent with message and invocation_state containing
        user_id, conversation_id, and end_session=False."""
        module, mock_agent = agent_module

        module.invoke("user-1", "conv-1", "Hello")

        mock_agent.assert_called_once_with(
            "Hello",
            invocation_state={
                "user_id": "user-1",
                "conversation_id": "conv-1",
                "end_session": False,
            },
        )

    def test_invoke_with_end_session_true_passes_end_session_true(self, agent_module):
        """Req 16.2 — invoke(..., end_session=True) passes end_session=True in invocation_state."""
        module, mock_agent = agent_module

        module.invoke("user-2", "conv-2", "Goodbye", end_session=True)

        mock_agent.assert_called_once_with(
            "Goodbye",
            invocation_state={
                "user_id": "user-2",
                "conversation_id": "conv-2",
                "end_session": True,
            },
        )

    def test_invoke_returns_agent_response_as_str(self, agent_module):
        """Req 16.3 — invoke returns the agent response as a str."""
        module, mock_agent = agent_module
        mock_agent.return_value = "mocked response"

        result = module.invoke("user-3", "conv-3", "What's up?")

        assert isinstance(result, str)
        assert result == "mocked response"

    def test_invoke_default_end_session_is_false(self, agent_module):
        """Req 16.1 — end_session defaults to False when not provided."""
        module, mock_agent = agent_module

        module.invoke("user-4", "conv-4", "Hi")

        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["invocation_state"]["end_session"] is False
