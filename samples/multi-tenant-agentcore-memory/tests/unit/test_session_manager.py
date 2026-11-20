# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
test_session_manager.py — Unit tests for MultiTenantAgentCoreMemorySessionManager.

External SDKs (strands, bedrock_agentcore) are stubbed in conftest.py, and the
TokenVendingMachine is patched so no STS/boto3 calls are made.
"""

import pytest
from unittest.mock import MagicMock, patch

from strands.agent.state import AgentState
from strands.types.session import SessionAgent

from agentcore_memory import (
    MultiTenantAgentCoreMemorySessionManager,
    AGENT_NAME,
    BOOTSTRAP_TENANT_ID,
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


class _ConversationManager:
    def __init__(self):
        self.removed_message_count = 0

    def get_state(self):
        return {"__name__": "_ConversationManager", "removed_message_count": self.removed_message_count}

    def restore_from_session(self, state):
        self.removed_message_count = state["removed_message_count"]
        return None


def _make_agent(agent_id="agent-1"):
    """Minimal agent stub with the attributes the session manager touches."""
    agent = MagicMock()
    agent.agent_id = agent_id
    agent.messages = []
    agent.state = AgentState()
    agent.conversation_manager = _ConversationManager()
    agent._model_state = {}
    agent.model.stateful = False
    return agent


def _make_ready_manager(agent=None):
    """Session manager that has run start-up initialize() for one agent."""
    mgr = _make_manager()
    mgr._tvm.get_session.return_value = MagicMock()
    agent = agent or _make_agent()
    mgr.initialize(agent)
    return mgr, agent


class TestConstruction:
    def test_missing_memory_id_raises_value_error(self):
        with pytest.raises(ValueError, match="memory_id"):
            MultiTenantAgentCoreMemorySessionManager(memory_id="", tvm_role_arn=TVM_ARN)

    def test_missing_tvm_role_arn_raises_value_error(self, monkeypatch):
        # Ensure the env fallback is absent so the guard fires.
        monkeypatch.delenv("AGENTCORE_TVM_ROLE_ARN", raising=False)
        with pytest.raises(ValueError, match="tvm_role_arn"):
            MultiTenantAgentCoreMemorySessionManager(memory_id=MEMORY_ID, tvm_role_arn=None)

    def test_boto_session_override_is_rejected(self):
        with pytest.raises(ValueError, match="boto_session"):
            MultiTenantAgentCoreMemorySessionManager(
                memory_id=MEMORY_ID, tvm_role_arn=TVM_ARN, boto_session=MagicMock(),
            )

    def test_valid_construction_sets_attributes(self):
        mgr = _make_manager()
        assert mgr.memory_id == MEMORY_ID
        assert mgr.agent_name == AGENT_NAME
        assert mgr.region_name == "us-east-1"

    def test_placeholder_runs_under_bootstrap_tvm_session(self):
        """Start-up memory calls never use the container's own credentials."""
        mgr = _make_manager()
        mgr._tvm.get_bootstrap_session.assert_called_once_with()
        assert mgr.boto_session is mgr._tvm.get_bootstrap_session.return_value
        assert mgr.config.actor_id.startswith(f"{BOOTSTRAP_TENANT_ID}:")
        assert mgr.config.session_id.startswith(f"{BOOTSTRAP_TENANT_ID}-")

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

    @pytest.mark.parametrize("bad", ["acme:corp", "acme/x", "_x", "agentcore-bootstrap", "a" * 58, "", None])
    def test_invalid_tenant_id_raises(self, bad):
        mgr = _make_manager()
        with pytest.raises(IsolationError):
            mgr._build_actor_id({"tenantId": bad}, "user-456")


class TestBuildSessionId:
    def test_session_id_format(self):
        mgr = _make_manager()
        result = mgr._build_session_id(TENANT_1, "user-456", "conv-001")
        assert result == f"tenant-001-{AGENT_NAME}-user-456-conv-001"

    def test_missing_tenant_id_raises(self):
        mgr = _make_manager()
        with pytest.raises(IsolationError, match="tenantId"):
            mgr._build_session_id({}, "user-456", "conv-001")

    @pytest.mark.parametrize("bad", ["conv:1", "conv/1", "conv 1", "-conv", "", None])
    def test_invalid_conversation_id_raises(self, bad):
        mgr = _make_manager()
        with pytest.raises(IsolationError, match="conversation_id"):
            mgr._build_session_id(TENANT_1, "user-456", bad)

    def test_session_id_over_100_characters_raises(self):
        mgr = _make_manager()
        with pytest.raises(IsolationError, match="100"):
            mgr._build_session_id(TENANT_1, "u" * 40, "c" * 60)


class TestPrepare:
    def test_prepare_sets_scoped_identifiers_and_swaps_clients(self):
        mgr, agent = _make_ready_manager()

        scoped_session = MagicMock()
        gmcp = MagicMock()
        gmdp = MagicMock()
        # session.client(...) returns control client first, data client second
        scoped_session.client.side_effect = [gmcp, gmdp]
        mgr._tvm.get_session.return_value = scoped_session

        mgr._prepare(agent, TENANT_1, "user-456", "conv-001")

        assert mgr.config.actor_id   == f"tenant-001:{AGENT_NAME}:user-456"
        assert mgr.config.session_id == f"tenant-001-{AGENT_NAME}-user-456-conv-001"
        assert mgr.session_id        == mgr.config.session_id
        mgr._tvm.get_session.assert_called_once_with(TENANT_1)
        assert mgr.memory_client.gmcp_client is gmcp
        assert mgr.memory_client.gmdp_client is gmdp

    def test_prepare_sets_two_namespace_retrieval_config(self):
        mgr, agent = _make_ready_manager()
        mgr._prepare(agent, TENANT_1, "user-456", "conv-001")

        assert "/{actorId}/preferences" in mgr.config.retrieval_config
        assert "/{actorId}/facts" in mgr.config.retrieval_config

    def test_prepare_uses_scoped_session_per_tenant(self):
        mgr, agent = _make_ready_manager()

        mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        actor_1 = mgr.config.actor_id
        mgr._prepare(agent, TENANT_2, "user-1", "conv-1")
        actor_2 = mgr.config.actor_id

        assert actor_1 != actor_2
        assert actor_1.startswith("tenant-001:")
        assert actor_2.startswith("tenant-002:")

    def test_prepare_rehydrates_stm_on_session_change(self):
        """On session change, agent.messages is restored from list_messages (STM)."""
        mgr, agent = _make_ready_manager()

        msg = MagicMock()
        msg.to_message.return_value = {"role": "user", "content": [{"text": "hi"}]}
        mgr.list_messages = MagicMock(return_value=[msg])

        mgr._prepare(agent, TENANT_1, "user-456", "conv-001")

        mgr.list_messages.assert_called_once_with(
            f"tenant-001-{AGENT_NAME}-user-456-conv-001", agent.agent_id, offset=0,
        )
        assert agent.messages == [{"role": "user", "content": [{"text": "hi"}]}]
        assert mgr._held_records[agent.agent_id] == [msg]

    def test_prepare_skips_rehydrate_when_session_unchanged(self):
        """Same session across two turns: rehydrate only fires on the first (change)."""
        mgr, agent = _make_ready_manager()
        mgr.list_messages = MagicMock(return_value=[])

        mgr._prepare(agent, TENANT_1, "user-456", "conv-001")  # switching: init -> session
        mgr._prepare(agent, TENANT_1, "user-456", "conv-001")  # same session, no switch

        assert mgr.list_messages.call_count == 1

    def test_prepare_propagates_isolation_error_on_sts_failure(self):
        mgr, agent = _make_ready_manager()
        mgr._tvm.get_session.side_effect = IsolationError("STS failed")
        with pytest.raises(IsolationError):
            mgr._prepare(agent, TENANT_1, "user-456", "conv-001")


class TestStateOnTenantSwitch:
    def test_state_from_tenant_a_does_not_reach_tenant_b(self):
        """agent.state, model state and conversation-manager state reset on a switch."""
        mgr, agent = _make_ready_manager()

        mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        agent.state.set("secret", "tenant-001 data")
        agent._model_state = {"response_id": "a-123"}
        agent.conversation_manager.removed_message_count = 7
        mgr._last_synced_internal_state[agent.agent_id] = {"state_version": 1}

        mgr._prepare(agent, TENANT_2, "user-1", "conv-1")

        assert agent.state.get("secret") is None
        assert agent._model_state == {}
        assert agent.conversation_manager.removed_message_count == 0
        assert agent.agent_id not in mgr._last_synced_internal_state

    def test_stored_state_for_the_new_session_is_restored(self):
        mgr, agent = _make_ready_manager()
        stored = SessionAgent(
            agent.agent_id, {"lang": "fr"},
            {"__name__": "_ConversationManager", "removed_message_count": 2},
            {"model_state": {"m": 1}},
        )
        mgr.read_agent = MagicMock(return_value=stored)
        mgr.list_messages = MagicMock(return_value=[])

        mgr._prepare(agent, TENANT_2, "user-1", "conv-9")

        mgr.read_agent.assert_called_once_with(f"tenant-002-{AGENT_NAME}-user-1-conv-9", agent.agent_id)
        assert agent.state.get("lang") == "fr"
        assert agent._model_state == {"m": 1}
        mgr.list_messages.assert_called_once_with(
            f"tenant-002-{AGENT_NAME}-user-1-conv-9", agent.agent_id, offset=2,
        )

    def test_switch_without_initialize_fails_closed(self):
        mgr = _make_manager()
        mgr._tvm.get_session.return_value = MagicMock()
        with pytest.raises(IsolationError):
            mgr._prepare(_make_agent(), TENANT_1, "user-1", "conv-1")

    def test_new_session_records_agent_with_its_own_created_at(self):
        mgr, agent = _make_ready_manager()
        mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        session_id, record = mgr.created_agents[-1]
        assert session_id == f"tenant-001-{AGENT_NAME}-user-1-conv-1"
        assert record.agent_id == agent.agent_id

    def test_stored_session_is_not_recorded_again(self):
        mgr, agent = _make_ready_manager()
        mgr.read_agent = MagicMock(return_value=SessionAgent(
            agent.agent_id, {}, {"__name__": "_ConversationManager", "removed_message_count": 0},
            {"model_state": {}},
        ))
        mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        assert not getattr(mgr, "created_agents", [])

    def test_switch_detected_on_actor_change_with_same_session_id(self):
        """Different actors whose session ids collide still trigger a reset."""
        mgr, agent = _make_ready_manager()
        mgr._prepare(agent, {"tenantId": "t"}, "assistant-x", "c1")
        first_session = mgr.config.session_id
        agent.state.set("secret", "t data")
        mgr._prepare(agent, {"tenantId": "t-assistant"}, "x", "c1")
        assert mgr.config.session_id == first_session
        assert agent.state.get("secret") is None

    def test_stateful_model_gets_no_local_transcript(self):
        mgr, agent = _make_ready_manager()
        agent.model.stateful = True
        agent.messages = [{"role": "user", "content": [{"text": "old"}]}]
        msg = MagicMock()
        msg.to_message.return_value = {"role": "user", "content": [{"text": "hi"}]}
        mgr.list_messages = MagicMock(return_value=[msg])
        mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        assert agent.messages == []


class TestFailedSwitch:
    def test_failed_restore_clears_and_unbinds(self):
        mgr, agent = _make_ready_manager()
        mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        agent.messages = [{"role": "user", "content": [{"text": "tenant-001 secret"}]}]
        agent.state.set("secret", "tenant-001 data")

        mgr.read_agent = MagicMock(side_effect=IsolationError("read failed"))
        with pytest.raises(IsolationError):
            mgr._prepare(agent, TENANT_2, "user-1", "conv-1")

        assert agent.messages == []
        assert agent.state.get("secret") is None
        assert mgr.config.session_id.startswith(f"{BOOTSTRAP_TENANT_ID}-")
        assert mgr.config.actor_id.startswith(f"{BOOTSTRAP_TENANT_ID}:")

        # The next request for tenant-002 is a switch again, not a reuse.
        mgr.read_agent = MagicMock(return_value=None)
        mgr.list_messages = MagicMock(return_value=[])
        mgr._prepare(agent, TENANT_2, "user-1", "conv-1")
        mgr.list_messages.assert_called_once()


def _base_read_agent(mgr_self, session_id, agent_id, **kwargs):
    """Mimic the SDK: query the current actor, then a legacy actor; swallow errors."""
    try:
        events = mgr_self.memory_client.list_events(
            memory_id="m", actor_id=mgr_self.config.actor_id, session_id=session_id,
        )
        if events:
            return events[0]
        mgr_self.memory_client.list_events(
            memory_id="m", actor_id=f"agent_{agent_id}", session_id=session_id,
        )
        return None
    except Exception:
        return None


class TestStoredRecordReads:
    def test_legacy_actor_lookups_are_skipped(self):
        import bedrock_agentcore.memory.integrations.strands.session_manager as sm
        mgr, agent = _make_ready_manager()
        mgr.config.actor_id = "tenant-001:assistant:u"
        mgr.memory_client.list_events = MagicMock(return_value=[])
        original = mgr.memory_client.list_events
        with patch.object(sm.AgentCoreMemorySessionManager, "read_agent", _base_read_agent):
            assert mgr.read_agent("s", agent.agent_id) is None
        actors = [c.kwargs["actor_id"] for c in original.call_args_list]
        assert actors == ["tenant-001:assistant:u"]
        assert mgr.memory_client.list_events is original

    def test_read_error_is_not_treated_as_missing_record(self):
        import bedrock_agentcore.memory.integrations.strands.session_manager as sm
        mgr, agent = _make_ready_manager()
        mgr.config.actor_id = "tenant-001:assistant:u"
        mgr.memory_client.list_events = MagicMock(side_effect=RuntimeError("throttled"))
        with patch.object(sm.AgentCoreMemorySessionManager, "read_agent", _base_read_agent):
            with pytest.raises(IsolationError, match="not resetting"):
                mgr.read_agent("s", agent.agent_id)


class TestHook:
    def _event(self, state):
        event = MagicMock()
        event.invocation_state = state
        return event

    def test_missing_invocation_state_keys_raise_isolation_error(self):
        mgr, _ = _make_ready_manager()
        with pytest.raises(IsolationError, match="tenant_context"):
            mgr._on_before_invocation(self._event({}))

    def test_hook_calls_prepare_with_state(self):
        mgr, agent = _make_ready_manager()
        mgr._prepare = MagicMock()
        event = self._event({"tenant_context": TENANT_1, "user_id": "u", "conversation_id": "c"})
        event.agent = agent
        mgr._on_before_invocation(event)
        mgr._prepare.assert_called_once_with(agent, TENANT_1, "u", "c")


class TestClientConfig:
    def test_swapped_clients_reuse_base_client_config(self):
        mgr, agent = _make_ready_manager()
        scoped = MagicMock()
        mgr._tvm.get_session.return_value = scoped
        mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        for call in scoped.client.call_args_list:
            assert call.kwargs["config"] is mgr._client_config


def _base_list_messages(mgr_self, session_id, agent_id, limit=None, offset=0, **kwargs):
    """Mimic the SDK: list events for the current actor and swallow errors."""
    try:
        return mgr_self.memory_client.list_events(
            memory_id="m", actor_id=mgr_self.config.actor_id, session_id=session_id,
        )
    except Exception:
        return []


class TestRound3:
    def test_history_read_error_fails_the_switch_and_unbinds(self):
        import bedrock_agentcore.memory.integrations.strands.session_manager as sm
        mgr, agent = _make_ready_manager()
        mgr.memory_client.list_events = MagicMock(side_effect=RuntimeError("throttled"))
        with patch.object(sm.AgentCoreMemorySessionManager, "list_messages", _base_list_messages):
            with pytest.raises(IsolationError, match="history"):
                mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        assert agent.messages == []
        assert mgr.config.session_id.startswith(f"{BOOTSTRAP_TENANT_ID}-")

    def test_unparseable_stored_record_is_not_overwritten(self):
        import bedrock_agentcore.memory.integrations.strands.session_manager as sm

        def read_agent_that_cannot_parse(mgr_self, session_id, agent_id, **kwargs):
            mgr_self.memory_client.list_events(
                memory_id="m", actor_id=mgr_self.config.actor_id, session_id=session_id,
            )
            return None  # the SDK swallows the parse error

        mgr, agent = _make_ready_manager()
        mgr.config.actor_id = "tenant-001:assistant:u"
        mgr.memory_client.list_events = MagicMock(return_value=[{"payload": [{"blob": "not json"}]}])
        with patch.object(sm.AgentCoreMemorySessionManager, "read_agent", read_agent_that_cannot_parse):
            with pytest.raises(IsolationError, match="parsed"):
                mgr.read_agent("s", agent.agent_id)

    def test_placeholder_records_are_never_read(self):
        import bedrock_agentcore.memory.integrations.strands.session_manager as sm
        mgr = _make_manager()
        with patch.object(sm.AgentCoreMemorySessionManager, "read_session") as base_session, \
             patch.object(sm.AgentCoreMemorySessionManager, "read_agent") as base_agent:
            assert mgr.read_session(mgr._placeholder_session) is None
            assert mgr.read_agent(mgr._placeholder_session, "agent-1") is None
            base_session.assert_not_called()
            base_agent.assert_not_called()
            # A tenant session still goes to the base implementation.
            base_agent.return_value = None
            mgr.config.actor_id = "tenant-001:assistant:u"
            mgr.read_agent("tenant-001-assistant-u-c", "agent-1")
            base_agent.assert_called_once()

    def test_corrupt_history_event_fails_the_read(self):
        import bedrock_agentcore.memory.integrations.strands.session_manager as sm

        def base_list_messages(mgr_self, session_id, agent_id, limit=None, offset=0, **kwargs):
            try:  # the SDK converts inside a catch-all and returns []
                return mgr_self.converter.events_to_messages([{"payload": []}])
            except Exception:
                return []

        mgr, agent = _make_ready_manager()
        mgr.converter = MagicMock()
        mgr.converter.events_to_messages.side_effect = ValueError("not json")
        with patch.object(sm.AgentCoreMemorySessionManager, "list_messages", base_list_messages):
            with pytest.raises(IsolationError, match="history"):
                mgr._prepare(agent, TENANT_1, "user-1", "conv-1")
        assert agent.messages == []
        assert mgr.converter.events_to_messages.side_effect is not None  # converter restored

    def test_clean_snapshot_is_taken_before_the_base_restore(self):
        import bedrock_agentcore.memory.integrations.strands.session_manager as sm

        def base_initialize_that_restores_old_state(mgr_self, agent, **kwargs):
            agent.state = AgentState({"v": 1})  # what an older deploy stored

        mgr = _make_manager()
        mgr._tvm.get_session.return_value = MagicMock()
        agent = _make_agent()
        agent.state = AgentState({"v": 2})  # what the code builds now
        with patch.object(sm.AgentCoreMemorySessionManager, "initialize", base_initialize_that_restores_old_state):
            mgr.initialize(agent)
        mgr._prepare(agent, TENANT_1, "user-1", "conv-new")
        assert agent.state.get("v") == 2
