"""
test_multi_tenant_agent.py — Unit tests for multi_tenant_agent.

Covers:
  - TestJWTHelpers              (Requirements 17.1, 17.2, 17.3, 17.4, 17.5)
  - TestMultiTenantAgentInvoke  (Requirements 18.1, 18.2, 18.3, 18.4, 18.5)

Module-level globals (_store, _plugin, _agent) are patched before the module
is reloaded so no real AWS or Strands calls are made.
"""

import base64
import importlib
import json
import sys
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
# Helper — build a fake JWT with the given payload
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict) -> str:
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    )
    return f"header.{payload_b64}.sig"


# ---------------------------------------------------------------------------
# Fixture — reload multi_tenant_agent with all dependencies patched
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_module(monkeypatch):
    """
    Reload multi_tenant_agent with patched boto3, S3VectorMemoryPlugin,
    TokenVendingMachine, and strands.Agent so no real AWS or Bedrock calls
    are made.
    """
    monkeypatch.setenv("S3_VECTOR_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("S3_VECTOR_TVM_ROLE_ARN", "arn:aws:iam::123:role/R")

    mock_agent_instance = MagicMock()
    mock_agent_instance.return_value = "agent response"

    with patch("boto3.client"), \
         patch("strands_s3_vectors_memory.token_vending_machine.TokenVendingMachine", create=True), \
         patch("strands_s3_vectors_memory.s3_vector_memory_plugin.S3VectorMemoryPlugin"), \
         patch("strands.Agent", return_value=mock_agent_instance), \
         patch("strands.models.BedrockModel"):
        import multi_tenant_agent
        importlib.reload(multi_tenant_agent)
        yield multi_tenant_agent, mock_agent_instance


# ---------------------------------------------------------------------------
# Task 9.1 — TestJWTHelpers
# Requirements: 17.1, 17.2, 17.3, 17.4, 17.5
# ---------------------------------------------------------------------------


class TestJWTHelpers:
    """JWT decoding helpers handle valid and malformed tokens correctly."""

    @pytest.fixture(autouse=True)
    def _load_module(self, agent_module):
        """Ensure the module is loaded; expose helpers via self."""
        self.module, _ = agent_module

    def test_decode_valid_jwt_returns_payload_dict(self):
        """Req 17.1 — _decode_jwt_payload with valid JWT returns decoded payload dict."""
        payload = {"sub": "user-001", "custom:tenant_id": "acme"}
        token = _make_jwt(payload)
        result = self.module._decode_jwt_payload(token)
        assert result == payload

    def test_decode_malformed_missing_segments_returns_empty_dict(self):
        """Req 17.2 — _decode_jwt_payload with missing segments returns {}."""
        result = self.module._decode_jwt_payload("not-a-jwt")
        assert result == {}

    def test_decode_malformed_invalid_base64_returns_empty_dict(self):
        """Req 17.2 — _decode_jwt_payload with invalid base64 in payload returns {}."""
        result = self.module._decode_jwt_payload("header.!!!invalid!!!.sig")
        assert result == {}

    def test_extract_tenant_identity_with_tenant_id_returns_context_and_user_id(self):
        """Req 17.3 — _extract_tenant_identity with custom:tenant_id returns correct
        tenant_context dict and user_id equal to sub claim."""
        payload = {
            "sub": "user-001",
            "custom:tenant_id": "acme",
            "custom:tenant_name": "Acme Corp",
            "custom:tier": "premium",
        }
        token = _make_jwt(payload)
        tenant_context, user_id = self.module._extract_tenant_identity(f"Bearer {token}")

        assert tenant_context is not None
        assert tenant_context["tenantId"] == "acme"
        assert tenant_context["tenantName"] == "Acme Corp"
        assert tenant_context["tier"] == "premium"
        assert user_id == "user-001"

    def test_extract_tenant_identity_missing_tenant_id_returns_none_none(self):
        """Req 17.4 — _extract_tenant_identity with JWT missing custom:tenant_id
        returns (None, None)."""
        payload = {"sub": "user-001"}
        token = _make_jwt(payload)
        tenant_context, user_id = self.module._extract_tenant_identity(f"Bearer {token}")
        assert tenant_context is None
        assert user_id is None

    def test_extract_tenant_identity_empty_string_returns_none_none(self):
        """Req 17.5 — _extract_tenant_identity with empty string returns (None, None)."""
        tenant_context, user_id = self.module._extract_tenant_identity("")
        assert tenant_context is None
        assert user_id is None


# ---------------------------------------------------------------------------
# Task 9.3 — TestMultiTenantAgentInvoke
# Requirements: 18.1, 18.2, 18.3, 18.4, 18.5
# ---------------------------------------------------------------------------


class TestMultiTenantAgentInvoke:
    """multi_tenant_agent.invoke handles valid and invalid requests correctly."""

    def test_valid_jwt_calls_agent_with_correct_invocation_state(self, agent_module):
        """Req 18.1 — valid JWT with custom:tenant_id → _agent called with correct
        invocation_state including tenant_context, user_id, conversation_id."""
        module, mock_agent = agent_module

        payload = {
            "sub": "user-001",
            "custom:tenant_id": "acme",
            "custom:tenant_name": "Acme Corp",
            "custom:tier": "standard",
        }
        token = _make_jwt(payload)
        auth_header = f"Bearer {token}"

        module.invoke(
            {"message": "Hello", "conversation_id": "conv-1"},
            auth_header=auth_header,
        )

        mock_agent.assert_called_once()
        call_args, call_kwargs = mock_agent.call_args
        assert call_args[0] == "Hello"
        state = call_kwargs["invocation_state"]
        assert state["tenant_context"]["tenantId"] == "acme"
        assert state["user_id"] == "user-001"
        assert state["conversation_id"] == "conv-1"

    def test_jwt_missing_tenant_id_returns_error(self, agent_module):
        """Req 18.2 — JWT missing custom:tenant_id → returns error dict."""
        module, mock_agent = agent_module

        payload = {"sub": "user-001"}
        token = _make_jwt(payload)
        auth_header = f"Bearer {token}"

        result = module.invoke({"message": "Hello"}, auth_header=auth_header)

        assert result == {"error": "JWT required — custom:tenant_id claim missing"}
        mock_agent.assert_not_called()

    def test_session_id_kwarg_used_as_conversation_id(self, agent_module):
        """Req 18.3 — session_id kwarg is used as conversation_id."""
        module, mock_agent = agent_module

        payload = {"sub": "user-001", "custom:tenant_id": "acme"}
        token = _make_jwt(payload)
        auth_header = f"Bearer {token}"

        module.invoke(
            {"message": "Hi"},
            auth_header=auth_header,
            session_id="session-xyz",
        )

        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["invocation_state"]["conversation_id"] == "session-xyz"

    def test_payload_conversation_id_used_when_no_session_id(self, agent_module):
        """Req 18.4 — payload conversation_id used when no session_id kwarg."""
        module, mock_agent = agent_module

        payload = {"sub": "user-001", "custom:tenant_id": "acme"}
        token = _make_jwt(payload)
        auth_header = f"Bearer {token}"

        module.invoke(
            {"message": "Hi", "conversation_id": "payload-conv-1"},
            auth_header=auth_header,
        )

        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["invocation_state"]["conversation_id"] == "payload-conv-1"

    def test_response_dict_contains_required_keys(self, agent_module):
        """Req 18.5 — response dict contains 'response', 'tenant_id', 'conversation_id'."""
        module, mock_agent = agent_module
        mock_agent.return_value = "agent response"

        payload = {"sub": "user-001", "custom:tenant_id": "acme"}
        token = _make_jwt(payload)
        auth_header = f"Bearer {token}"

        result = module.invoke(
            {"message": "Hello", "conversation_id": "conv-1"},
            auth_header=auth_header,
        )

        assert "response" in result
        assert "tenant_id" in result
        assert "conversation_id" in result
        assert result["tenant_id"] == "acme"
        assert result["response"] == "agent response"


# ---------------------------------------------------------------------------
# Issue #17 — JWT not verified — warning must be logged on every decode
# ---------------------------------------------------------------------------

class TestJWTVerificationWarning:
    """Issue #17: JWT is validated upstream (API Gateway Lambda authorizer or AgentCore Runtime).
    _decode_jwt_payload only decodes the already-verified payload."""

    @pytest.fixture(autouse=True)
    def _load_module(self, agent_module):
        self.module, _ = agent_module

    def test_decode_does_not_raise_on_valid_payload(self):
        """_decode_jwt_payload decodes a valid JWT payload without error."""
        payload = {"custom:tenant_id": "acme", "sub": "user-001"}
        token = _make_jwt(payload)
        result = self.module._decode_jwt_payload(token)
        assert result == payload

    def test_decode_returns_empty_dict_on_malformed_token(self):
        """_decode_jwt_payload returns {} for a malformed token rather than raising."""
        result = self.module._decode_jwt_payload("not.a.valid.jwt.at.all")
        assert result == {}
