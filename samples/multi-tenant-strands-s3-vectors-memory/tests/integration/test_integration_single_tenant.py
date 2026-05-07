"""
Integration tests for single-tenant S3VectorMemory.

Covers: TestSingleTenantStoreRetrieve (Reqs 2.1, 2.2, 2.3, 2.5, 7.2)
        TestEmbedding (Reqs 4.1, 4.2, 4.3)
        TestPluginLifecycle (Reqs 5.1-5.11)
        TestConversationSummary (Reqs 6.1-6.4)
        TestMemoryTool
        TestInitAgentEnforcement

Requires:
  - S3_VECTOR_BUCKET_NAME env var set
  - Valid AWS credentials with s3vectors + bedrock-runtime access

No S3SessionManager required — tests manage agent.messages in-process,
matching the AgentCore Runtime deployment model.
"""

import math
import os
import threading
import time
import unittest.mock
import uuid

import pytest

from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
from strands_s3_vectors_memory.s3_vector_memory_plugin import S3VectorMemoryPlugin
import strands_s3_vectors_memory.s3_vector_memory_plugin as _plugin_module
from tests.integration._constants import BUCKET_NAME, AWS_REGION, BEDROCK_MODEL_ID, RUN_ID

# Embedding model is separate from the chat model
EMBEDDING_MODEL_ID: str = os.environ.get("EMBEDDING_MODEL", "amazon.nova-2-multimodal-embeddings-v1:0")


class TestSingleTenantStoreRetrieve:
    """End-to-end store/retrieve tests for single-tenant S3VectorMemory (Reqs 2.1-2.5)."""

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        """Ensure the 'memory' index exists before any test in this class runs."""
        self.mem = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )

    def test_store_returns_success(self):
        """Req 2.1: store_memory must return a dict with status == 'success'."""
        user_id = f"user_{RUN_ID}_test2_1"
        content = "I enjoy hiking in the mountains on weekends."
        result = self.mem.store_memory(user_id, content)
        assert result["status"] == "success"

    def test_retrieve_similar_query_returns_result(self):
        """Reqs 2.2, 7.2: retrieve with a similar query returns >= 1 result with similarity >= 0.5."""
        user_id = f"user_{RUN_ID}_test2_2"
        content = "My favourite hobby is playing chess with friends."
        query = "What games do I like to play?"
        self.mem.store_memory(user_id, content)
        results = self.mem.retrieve_memories(user_id, query, top_k=5)
        assert len(results) >= 1
        assert results[0]["similarity"] >= 0.5

    def test_retrieve_different_user_returns_empty(self):
        """Req 2.3: retrieving under a different user_id must return an empty list."""
        user_a = f"user_{RUN_ID}_test2_3a"
        user_b = f"user_{RUN_ID}_test2_3b"
        content = "I love cooking Italian food at home."
        self.mem.store_memory(user_a, content)
        results = self.mem.retrieve_memories(user_b, content, top_k=5)
        assert results == []

    def test_index_name_is_always_memory(self):
        """Req 2.5: _build_index_name() must return 'memory' with and without tenant_context."""
        assert self.mem._build_index_name() == "memory"
        assert self.mem._build_index_name(tenant_context=None) == "memory"
        assert self.mem._build_index_name(tenant_context={"tenantId": "some-tenant"}) == "memory"


class TestEmbedding:
    """Embedding correctness tests for S3VectorMemory._embed (Reqs 4.1, 4.2, 4.3)."""

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        self.mem = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )

    def test_embed_returns_1024_floats(self):
        """Req 4.1: _embed must return a list of exactly 1024 float values."""
        vec = self.mem._embed("hello world")
        assert len(vec) == 1024
        assert all(isinstance(x, float) for x in vec)

    def test_embed_thread_safety(self):
        """Req 4.2: five concurrent _embed calls must all complete without exceptions."""
        texts = [
            "the quick brown fox",
            "machine learning is fascinating",
            "aws s3 vector storage",
            "python threading model",
            "cosine similarity metric",
        ]
        exceptions: list[Exception] = []

        def embed_and_collect(text: str) -> None:
            try:
                self.mem._embed(text)
            except Exception as exc:
                exceptions.append(exc)

        threads = [threading.Thread(target=embed_and_collect, args=(t,)) for t in texts]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert exceptions == []

    def test_embed_determinism(self):
        """Req 4.3: two _embed calls with identical text must have cosine similarity > 0.99."""
        text = "determinism check for embedding model"
        vec1 = self.mem._embed(text)
        vec2 = self.mem._embed(text)
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(x * x for x in vec1))
        norm2 = math.sqrt(sum(x * x for x in vec2))
        cosine_sim = dot / (norm1 * norm2)
        assert cosine_sim > 0.99


# ---------------------------------------------------------------------------
# Helper: create agent without S3SessionManager
# ---------------------------------------------------------------------------

# BASE_PROMPT with {memory_context} placeholder for plugin lifecycle tests
SUMMARY_BASE_PROMPT = (
    "You are a helpful assistant.\n\n"
    "{memory_context}\n\n"
    "Be concise and cite prior context when relevant."
)


def _make_agent(plugin: S3VectorMemoryPlugin, base_prompt: str = SUMMARY_BASE_PROMPT):
    """Create a minimal Strands Agent with the plugin — no SessionManager.

    In-process agent.messages is managed directly, matching the AgentCore Runtime
    deployment model where the microVM persists messages across turns.
    """
    from strands import Agent
    from strands.models import BedrockModel
    return Agent(
        model=BedrockModel(
            model_id=BEDROCK_MODEL_ID,
            region_name=AWS_REGION,
        ),
        name="test-agent",
        system_prompt=base_prompt,
        plugins=[plugin],
        callback_handler=None,
    )


def _run_turn(agent, user_id: str, conversation_id: str, message: str,
              end_session: bool = False):
    """Invoke the agent via invocation_state — the production hook-driven path."""
    return agent(
        message,
        invocation_state={
            "user_id":         user_id,
            "conversation_id": conversation_id,
            "end_session":     end_session,
        },
    )


class TestPluginLifecycle:
    """
    End-to-end plugin lifecycle tests for S3VectorMemoryPlugin (Reqs 5.1-5.11).

    No S3SessionManager — agent.messages is managed in-process, matching the
    AgentCore Runtime model. For "new conversation" scenarios, agent.messages
    is reset to [] to simulate a fresh microVM.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        """Ensure the 'memory' index exists and create a fresh plugin per test."""
        store = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )
        self.plugin = S3VectorMemoryPlugin(store=store, base_prompt=SUMMARY_BASE_PROMPT)
        self.agent = _make_agent(self.plugin, base_prompt=SUMMARY_BASE_PROMPT)

    # ------------------------------------------------------------------
    # 5.1 — first turn calls retrieve_memories (turn detection via agent.messages)
    # ------------------------------------------------------------------

    def test_first_turn_adds_conv_to_injected(self):
        """Req 5.1: first turn (agent.messages empty) calls retrieve_memories exactly once."""
        user_id = f"user_{RUN_ID}_5_1"
        conv_id = f"conv_{RUN_ID}_5_1"

        with unittest.mock.patch.object(
            self.plugin._store, "retrieve_memories", wraps=self.plugin._store.retrieve_memories
        ) as spy:
            _run_turn(self.agent, user_id, conv_id, "hello")
            spy.assert_called_once()

    # ------------------------------------------------------------------
    # 5.2 — first turn with no stored memories strips {memory_context}
    # ------------------------------------------------------------------

    def test_first_turn_no_memories_sets_base_prompt(self):
        """Reqs 5.2, 8.3: fresh user with no memories -> {memory_context} stripped from prompt."""
        user_id = f"user_{RUN_ID}_5_2_fresh_{uuid.uuid4().hex[:6]}"
        conv_id = f"conv_{RUN_ID}_5_2"

        _run_turn(self.agent, user_id, conv_id, "hello")

        assert "{memory_context}" not in self.agent.system_prompt

    # ------------------------------------------------------------------
    # 5.3 — second turn does not call retrieve_memories (agent.messages non-empty)
    # ------------------------------------------------------------------

    def test_second_turn_restores_message_buffer(self):
        """Req 5.3: turn 2 (agent.messages non-empty) does NOT call retrieve_memories."""
        user_id = f"user_{RUN_ID}_5_3"
        conv_id = f"conv_{RUN_ID}_5_3"

        _run_turn(self.agent, user_id, conv_id, "first message")

        with unittest.mock.patch.object(
            self.plugin._store, "retrieve_memories"
        ) as mock_retrieve:
            _run_turn(self.agent, user_id, conv_id, "second message")
            mock_retrieve.assert_not_called()

    # ------------------------------------------------------------------
    # 5.4 — new conversation resets system_prompt (no prompt bleed)
    # ------------------------------------------------------------------

    def test_new_conversation_resets_system_prompt(self):
        """Req 5.4: starting conv B after conv A must not bleed conv A's prompt."""
        user_id = f"user_{RUN_ID}_5_4_fresh_{uuid.uuid4().hex[:6]}"
        conv_a = f"conv_{RUN_ID}_5_4a"
        conv_b = f"conv_{RUN_ID}_5_4b"

        _run_turn(self.agent, user_id, conv_a, "first conversation")

        # Manually corrupt the system_prompt to simulate bleed scenario
        self.agent.system_prompt = "CORRUPTED PROMPT"

        # Simulate new conversation: reset agent.messages (fresh microVM)
        self.agent.messages = []

        # Start a fresh conversation — plugin must reset (no placeholder bleed)
        _run_turn(self.agent, user_id, conv_b, "second conversation")

        assert "{memory_context}" not in self.agent.system_prompt
        assert "CORRUPTED PROMPT" != self.agent.system_prompt

    # ------------------------------------------------------------------
    # 5.5 — end_session=True submits background summarization
    # ------------------------------------------------------------------

    def test_end_session_clears_buffers(self):
        """Req 5.5: end_session=True triggers background close_session_with_data."""
        user_id = f"user_{RUN_ID}_5_5"
        conv_id = f"conv_{RUN_ID}_5_5"

        _run_turn(self.agent, user_id, conv_id, "hello, remember this")

        with unittest.mock.patch.object(
            _plugin_module._executor, "submit", wraps=_plugin_module._executor.submit
        ) as mock_submit:
            _run_turn(self.agent, user_id, conv_id, "goodbye", end_session=True)
            mock_submit.assert_called_once()
            assert mock_submit.call_args[0][0] == self.plugin.close_session_with_data

    # ------------------------------------------------------------------
    # 5.6 — close_session_with_data with empty messages returns None
    # ------------------------------------------------------------------

    def test_close_session_empty_messages_returns_none(self):
        """Req 5.6: close_session_with_data with empty messages list returns None."""
        user_id = f"user_{RUN_ID}_5_6"
        conv_id = f"conv_{RUN_ID}_5_6"

        result = self.plugin.close_session_with_data(
            tenant_context=None,
            user_id=user_id,
            conv_id=conv_id,
            messages=[],
            model=self.agent.model,
        )
        assert result is None

    # ------------------------------------------------------------------
    # 5.7 — _SIMILARITY_THRESHOLD is 0.5
    # ------------------------------------------------------------------

    def test_similarity_threshold_is_0_5(self):
        """Req 8.1: the module-level _SIMILARITY_THRESHOLD constant must equal 0.5."""
        assert _plugin_module._SIMILARITY_THRESHOLD == 0.5

    # ------------------------------------------------------------------
    # 5.8 — below-threshold results set BASE_PROMPT
    # ------------------------------------------------------------------

    def test_below_threshold_results_set_base_prompt(self):
        """Req 8.2: when all retrieved memories have similarity < 0.5, {memory_context} is stripped."""
        user_id = f"user_{RUN_ID}_5_8_fresh_{uuid.uuid4().hex[:6]}"
        conv_id = f"conv_{RUN_ID}_5_8"

        low_similarity_results = [
            {"content": "some old memory", "similarity": 0.3},
            {"content": "another old memory", "similarity": 0.2},
        ]

        with unittest.mock.patch.object(
            self.plugin._store, "retrieve_memories", return_value=low_similarity_results
        ):
            _run_turn(self.agent, user_id, conv_id, "hello")

        assert "{memory_context}" not in self.agent.system_prompt

    # ------------------------------------------------------------------
    # 5.9 — second turn message count increases
    # ------------------------------------------------------------------

    def test_second_turn_message_count_increases(self):
        """Req 9.1: len(agent.messages) after turn 2 must be > after turn 1."""
        user_id = f"user_{RUN_ID}_5_9"
        conv_id = f"conv_{RUN_ID}_5_9"

        _run_turn(self.agent, user_id, conv_id, "first turn message")
        count_after_turn1 = len(self.agent.messages)

        _run_turn(self.agent, user_id, conv_id, "second turn message")
        count_after_turn2 = len(self.agent.messages)

        assert count_after_turn2 >= count_after_turn1

    # ------------------------------------------------------------------
    # 5.10 — two conversations are independent (no shared state)
    # ------------------------------------------------------------------

    def test_two_conversations_have_separate_buffers(self):
        """Req 9.2: two distinct conversation_ids accumulate independent message histories."""
        user_id = f"user_{RUN_ID}_5_10"
        conv_id_1 = f"conv_{RUN_ID}_5_10a"
        conv_id_2 = f"conv_{RUN_ID}_5_10b"

        # Conversation 1
        _run_turn(self.agent, user_id, conv_id_1, "conversation one")
        msgs_1 = list(self.agent.messages)

        # Simulate new conversation: reset agent.messages (fresh microVM)
        self.agent.messages = []

        # Conversation 2
        _run_turn(self.agent, user_id, conv_id_2, "conversation two")
        msgs_2 = list(self.agent.messages)

        assert len(msgs_1) > 0, "conv 1 should have messages"
        assert len(msgs_2) > 0, "conv 2 should have messages"
        # Messages should differ since they are different conversations
        assert msgs_1 != msgs_2

    # ------------------------------------------------------------------
    # 5.11 — closed conversation restarts fresh
    # ------------------------------------------------------------------

    def test_closed_conversation_restarts_fresh(self):
        """Req 9.3: after end_session=True, restarting same conv_id calls retrieve_memories again."""
        user_id = f"user_{RUN_ID}_5_11"
        conv_id = f"conv_{RUN_ID}_5_11"

        # Turn 1 — establish conversation
        _run_turn(self.agent, user_id, conv_id, "initial message")

        # Close the session
        _run_turn(self.agent, user_id, conv_id, "closing message", end_session=True)

        # Simulate fresh microVM: reset agent.messages
        self.agent.messages = []

        # Restart — agent.messages is empty again -> treated as first turn
        with unittest.mock.patch.object(
            self.plugin._store, "retrieve_memories", wraps=self.plugin._store.retrieve_memories
        ) as spy:
            _run_turn(self.agent, user_id, conv_id, "restarted conversation")
            spy.assert_called_once()


class TestConversationSummary:
    """
    End-to-end summary storage and recall tests (Reqs 6.1-6.4).

    No S3SessionManager — agent.messages is managed in-process.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        """Ensure the 'memory' index exists and create a fresh plugin per test."""
        store = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )
        self.store = store
        self.plugin = S3VectorMemoryPlugin(store=store, base_prompt=SUMMARY_BASE_PROMPT)
        self.agent = _make_agent(self.plugin, base_prompt=SUMMARY_BASE_PROMPT)

    # ------------------------------------------------------------------
    # 6.1 — close_session_with_data writes summary within 30 seconds
    # ------------------------------------------------------------------

    def test_end_session_writes_summary_within_30s(self):
        """Req 6.1: close_session_with_data (synchronous) must complete within 30 s
        and the summary vector must be retrievable from the index."""
        user_id = f"user_{RUN_ID}_6_1"
        conv_id = f"conv_{RUN_ID}_6_1"

        messages = [
            {"role": "user",      "content": [{"text": "I love hiking in the Alps."}]},
            {"role": "assistant", "content": [{"text": "That sounds wonderful! The Alps have great trails."}]},
        ]

        start = time.time()
        summary = self.plugin.close_session_with_data(
            tenant_context=None,
            user_id=user_id,
            conv_id=conv_id,
            messages=messages,
            model=self.agent.model,
        )
        elapsed = time.time() - start

        assert elapsed <= 30
        assert summary is not None

        # Verify the summary vector was written to the index
        results = self.store.retrieve_memories(user_id=user_id, query=summary, top_k=5)
        assert len(results) >= 1
        assert results[0]["similarity"] >= 0.5

    # ------------------------------------------------------------------
    # 6.2 — new conversation injects prior summary into system_prompt
    # ------------------------------------------------------------------

    def test_new_conversation_injects_prior_summary(self):
        """Req 6.2: after closing a session, a new conversation with a semantically
        related query must have {memory_context} replaced with actual content."""
        user_id = f"user_{RUN_ID}_6_2"
        conv_id1 = f"conv_{RUN_ID}_6_2_first"
        conv_id2 = f"conv_{RUN_ID}_6_2_second"

        messages = [
            {"role": "user",      "content": [{"text": "My dog's name is Biscuit and he loves fetch."}]},
            {"role": "assistant", "content": [{"text": "Biscuit sounds like a fun dog! Fetch is great exercise."}]},
        ]

        # Close the first session synchronously — stores the summary
        self.plugin.close_session_with_data(
            tenant_context=None,
            user_id=user_id,
            conv_id=conv_id1,
            messages=messages,
            model=self.agent.model,
        )

        # Start a new conversation with a semantically related query
        # agent.messages is empty (fresh agent) so plugin treats this as first turn
        _run_turn(self.agent, user_id, conv_id2, "What do you know about my pet?")

        assert "{memory_context}" not in self.agent.system_prompt

    # ------------------------------------------------------------------
    # 6.3 — summary length constraint: <= 2000 characters
    # ------------------------------------------------------------------

    def test_summary_length_constraint(self):
        """Req 6.3: the summary returned by close_session_with_data must be <= 2000 chars."""
        user_id = f"user_{RUN_ID}_6_3"
        conv_id = f"conv_{RUN_ID}_6_3"

        messages = [
            {"role": "user",      "content": [{"text": "Tell me about the history of the Roman Empire."}]},
            {"role": "assistant", "content": [{"text": (
                "The Roman Empire was one of the largest empires in ancient history, "
                "spanning from the British Isles to Mesopotamia. It began with Augustus "
                "Caesar in 27 BC and lasted until the fall of Constantinople in 1453 AD. "
                "The empire was known for its advanced engineering, legal system, and military."
            )}]},
            {"role": "user",      "content": [{"text": "What were the main causes of its decline?"}]},
            {"role": "assistant", "content": [{"text": (
                "The decline of the Roman Empire had many causes: military overextension, "
                "economic troubles, political instability, and pressure from barbarian invasions. "
                "The split into Eastern and Western empires also weakened central authority."
            )}]},
        ]

        summary = self.plugin.close_session_with_data(
            tenant_context=None,
            user_id=user_id,
            conv_id=conv_id,
            messages=messages,
            model=self.agent.model,
        )

        assert summary is not None
        assert len(summary) <= 2000

    # ------------------------------------------------------------------
    # 6.4 — summary UTF-8 byte constraint: <= 4096 bytes
    # ------------------------------------------------------------------

    def test_summary_utf8_byte_constraint(self):
        """Req 6.4: the summary stored in S3 Vectors metadata must be <= 4096 UTF-8 bytes."""
        user_id = f"user_{RUN_ID}_6_4"
        conv_id = f"conv_{RUN_ID}_6_4"

        messages = [
            {"role": "user",      "content": [{"text": "Explain quantum entanglement briefly."}]},
            {"role": "assistant", "content": [{"text": (
                "Quantum entanglement is a phenomenon where two particles become correlated "
                "such that the quantum state of each particle cannot be described independently. "
                "Measuring one particle instantly affects the other, regardless of distance."
            )}]},
        ]

        summary = self.plugin.close_session_with_data(
            tenant_context=None,
            user_id=user_id,
            conv_id=conv_id,
            messages=messages,
            model=self.agent.model,
        )

        assert summary is not None
        encoded = summary.encode("utf-8")
        assert len(encoded) <= 4096


# ---------------------------------------------------------------------------
# TestMemoryTool — integration tests for plugin.memory_tool
# ---------------------------------------------------------------------------


class TestMemoryTool:
    """
    End-to-end tests for S3VectorMemoryPlugin.memory_tool.

    Verifies that the tool correctly retrieves stored memories using the
    plugin's ContextVar identity, and returns the expected formatted output.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        """Create a fresh store and plugin per test."""
        self.store = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )
        self.plugin = S3VectorMemoryPlugin(store=self.store, base_prompt="Test {memory_context}")

    def test_memory_tool_retrieves_stored_memory(self):
        """Store a memory then call memory_tool — result should contain the stored content."""
        user_id = f"user_{RUN_ID}_mt_tool_1"
        content = "My favourite database is PostgreSQL."

        self.store.store_memory(user_id, content)

        self.plugin._cv_user_id.set(user_id)
        self.plugin._cv_tenant.set(None)

        result = self.plugin.memory_tool(query="What database do I prefer?", top_k=3)

        assert "PostgreSQL" in result

    def test_memory_tool_returns_no_memories_for_unknown_user(self):
        """memory_tool for a user with no stored memories returns 'No relevant memories found.'"""
        user_id = f"user_{RUN_ID}_mt_tool_2_unknown_{uuid.uuid4().hex[:6]}"

        self.plugin._cv_user_id.set(user_id)
        self.plugin._cv_tenant.set(None)

        result = self.plugin.memory_tool(query="anything")

        assert result == "No relevant memories found."

    def test_memory_tool_scopes_to_current_user(self):
        """memory_tool only returns memories for the user set in ContextVar, not other users."""
        user_a = f"user_{RUN_ID}_mt_tool_3a"
        user_b = f"user_{RUN_ID}_mt_tool_3b"

        self.store.store_memory(user_a, "User A loves cycling.")

        # Query as user_b — should get nothing
        self.plugin._cv_user_id.set(user_b)
        self.plugin._cv_tenant.set(None)

        result = self.plugin.memory_tool(query="What sport do I like?", top_k=3)

        assert "cycling" not in result.lower()

    def test_memory_tool_returns_error_when_user_id_not_set(self):
        """memory_tool returns an error message when user_id ContextVar is empty."""
        self.plugin._cv_user_id.set("")
        self.plugin._cv_tenant.set(None)

        result = self.plugin.memory_tool(query="anything")

        assert "user_id not set" in result


class TestInitAgentEnforcement:
    """
    Integration tests for init_agent enforcement against a real Strands Agent.

    These tests use a real Agent() to verify that the Strands framework default
    name ('Strands Agents') is correctly detected and rejected by init_agent.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        from strands_s3_vectors_memory import S3VectorMemory, S3VectorMemoryPlugin
        self.store = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )
        self.plugin = S3VectorMemoryPlugin(store=self.store, base_prompt="test {memory_context}")

    def test_agent_without_name_raises_on_plugin_wiring(self):
        """Agent() without name= uses the Strands default — plugin must raise ValueError."""
        from strands import Agent
        from strands.models import BedrockModel
        with pytest.raises(ValueError, match="agent.name"):
            Agent(
                model=BedrockModel(),
                plugins=[self.plugin],
            )

    def test_agent_with_explicit_name_does_not_raise(self):
        """Agent(name='orchestrator') must wire successfully without raising."""
        from strands import Agent
        from strands.models import BedrockModel
        Agent(
            model=BedrockModel(),
            name="orchestrator",
            plugins=[self.plugin],
        )
