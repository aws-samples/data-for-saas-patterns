"""
test_session_manager.py — Unit tests for MultiTenantAgentCoreMemorySessionManager.

External SDKs (strands, bedrock_agentcore) are stubbed in conftest.py, and the
TokenVendingMachine is patched so no STS/boto3 calls are made.
"""

import pytest
from unittest.mock import MagicMock, patch

from agentcore_memory import (
    MultiTenantAgentCoreMemorySessionManager,
    AGENT_NAME,
    IsolationError,
)

MEMORY_ID = "mem-abc-123"
TVM_ARN   = "arn:aws:iam::123456789012:role/TestTvmRole"

TENANT_1 = {"tenantId": "tenant-001", "tenantName": "Acme Corp"}
TENANT_2 = {"tenantId": "tenant-002", "tenantName": "Globex Inc"}


def _make_manager():
    """Build a session manager with the TVM patched out."""
    with patch("agentcore_memory.agentcore_memory_session_manager.TokenVendingMachine") as tvm_cls:
        tvm_cls.return_value = MagicMock()
        mgr = MultiTenantAgentCoreMemorySessionManager(
            memory_id=MEMORY_ID, tvm_role_arn=TVM_ARN, region_name="us-east-1",
        )
    return mgr


class TestConstruction:
    def test_missing_memory_id_raises_value_error(self):
        with pytest.raises(ValueError, match="memory_id"):
            MultiTenantAgentCoreMemorySessionManager(memory_id="", tvm_role_arn=TVM_ARN)

    def test_missing_tvm_role_arn_raises_value_error(self, monkeypatch):
        # Ensure the env fallback is absent so the guard fires.
        monkeypatch.delenv("AGENTCORE_TVM_ROLE_ARN", raising=False)
        with pytest.raises(ValueError, match="tvm_role_arn"):
            MultiTenantAgentCoreMemorySessionManager(memory_id=MEMORY_ID, tvm_role_arn=None)

    def test_valid_construction_sets_attributes(self):
        mgr = _make_manager()
        assert mgr.memory_id == MEMORY_ID
        assert mgr.agent_name == AGENT_NAME
        assert mgr.region_name == "us-east-1"

    def test_retrieval_config_uses_tenant_scoped_namespaces(self):
        mgr = _make_manager()
        # Long-term recall must be configured against the {actorId} namespaces so
        # each resolved path stays within the tenant's ABAC-allowed prefix. Both
        # strategy namespaces must be present (preferences + facts).
        assert mgr.config.retrieval_config is not None
        assert "/{actorId}/preferences" in mgr.config.retrieval_config
        assert "/{actorId}/facts" in mgr.config.retrieval_config


class TestBuildActorId:
    def test_actor_id_format(self):
        mgr = _make_manager()
        assert mgr._build_actor_id(TENANT_1, "user-456") == f"tenant-001:{AGENT_NAME}:user-456"

    def test_actor_id_embeds_tenant(self):
        mgr = _make_manager()
        assert mgr._build_actor_id(TENANT_1, "u").startswith("tenant-001:")
        assert mgr._build_actor_id(TENANT_2, "u").startswith("tenant-002:")

    def test_missing_tenant_id_raises(self):
        mgr = _make_manager()
        with pytest.raises(IsolationError, match="tenantId"):
            mgr._build_actor_id({}, "user-456")


class TestBuildSessionId:
    def test_session_id_format(self):
        mgr = _make_manager()
        result = mgr._build_session_id(TENANT_1, "user-456", "conv-001")
        assert result == f"tenant-001-{AGENT_NAME}-user-456-conv-001"

    def test_missing_tenant_id_raises(self):
        mgr = _make_manager()
        with pytest.raises(IsolationError, match="tenantId"):
            mgr._build_session_id({}, "user-456", "conv-001")


def _make_agent(agent_id="agent-1"):
    """Minimal agent stub with the attributes _prepare touches."""
    agent = MagicMock()
    agent.agent_id = agent_id
    agent.messages = []
    return agent


class TestPrepare:
    def test_prepare_sets_scoped_identifiers_and_swaps_clients(self):
        mgr = _make_manager()

        scoped_session = MagicMock()
        gmcp = MagicMock()
        gmdp = MagicMock()
        # session.client(...) returns control client first, data client second
        scoped_session.client.side_effect = [gmcp, gmdp]
        mgr._tvm.get_session.return_value = scoped_session
        mgr.list_messages = MagicMock(return_value=[])

        mgr._prepare(_make_agent(), TENANT_1, "user-456", "conv-001")

        assert mgr.config.actor_id   == f"tenant-001:{AGENT_NAME}:user-456"
        assert mgr.config.session_id == f"tenant-001-{AGENT_NAME}-user-456-conv-001"
        assert mgr.session_id        == mgr.config.session_id
        mgr._tvm.get_session.assert_called_once_with(TENANT_1)
        assert mgr.memory_client.gmcp_client is gmcp
        assert mgr.memory_client.gmdp_client is gmdp

    def test_prepare_sets_two_namespace_retrieval_config(self):
        mgr = _make_manager()
        mgr._tvm.get_session.return_value = MagicMock()
        mgr.list_messages = MagicMock(return_value=[])

        mgr._prepare(_make_agent(), TENANT_1, "user-456", "conv-001")

        assert "/{actorId}/preferences" in mgr.config.retrieval_config
        assert "/{actorId}/facts" in mgr.config.retrieval_config

    def test_prepare_uses_scoped_session_per_tenant(self):
        mgr = _make_manager()
        mgr._tvm.get_session.return_value = MagicMock()
        mgr.list_messages = MagicMock(return_value=[])

        mgr._prepare(_make_agent(), TENANT_1, "user-1", "conv-1")
        actor_1 = mgr.config.actor_id
        mgr._prepare(_make_agent(), TENANT_2, "user-1", "conv-1")
        actor_2 = mgr.config.actor_id

        assert actor_1 != actor_2
        assert actor_1.startswith("tenant-001:")
        assert actor_2.startswith("tenant-002:")

    def test_prepare_rehydrates_stm_on_session_change(self):
        """On session change, agent.messages is restored from list_messages (STM)."""
        mgr = _make_manager()
        mgr._tvm.get_session.return_value = MagicMock()

        msg = MagicMock()
        msg.to_message.return_value = {"role": "user", "content": [{"text": "hi"}]}
        mgr.list_messages = MagicMock(return_value=[msg])

        agent = _make_agent()
        mgr._prepare(agent, TENANT_1, "user-456", "conv-001")

        mgr.list_messages.assert_called_once_with(
            f"tenant-001-{AGENT_NAME}-user-456-conv-001", agent.agent_id
        )
        assert agent.messages == [{"role": "user", "content": [{"text": "hi"}]}]

    def test_prepare_skips_rehydrate_when_session_unchanged(self):
        """Same session across two turns: rehydrate only fires on the first (change)."""
        mgr = _make_manager()
        mgr._tvm.get_session.return_value = MagicMock()
        mgr.list_messages = MagicMock(return_value=[])

        agent = _make_agent()
        mgr._prepare(agent, TENANT_1, "user-456", "conv-001")  # switching: init -> session
        mgr._prepare(agent, TENANT_1, "user-456", "conv-001")  # same session, no switch

        assert mgr.list_messages.call_count == 1

    def test_prepare_propagates_isolation_error_on_sts_failure(self):
        mgr = _make_manager()
        mgr._tvm.get_session.side_effect = IsolationError("STS failed")
        with pytest.raises(IsolationError):
            mgr._prepare(_make_agent(), TENANT_1, "user-456", "conv-001")
