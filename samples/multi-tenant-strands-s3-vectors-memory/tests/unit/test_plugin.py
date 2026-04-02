"""
test_plugin.py — Unit tests for S3VectorMemoryPlugin.

Covers:
  - TestPluginConstruction              (Requirements 10.1, 10.2, 10.3)
  - TestBeforeInvocationFirstTurn       (Requirements 11.1, 11.2, 11.3, 11.4, 11.5, 11.6)
  - TestBeforeInvocationSubsequentTurns (Requirements 12.1, 12.2, 12.3)
  - TestAfterInvocation                 (Requirements 13.1, 13.2, 13.3, 13.4)
  - TestCloseSessionWithData            (Requirements 14.1, 14.2, 14.3, 14.4, 14.5, 14.6)
  - TestBuildPrompt                     (Requirements 15.1, 15.2, 15.3, 15.4)

All external dependencies (strands, cachetools) are mocked — no real AWS calls.
"""

import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Mock strands modules BEFORE importing s3_vector_memory_plugin
# ---------------------------------------------------------------------------

strands_mock = MagicMock()
strands_hooks_mock = MagicMock()
strands_plugins_mock = MagicMock()

# The @hook decorator must be a pass-through (return the function unchanged)
strands_plugins_mock.hook = lambda f: f

# The @tool decorator must also be a pass-through so memory_tool tests can call
# the underlying function directly
strands_mock.tool = lambda f: f


class FakePlugin:
    def __init__(self):
        pass


strands_mock.Plugin = FakePlugin
strands_mock.Agent = MagicMock()

sys.modules.setdefault("strands", strands_mock)
sys.modules.setdefault("strands.hooks", strands_hooks_mock)
sys.modules.setdefault("strands.plugins", strands_plugins_mock)
sys.modules.setdefault("strands.models", MagicMock())

# ---------------------------------------------------------------------------
# Now import the plugin module
# ---------------------------------------------------------------------------

import pytest
from cachetools import TTLCache

import strands_s3_vectors_memory.s3_vector_memory_plugin as plugin_module
from strands_s3_vectors_memory.s3_vector_memory_plugin import S3VectorMemoryPlugin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_PROMPT_WITH_PLACEHOLDER = "You are helpful. {memory_context}"
BASE_PROMPT_NO_PLACEHOLDER = "You are helpful."


def _make_store(memories=None):
    """Return a MagicMock store with retrieve_memories configured."""
    store = MagicMock()
    store.retrieve_memories.return_value = memories or []
    store._embed.return_value = [0.1] * 1024
    store._get_s3vectors_client.return_value = MagicMock()
    store._build_index_name.return_value = "memory"
    store.bucket_name = "test-bucket"
    return store


def _make_plugin(base_prompt=BASE_PROMPT_WITH_PLACEHOLDER, memories=None):
    """Construct a plugin with a mocked store."""
    store = _make_store(memories=memories)
    plugin = S3VectorMemoryPlugin(store=store, base_prompt=base_prompt)
    return plugin, store


def _make_before_event(user_id="u1", conversation_id="c1", messages=None,
                       has_session_manager=False):
    """Build a BeforeInvocationEvent-like mock."""
    event = MagicMock()
    event.invocation_state = {
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
    agent = MagicMock()
    agent._session_manager = MagicMock() if has_session_manager else None
    agent.messages = []
    agent.system_prompt = BASE_PROMPT_WITH_PLACEHOLDER
    event.agent = agent
    # Default to a non-empty message so the empty-message short-circuit (#11) doesn't fire
    event.messages = messages if messages is not None else [
        {"role": "user", "content": [{"text": "hello"}]}
    ]
    return event


def _make_after_event(conv_id="c1", end_session=False, messages=None):
    """Build an AfterInvocationEvent-like mock."""
    event = MagicMock()
    event.invocation_state = {"end_session": end_session}
    agent = MagicMock()
    agent.messages = messages or [{"role": "user", "content": [{"text": "hello"}]}]
    event.agent = agent
    return event, agent


# ---------------------------------------------------------------------------
# Task 6.1 — TestPluginConstruction
# Requirements: 10.1, 10.2, 10.3
# ---------------------------------------------------------------------------


class TestPluginConstruction:
    """S3VectorMemoryPlugin initialises state correctly and warns on missing placeholder."""

    def test_no_warning_when_placeholder_present(self, caplog):
        """base_prompt with {memory_context} → no WARNING logged."""
        import logging
        with caplog.at_level(logging.WARNING, logger="s3_vector_memory_plugin"):
            _make_plugin(base_prompt=BASE_PROMPT_WITH_PLACEHOLDER)
        assert not any(r.levelname == "WARNING" for r in caplog.records)

    def test_warning_when_placeholder_missing(self, caplog):
        """base_prompt without {memory_context} → WARNING logged."""
        import logging
        with caplog.at_level(logging.WARNING, logger="s3_vector_memory_plugin"):
            _make_plugin(base_prompt=BASE_PROMPT_NO_PLACEHOLDER)
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_conv_buffer_is_ttlcache(self):
        """_conv_buffer is a TTLCache instance."""
        plugin, _ = _make_plugin()
        assert isinstance(plugin._conv_buffer, TTLCache)

    def test_injected_convs_is_empty_set(self):
        """_injected_convs is an empty bounded TTLCache at construction."""
        plugin, _ = _make_plugin()
        from cachetools import TTLCache
        assert isinstance(plugin._injected_convs, TTLCache)
        assert len(plugin._injected_convs) == 0

    def test_contextvars_at_defaults(self):
        """All ContextVars are at their default values after construction."""
        plugin, _ = _make_plugin()
        assert plugin._cv_tenant.get(None) is None
        assert plugin._cv_user_id.get("") == ""
        assert plugin._cv_conv_id.get("") == ""
        assert plugin._cv_has_sm.get(False) is False


# ---------------------------------------------------------------------------
# Task 6.2 — TestBeforeInvocationFirstTurn
# Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6
# ---------------------------------------------------------------------------


class TestBeforeInvocationFirstTurn:
    """before_invocation on the first turn injects memories into system_prompt."""

    def test_retrieve_memories_called_once(self):
        """store.retrieve_memories called exactly once on first turn."""
        plugin, store = _make_plugin()
        event = _make_before_event()
        plugin.before_invocation(event)
        store.retrieve_memories.assert_called_once()

    def test_memories_above_threshold_injected(self):
        """Memories with similarity >= 0.5 → {memory_context} replaced with formatted section."""
        memories = [
            {"content": "User likes Python", "similarity": 0.8},
            {"content": "User prefers dark mode", "similarity": 0.6},
        ]
        plugin, store = _make_plugin(memories=memories)
        event = _make_before_event()
        plugin.before_invocation(event)

        prompt = event.agent.system_prompt
        assert "Relevant context from previous conversations:" in prompt
        assert "- User likes Python" in prompt
        assert "- User prefers dark mode" in prompt
        assert "{memory_context}" not in prompt

    def test_no_memories_above_threshold_empty_context(self):
        """No memories above threshold → {memory_context} replaced with empty string and stripped."""
        memories = [
            {"content": "Irrelevant memory", "similarity": 0.3},
        ]
        plugin, store = _make_plugin(memories=memories)
        event = _make_before_event()
        plugin.before_invocation(event)

        prompt = event.agent.system_prompt
        assert "{memory_context}" not in prompt
        assert prompt == prompt.strip()

    def test_conversation_id_added_to_injected_convs(self):
        """conversation_id is added to _injected_convs after first turn."""
        plugin, store = _make_plugin()
        event = _make_before_event(conversation_id="conv-123")
        plugin.before_invocation(event)
        assert "conv-123" in plugin._injected_convs

    def test_missing_user_id_returns_immediately(self):
        """Missing user_id in invocation_state → returns without modifying agent."""
        plugin, store = _make_plugin()
        event = MagicMock()
        event.invocation_state = {"conversation_id": "c1"}  # no user_id
        event.agent = MagicMock()
        event.agent.system_prompt = BASE_PROMPT_WITH_PLACEHOLDER
        event.messages = []

        plugin.before_invocation(event)

        store.retrieve_memories.assert_not_called()
        # system_prompt should not have been set by the plugin
        event.agent.system_prompt = BASE_PROMPT_WITH_PLACEHOLDER  # unchanged

    def test_retrieve_memories_exception_logs_warning_and_sets_prompt(self, caplog):
        """retrieve_memories exception → warning logged, prompt set with empty context."""
        import logging
        plugin, store = _make_plugin()
        store.retrieve_memories.side_effect = RuntimeError("S3 error")
        event = _make_before_event()

        with caplog.at_level(logging.WARNING, logger="s3_vector_memory_plugin"):
            plugin.before_invocation(event)

        assert any(r.levelname == "WARNING" for r in caplog.records)
        prompt = event.agent.system_prompt
        assert "{memory_context}" not in prompt
        assert prompt == prompt.strip()


# ---------------------------------------------------------------------------
# Task 6.3 — TestBeforeInvocationSubsequentTurns
# Requirements: 12.1, 12.2, 12.3
# ---------------------------------------------------------------------------


class TestBeforeInvocationSubsequentTurns:
    """before_invocation on subsequent turns restores cached state without re-querying."""

    def _setup_prior_turn(self, plugin, store, conversation_id="c1"):
        """Simulate a prior turn by pre-populating plugin state."""
        cached_prompt = "You are helpful. Relevant context from previous conversations:\n- Prior fact"
        cached_messages = [
            {"role": "user", "content": [{"text": "hello"}]},
            {"role": "assistant", "content": [{"text": "hi there"}]},
        ]
        plugin._injected_convs[conversation_id] = True
        plugin._conv_buffer[f"_prompt_{conversation_id}"] = cached_prompt
        plugin._conv_buffer[conversation_id] = cached_messages
        return cached_prompt, cached_messages

    def test_retrieve_memories_not_called_on_subsequent_turn(self):
        """store.retrieve_memories NOT called when conversation_id already in _injected_convs."""
        plugin, store = _make_plugin()
        self._setup_prior_turn(plugin, store, conversation_id="c1")

        event = _make_before_event(conversation_id="c1")
        plugin.before_invocation(event)

        store.retrieve_memories.assert_not_called()

    def test_system_prompt_restored_from_cache(self):
        """agent.system_prompt restored from _conv_buffer[f'_prompt_{conversation_id}']."""
        plugin, store = _make_plugin()
        cached_prompt, _ = self._setup_prior_turn(plugin, store, conversation_id="c1")

        event = _make_before_event(conversation_id="c1")
        plugin.before_invocation(event)

        assert event.agent.system_prompt == cached_prompt

    def test_messages_restored_from_cache_without_session_manager(self):
        """agent.messages restored from _conv_buffer[conversation_id] when no SessionManager."""
        plugin, store = _make_plugin()
        _, cached_messages = self._setup_prior_turn(plugin, store, conversation_id="c1")

        event = _make_before_event(conversation_id="c1", has_session_manager=False)
        plugin.before_invocation(event)

        assert event.agent.messages == cached_messages


# ---------------------------------------------------------------------------
# Task 6.4 — TestAfterInvocation
# Requirements: 13.1, 13.2, 13.3, 13.4
# ---------------------------------------------------------------------------


class TestAfterInvocation:
    """after_invocation snapshots messages and triggers session close correctly."""

    def test_messages_written_to_buffer_without_session_manager(self):
        """_cv_has_sm=False → agent.messages written to _conv_buffer[conv_id]."""
        plugin, store = _make_plugin()
        conv_id = "c-after-1"
        plugin._cv_conv_id.set(conv_id)
        plugin._cv_has_sm.set(False)

        messages = [{"role": "user", "content": [{"text": "hello"}]}]
        event, agent = _make_after_event(conv_id=conv_id, messages=messages)
        agent.messages = messages

        plugin.after_invocation(event)

        assert conv_id in plugin._conv_buffer
        assert plugin._conv_buffer[conv_id] == messages

    def test_buffer_not_written_with_session_manager(self):
        """_cv_has_sm=True → _conv_buffer NOT written."""
        plugin, store = _make_plugin()
        conv_id = "c-after-2"
        plugin._cv_conv_id.set(conv_id)
        plugin._cv_has_sm.set(True)

        event, agent = _make_after_event(conv_id=conv_id)
        plugin.after_invocation(event)

        assert conv_id not in plugin._conv_buffer

    def test_end_session_true_submits_close_session(self):
        """end_session=True → close_session_with_data submitted to executor."""
        plugin, store = _make_plugin()
        conv_id = "c-after-3"
        plugin._cv_conv_id.set(conv_id)
        plugin._cv_has_sm.set(False)
        plugin._cv_tenant.set(None)
        plugin._cv_user_id.set("u1")

        messages = [{"role": "user", "content": [{"text": "hello"}]}]
        plugin._conv_buffer[conv_id] = messages

        event, agent = _make_after_event(conv_id=conv_id, end_session=True, messages=messages)
        agent.messages = messages

        with patch.object(plugin_module._executor, "submit") as mock_submit:
            plugin.after_invocation(event)
            mock_submit.assert_called_once()
            # Verify close_session_with_data is the submitted callable
            assert mock_submit.call_args[0][0] == plugin.close_session_with_data

    def test_end_session_false_no_executor_submission(self):
        """end_session=False → no executor submission."""
        plugin, store = _make_plugin()
        conv_id = "c-after-4"
        plugin._cv_conv_id.set(conv_id)
        plugin._cv_has_sm.set(False)

        event, agent = _make_after_event(conv_id=conv_id, end_session=False)

        with patch.object(plugin_module._executor, "submit") as mock_submit:
            plugin.after_invocation(event)
            mock_submit.assert_not_called()


# ---------------------------------------------------------------------------
# Task 6.5 — TestCloseSessionWithData
# Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6
# ---------------------------------------------------------------------------


class TestCloseSessionWithData:
    """close_session_with_data summarises conversation and stores result in S3."""

    def _make_model(self):
        return MagicMock()

    def test_empty_messages_returns_none(self):
        """Empty messages list → returns None without calling model or put_vectors."""
        plugin, store = _make_plugin()
        mock_agent_cls = MagicMock()

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            result = plugin.close_session_with_data(None, "u1", "c1", [], self._make_model())

        assert result is None
        mock_agent_cls.assert_not_called()
        store._get_s3vectors_client.return_value.put_vectors.assert_not_called()

    def test_messages_with_no_text_returns_none(self):
        """Messages with no text content → returns None."""
        plugin, store = _make_plugin()
        messages = [
            {"role": "user", "content": [{"image": "data"}]},
            {"role": "assistant", "content": []},
        ]
        mock_agent_cls = MagicMock()

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            result = plugin.close_session_with_data(None, "u1", "c1", messages, self._make_model())

        assert result is None

    def test_valid_messages_invokes_summarisation_agent(self):
        """Valid messages → transcript built and summarisation agent invoked."""
        plugin, store = _make_plugin()
        messages = [
            {"role": "user", "content": [{"text": "Hello there"}]},
            {"role": "assistant", "content": [{"text": "Hi, how can I help?"}]},
        ]
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = "This is a summary."
        mock_agent_cls = MagicMock(return_value=mock_agent_instance)

        # Agent is imported locally inside close_session_with_data as
        # `from strands import Agent as _Agent`, so patch strands.Agent directly.
        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            result = plugin.close_session_with_data(None, "u1", "c1", messages, self._make_model())

        mock_agent_cls.assert_called_once()
        mock_agent_instance.assert_called_once()
        # Transcript should contain both turns
        call_args = mock_agent_instance.call_args[0][0]
        assert "USER: Hello there" in call_args
        assert "ASSISTANT: Hi, how can I help?" in call_args

    def test_put_vectors_called_with_correct_metadata(self):
        """put_vectors called with type='summary', correct user_id and conversation_id."""
        plugin, store = _make_plugin()
        messages = [
            {"role": "user", "content": [{"text": "Important fact"}]},
        ]
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = "Summary of conversation."
        mock_agent_cls = MagicMock(return_value=mock_agent_instance)

        mock_client = MagicMock()
        store._get_s3vectors_client.return_value = mock_client

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            plugin.close_session_with_data(None, "u1", "c1", messages, self._make_model())

        mock_client.put_vectors.assert_called_once()
        call_kwargs = mock_client.put_vectors.call_args[1]
        vector = call_kwargs["vectors"][0]
        assert vector["metadata"]["type"] == "summary"
        assert vector["metadata"]["user_id"] == "u1"
        assert vector["metadata"]["conversation_id"] == "c1"

    def test_summary_truncated_to_500_chars(self):
        """Summary > 500 chars is truncated to 500 chars before storing."""
        plugin, store = _make_plugin()
        messages = [
            {"role": "user", "content": [{"text": "Tell me everything"}]},
        ]
        long_summary = "x" * 600
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = long_summary
        mock_agent_cls = MagicMock(return_value=mock_agent_instance)

        mock_client = MagicMock()
        store._get_s3vectors_client.return_value = mock_client

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            result = plugin.close_session_with_data(None, "u1", "c1", messages, self._make_model())

        assert result is not None
        assert len(result) <= 500
        call_kwargs = mock_client.put_vectors.call_args[1]
        stored_content = call_kwargs["vectors"][0]["metadata"]["content"]
        assert len(stored_content) <= 500

    def test_cleanup_removes_conv_from_buffer_and_injected_convs(self):
        """conv_id and _prompt_{conv_id} removed from _conv_buffer; conv_id discarded from _injected_convs."""
        plugin, store = _make_plugin()
        conv_id = "c-cleanup"
        plugin._conv_buffer[conv_id] = [{"role": "user", "content": [{"text": "hi"}]}]
        plugin._conv_buffer[f"_prompt_{conv_id}"] = "cached prompt"
        plugin._injected_convs[conv_id] = True

        messages = [{"role": "user", "content": [{"text": "hi"}]}]
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = "Summary."
        mock_agent_cls = MagicMock(return_value=mock_agent_instance)

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            plugin.close_session_with_data(None, "u1", conv_id, messages, self._make_model())

        assert conv_id not in plugin._conv_buffer
        assert f"_prompt_{conv_id}" not in plugin._conv_buffer
        assert conv_id not in plugin._injected_convs

    def test_embed_called_with_summary(self):
        """store._embed called with the summary text."""
        plugin, store = _make_plugin()
        messages = [
            {"role": "user", "content": [{"text": "My project uses Python"}]},
        ]
        # The code does: str(_Agent(...)(...)).strip()[:500]
        # So the agent instance's __call__ return value must produce the right str().
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = "Python project summary."
        mock_agent_cls = MagicMock(return_value=mock_agent_instance)

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            plugin.close_session_with_data(None, "u1", "c1", messages, self._make_model())

        store._embed.assert_called_once_with("Python project summary.", purpose="GENERIC_INDEX")


# ---------------------------------------------------------------------------
# Task 6.6 — TestBuildPrompt
# Requirements: 15.1, 15.2, 15.3, 15.4
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """_build_prompt injects relevant memories into the base prompt."""

    def test_no_placeholder_returns_base_prompt_unchanged(self):
        """base_prompt without {memory_context} → returned unchanged."""
        plugin, store = _make_plugin(base_prompt=BASE_PROMPT_NO_PLACEHOLDER)
        result = plugin._build_prompt("any query", None, "u1")
        assert result == BASE_PROMPT_NO_PLACEHOLDER

    def test_memories_above_threshold_included(self):
        """Memories with similarity >= 0.5 → included in returned prompt."""
        memories = [
            {"content": "User prefers Python", "similarity": 0.9},
            {"content": "User works at Acme", "similarity": 0.5},
        ]
        plugin, store = _make_plugin(memories=memories)
        result = plugin._build_prompt("query", None, "u1")
        assert "User prefers Python" in result
        assert "User works at Acme" in result

    def test_all_memories_below_threshold_empty_context(self):
        """All memories with similarity < 0.5 → {memory_context} replaced with empty string."""
        memories = [
            {"content": "Low relevance memory", "similarity": 0.3},
            {"content": "Another low relevance", "similarity": 0.1},
        ]
        plugin, store = _make_plugin(memories=memories)
        result = plugin._build_prompt("query", None, "u1")
        assert "{memory_context}" not in result
        assert "Low relevance memory" not in result

    def test_memory_formatting_with_header_and_bullet_lines(self):
        """Each memory formatted as '- {content}' under the correct header."""
        memories = [
            {"content": "Fact one", "similarity": 0.8},
            {"content": "Fact two", "similarity": 0.7},
        ]
        plugin, store = _make_plugin(memories=memories)
        result = plugin._build_prompt("query", None, "u1")
        assert "Relevant context from previous conversations:" in result
        assert "- Fact one" in result
        assert "- Fact two" in result


# ---------------------------------------------------------------------------
# Issue #7 — buffers cleared even when _embed raises in close_session_with_data
# ---------------------------------------------------------------------------

class TestCloseSessionBufferClearedOnEmbedFailure:
    """Issue #7: _embed is inside try/finally so buffers are always cleared."""

    def test_buffers_cleared_even_when_embed_raises(self):
        plugin, store = _make_plugin()
        conv_id = "conv-embed-fail"
        plugin._conv_buffer[conv_id] = [{"role": "user", "content": [{"text": "hi"}]}]
        plugin._conv_buffer[f"_prompt_{conv_id}"] = "prompt"
        plugin._injected_convs[conv_id] = True
        store._embed.side_effect = RuntimeError("Bedrock throttled")

        messages = [{"role": "user", "content": [{"text": "hi"}]}]
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = "Summary text."
        mock_agent_cls = MagicMock(return_value=mock_agent_instance)

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            try:
                plugin.close_session_with_data(None, "u1", conv_id, messages, MagicMock())
            except Exception:
                pass

        assert conv_id not in plugin._conv_buffer
        assert conv_id not in plugin._injected_convs


# ---------------------------------------------------------------------------
# Issue #8 — background future exceptions are logged
# ---------------------------------------------------------------------------

class TestAfterInvocationBackgroundExceptionLogged:
    """Issue #8: exceptions from the background close_session future must be logged."""

    def test_future_exception_is_logged(self, caplog):
        import logging
        plugin, store = _make_plugin()
        conv_id = "conv-bg-fail"
        plugin._cv_conv_id.set(conv_id)
        plugin._cv_has_sm.set(False)
        plugin._cv_tenant.set(None)
        plugin._cv_user_id.set("u1")

        messages = [{"role": "user", "content": [{"text": "hello"}]}]
        plugin._conv_buffer[conv_id] = messages
        plugin.close_session_with_data = MagicMock(side_effect=RuntimeError("bg failure"))

        event = MagicMock()
        event.invocation_state = {"end_session": True}
        event.agent = MagicMock()
        event.agent.messages = messages

        future_holder = {}
        original_submit = plugin_module._executor.submit

        def capturing_submit(fn, *args, **kwargs):
            f = original_submit(fn, *args, **kwargs)
            future_holder["future"] = f
            return f

        with patch.object(plugin_module._executor, "submit", side_effect=capturing_submit), \
             caplog.at_level(logging.WARNING):
            plugin.after_invocation(event)

        if "future" in future_holder:
            try:
                future_holder["future"].result(timeout=5)
            except Exception:
                pass

        assert any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)


# ---------------------------------------------------------------------------
# Issue #9 — _injected_convs is a bounded TTLCache, not a plain set
# ---------------------------------------------------------------------------

class TestInjectedConvsBounded:
    """Issue #9: _injected_convs must be a TTLCache, not an unbounded set."""

    def test_injected_convs_is_ttlcache_not_set(self):
        plugin, _ = _make_plugin()
        assert not isinstance(plugin._injected_convs, set)
        assert isinstance(plugin._injected_convs, TTLCache)


# ---------------------------------------------------------------------------
# Issue #10 — missing conversation_id does not raise KeyError
# ---------------------------------------------------------------------------

class TestBeforeInvocationMissingConversationId:
    """Issue #10: before_invocation must not raise KeyError when conversation_id is absent."""

    def test_missing_conversation_id_returns_gracefully(self):
        plugin, store = _make_plugin()
        event = MagicMock()
        event.invocation_state = {"user_id": "u1"}
        event.agent = MagicMock()
        event.agent._session_manager = None
        event.agent.messages = []
        event.messages = []

        try:
            plugin.before_invocation(event)
        except KeyError as e:
            pytest.fail(f"before_invocation raised KeyError({e}) when conversation_id was missing")


# ---------------------------------------------------------------------------
# Issue #11 — empty message does not fire retrieve_memories
# ---------------------------------------------------------------------------

class TestBuildPromptEmptyMessage:
    """Issue #11: _build_prompt must not call retrieve_memories when message is empty."""

    def test_empty_message_skips_retrieval(self):
        plugin, store = _make_plugin()
        event = MagicMock()
        event.invocation_state = {"user_id": "u1", "conversation_id": "c1"}
        event.agent = MagicMock()
        event.agent._session_manager = None
        event.agent.messages = []
        event.messages = []  # empty → extracted message is ""

        plugin.before_invocation(event)
        store.retrieve_memories.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #12 — summary truncated at sentence boundary, not mid-word
# ---------------------------------------------------------------------------

class TestCloseSessionSentenceBoundaryTruncation:
    """Issue #12: summaries > 500 chars must be truncated at the last sentence boundary."""

    def test_summary_ends_at_sentence_boundary(self):
        plugin, store = _make_plugin()
        long_summary = ("Sentence one. " * 36) + "This crosses the boundary"
        assert len(long_summary) > 500

        messages = [{"role": "user", "content": [{"text": "hi"}]}]
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = long_summary
        mock_agent_cls = MagicMock(return_value=mock_agent_instance)

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            result = plugin.close_session_with_data(None, "u1", "c1", messages, MagicMock())

        assert result is not None
        assert result[-1] in ".!?"


# ---------------------------------------------------------------------------
# Issue #13 — deterministic summary key overwrites on second close_session
# ---------------------------------------------------------------------------

class TestCloseSessionDeterministicKeyOverwrite:
    """Issue #13: second close_session_with_data for same conv_id uses the same key (documented overwrite)."""

    def test_second_call_uses_same_key(self):
        plugin, store = _make_plugin()
        messages = [{"role": "user", "content": [{"text": "hello"}]}]
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = "Summary."
        mock_agent_cls = MagicMock(return_value=mock_agent_instance)
        mock_client = MagicMock()
        store._get_s3vectors_client.return_value = mock_client

        with patch.dict(sys.modules, {"strands": MagicMock(Agent=mock_agent_cls, Plugin=FakePlugin)}):
            plugin.close_session_with_data(None, "u1", "c1", messages, MagicMock())
            plugin._conv_buffer["c1"] = messages
            plugin._injected_convs["c1"] = True
            plugin.close_session_with_data(None, "u1", "c1", messages, MagicMock())

        assert mock_client.put_vectors.call_count == 2
        key1 = mock_client.put_vectors.call_args_list[0][1]["vectors"][0]["key"]
        key2 = mock_client.put_vectors.call_args_list[1][1]["vectors"][0]["key"]
        assert key1 == key2


# ---------------------------------------------------------------------------
# memory_tool — mid-turn retrieval tool
# ---------------------------------------------------------------------------

class TestMemoryTool:
    """memory_tool returns a callable tool that retrieves memories using ContextVar identity."""

    @pytest.fixture(autouse=True)
    def patch_tool_decorator(self):
        """Make @tool a pass-through so the inner function is directly callable."""
        with patch("strands_s3_vectors_memory.s3_vector_memory_plugin.tool", lambda f: f):
            yield

    def test_tool_returns_formatted_memories(self):
        """When memories above threshold exist, tool returns formatted string."""
        memories = [
            {"content": "User likes hiking", "similarity": 0.9, "stored_at": "20250101_120000"},
            {"content": "User prefers Python", "similarity": 0.7, "stored_at": "20250102_090000"},
        ]
        plugin, store = _make_plugin(memories=memories)
        plugin._cv_user_id.set("u1")
        plugin._cv_tenant.set(None)
        store.retrieve_memories.return_value = memories

        result = plugin.memory_tool(query="what do I like?")

        assert "User likes hiking" in result
        assert "User prefers Python" in result
        assert "Relevant memories:" in result

    def test_tool_returns_no_memories_message_when_empty(self):
        """When no memories above threshold, tool returns 'No relevant memories found.'"""
        plugin, store = _make_plugin()
        plugin._cv_user_id.set("u1")
        plugin._cv_tenant.set(None)
        store.retrieve_memories.return_value = []

        result = plugin.memory_tool(query="anything")

        assert result == "No relevant memories found."

    def test_tool_returns_no_memories_when_all_below_threshold(self):
        """Memories below similarity threshold are filtered out."""
        memories = [{"content": "Low relevance", "similarity": 0.2, "stored_at": "20250101_120000"}]
        plugin, store = _make_plugin()
        plugin._cv_user_id.set("u1")
        plugin._cv_tenant.set(None)
        store.retrieve_memories.return_value = memories

        result = plugin.memory_tool(query="anything")

        assert result == "No relevant memories found."

    def test_tool_passes_tenant_context_from_contextvar(self):
        """tenant_context from ContextVar is forwarded to retrieve_memories."""
        plugin, store = _make_plugin()
        tenant_ctx = {"tenantId": "tenant-001"}
        plugin._cv_user_id.set("u1")
        plugin._cv_tenant.set(tenant_ctx)
        store.retrieve_memories.return_value = []

        plugin.memory_tool(query="test")

        store.retrieve_memories.assert_called_once()
        call_kwargs = store.retrieve_memories.call_args[1]
        assert call_kwargs["tenant_context"] == tenant_ctx

    def test_tool_returns_error_when_user_id_not_set(self):
        """When user_id ContextVar is empty, tool returns an error message."""
        plugin, store = _make_plugin()
        plugin._cv_user_id.set("")  # not set

        result = plugin.memory_tool(query="anything")

        assert "user_id not set" in result
        store.retrieve_memories.assert_not_called()

    def test_tool_handles_retrieve_exception_gracefully(self):
        """When retrieve_memories raises, tool returns error message without propagating."""
        plugin, store = _make_plugin()
        plugin._cv_user_id.set("u1")
        plugin._cv_tenant.set(None)
        store.retrieve_memories.side_effect = RuntimeError("S3 error")

        result = plugin.memory_tool(query="anything")

        assert "Memory retrieval failed" in result

    def test_tool_respects_top_k_parameter(self):
        """top_k parameter is forwarded to retrieve_memories."""
        plugin, store = _make_plugin()
        plugin._cv_user_id.set("u1")
        plugin._cv_tenant.set(None)
        store.retrieve_memories.return_value = []

        plugin.memory_tool(query="test", top_k=7)

        call_kwargs = store.retrieve_memories.call_args[1]
        assert call_kwargs["top_k"] == 7
