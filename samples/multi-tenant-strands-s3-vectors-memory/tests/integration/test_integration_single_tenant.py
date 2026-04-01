"""
Integration tests for single-tenant S3VectorMemory.

Covers: TestSingleTenantStoreRetrieve (Reqs 2.1, 2.2, 2.3, 2.5, 7.2)
        TestEmbedding (Reqs 4.1, 4.2, 4.3)

Requires:
  - S3_VECTOR_BUCKET_NAME env var set
  - Valid AWS credentials with s3vectors + bedrock-runtime access
"""

import os
import math
import threading
import uuid

import pytest

from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
from tests.integration._constants import BUCKET_NAME, AWS_REGION, BEDROCK_MODEL_ID, RUN_ID

# Embedding model is separate from the chat model
EMBEDDING_MODEL_ID: str = os.environ.get("EMBEDDING_MODEL", "amazon.nova-2-multimodal-embeddings-v1:0")


class TestSingleTenantStoreRetrieve:
    """End-to-end store/retrieve tests for single-tenant S3VectorMemory (Reqs 2.1–2.5)."""

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        """Ensure the 'memory' index exists before any test in this class runs."""
        self.mem = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )

    # ------------------------------------------------------------------
    # 2.1 — store_memory returns success
    # ------------------------------------------------------------------

    def test_store_returns_success(self):
        """Req 2.1: store_memory must return a dict with status == 'success'."""
        user_id = f"user_{RUN_ID}_test2_1"
        content = "I enjoy hiking in the mountains on weekends."

        result = self.mem.store_memory(user_id, content)

        assert result["status"] == "success"

    # ------------------------------------------------------------------
    # 2.2 / 7.2 — retrieve with semantically similar query returns result
    # ------------------------------------------------------------------

    def test_retrieve_similar_query_returns_result(self):
        """Reqs 2.2, 7.2: retrieve with a similar query returns >= 1 result with similarity >= 0.5."""
        user_id = f"user_{RUN_ID}_test2_2"
        content = "My favourite hobby is playing chess with friends."
        query   = "What games do I like to play?"

        self.mem.store_memory(user_id, content)
        results = self.mem.retrieve_memories(user_id, query, top_k=5)

        assert len(results) >= 1, "Expected at least one result for a semantically similar query"
        assert results[0]["similarity"] >= 0.5, (
            f"Expected similarity >= 0.5, got {results[0]['similarity']}"
        )

    # ------------------------------------------------------------------
    # 2.3 — different user returns empty list
    # ------------------------------------------------------------------

    def test_retrieve_different_user_returns_empty(self):
        """Req 2.3: retrieving under a different user_id must return an empty list."""
        user_a = f"user_{RUN_ID}_test2_3a"
        user_b = f"user_{RUN_ID}_test2_3b"
        content = "I love cooking Italian food at home."

        self.mem.store_memory(user_a, content)
        results = self.mem.retrieve_memories(user_b, content, top_k=5)

        assert results == [], (
            f"Expected empty list for user_b, got {results}"
        )

    # ------------------------------------------------------------------
    # 2.4 / 2.5 — _build_index_name always returns "memory"
    # ------------------------------------------------------------------

    def test_index_name_is_always_memory(self):
        """Req 2.5: _build_index_name() must return 'memory' with and without tenant_context."""
        assert self.mem._build_index_name() == "memory"
        assert self.mem._build_index_name(tenant_context=None) == "memory"
        assert self.mem._build_index_name(tenant_context={"tenantId": "some-tenant"}) == "memory"


class TestEmbedding:
    """Embedding correctness tests for S3VectorMemory._embed (Reqs 4.1, 4.2, 4.3)."""

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        """Ensure the 'memory' index exists before any test in this class runs."""
        self.mem = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )

    # ------------------------------------------------------------------
    # 4.1 — _embed returns exactly 1024 floats
    # ------------------------------------------------------------------

    def test_embed_returns_1024_floats(self):
        """Req 4.1: _embed must return a list of exactly 1024 float values."""
        vec = self.mem._embed("hello world")

        assert len(vec) == 1024, f"Expected 1024 dimensions, got {len(vec)}"
        assert all(isinstance(x, float) for x in vec), "All elements must be float"

    # ------------------------------------------------------------------
    # 4.2 — _embed is thread-safe across 5 concurrent threads
    # ------------------------------------------------------------------

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
            except Exception as exc:  # noqa: BLE001
                exceptions.append(exc)

        threads = [threading.Thread(target=embed_and_collect, args=(t,)) for t in texts]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert exceptions == [], f"Exceptions raised in threads: {exceptions}"

    # ------------------------------------------------------------------
    # 4.3 — _embed is deterministic (cosine similarity > 0.99)
    # ------------------------------------------------------------------

    def test_embed_determinism(self):
        """Req 4.3: two _embed calls with identical text must have cosine similarity > 0.99."""
        text = "determinism check for embedding model"

        vec1 = self.mem._embed(text)
        vec2 = self.mem._embed(text)

        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(x * x for x in vec1))
        norm2 = math.sqrt(sum(x * x for x in vec2))
        cosine_sim = dot / (norm1 * norm2)

        assert cosine_sim > 0.99, (
            f"Expected cosine similarity > 0.99 for identical inputs, got {cosine_sim:.6f}"
        )


# ---------------------------------------------------------------------------
# Imports for TestPluginLifecycle
# ---------------------------------------------------------------------------
import time
import unittest.mock
import strands_s3_vectors_memory.s3_vector_memory_plugin as _plugin_module
from strands_s3_vectors_memory.s3_vector_memory_plugin import S3VectorMemoryPlugin

# BASE_PROMPT used by the plugin lifecycle tests.
# Intentionally omits {memory_context} so that when no memories are found,
# the plugin returns self._base_prompt unchanged — making system_prompt == BASE_PROMPT
# a reliable assertion (Reqs 5.2, 8.2, 8.3).
BASE_PROMPT = "You are a helpful assistant. Be concise."


def _make_agent(plugin: S3VectorMemoryPlugin):
    """Create a minimal Strands Agent with the plugin attached."""
    from strands import Agent
    from strands.models import BedrockModel
    return Agent(
        model=BedrockModel(
            model_id=BEDROCK_MODEL_ID,
            region_name=AWS_REGION,
        ),
        system_prompt=BASE_PROMPT,
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
    End-to-end plugin lifecycle tests for S3VectorMemoryPlugin (Reqs 5.1–5.6, 8.1–8.3, 9.1–9.3).

    Each test creates its own plugin instance to ensure full isolation.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        """Ensure the 'memory' index exists and create a fresh plugin per test."""
        store = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )
        self.plugin = S3VectorMemoryPlugin(store=store, base_prompt=BASE_PROMPT)
        self.agent  = _make_agent(self.plugin)

    # ------------------------------------------------------------------
    # 5.1 — first turn adds conversation_id to _injected_convs
    # ------------------------------------------------------------------

    def test_first_turn_adds_conv_to_injected(self):
        """Req 5.1: after turn 1, conversation_id must be in plugin._injected_convs."""
        user_id = f"user_{RUN_ID}_5_1"
        conv_id = f"conv_{RUN_ID}_5_1"

        _run_turn(self.agent, user_id, conv_id, "hello")

        assert conv_id in self.plugin._injected_convs

    # ------------------------------------------------------------------
    # 5.2 / 8.3 — first turn with no stored memories sets BASE_PROMPT
    # ------------------------------------------------------------------

    def test_first_turn_no_memories_sets_base_prompt(self):
        """Reqs 5.2, 8.3: fresh user with no memories → system_prompt == BASE_PROMPT."""
        # Use a unique user_id that has never stored anything
        user_id = f"user_{RUN_ID}_5_2_fresh_{uuid.uuid4().hex[:6]}"
        conv_id = f"conv_{RUN_ID}_5_2"

        _run_turn(self.agent, user_id, conv_id, "hello")

        assert self.agent.system_prompt == BASE_PROMPT

    # ------------------------------------------------------------------
    # 5.3 — second turn restores message buffer
    # ------------------------------------------------------------------

    def test_second_turn_restores_message_buffer(self):
        """Req 5.3: turn 2 restores agent.messages from the buffer saved after turn 1."""
        user_id = f"user_{RUN_ID}_5_3"
        conv_id = f"conv_{RUN_ID}_5_3"

        _run_turn(self.agent, user_id, conv_id, "first message")
        messages_after_turn1 = list(self.agent.messages)

        # Reset agent messages to simulate a fresh request context
        self.agent.messages = []

        _run_turn(self.agent, user_id, conv_id, "second message")

        # After turn 2, the buffer from turn 1 should have been restored before
        # the new message was appended — so agent.messages should be non-empty
        # and contain at least the turn-1 messages
        assert len(self.agent.messages) >= len(messages_after_turn1)

    # ------------------------------------------------------------------
    # 5.4 — new conversation resets system_prompt (no prompt bleed)
    # ------------------------------------------------------------------

    def test_new_conversation_resets_system_prompt(self):
        """Req 5.4: starting conv B after conv A must not bleed conv A's prompt."""
        user_id = f"user_{RUN_ID}_5_4_fresh_{uuid.uuid4().hex[:6]}"
        conv_a  = f"conv_{RUN_ID}_5_4a"
        conv_b  = f"conv_{RUN_ID}_5_4b"

        _run_turn(self.agent, user_id, conv_a, "first conversation")

        # Manually corrupt the system_prompt to simulate bleed scenario
        self.agent.system_prompt = "CORRUPTED PROMPT"

        # Start a fresh conversation — plugin must reset to BASE_PROMPT
        _run_turn(self.agent, user_id, conv_b, "second conversation")

        assert self.agent.system_prompt == BASE_PROMPT

    # ------------------------------------------------------------------
    # 5.5 — end_session=True clears buffers after background summarization
    # ------------------------------------------------------------------

    def test_end_session_clears_buffers(self):
        """Req 5.5: end_session=True removes conv_id from _conv_buffer and _injected_convs."""
        user_id = f"user_{RUN_ID}_5_5"
        conv_id = f"conv_{RUN_ID}_5_5"

        # Turn 1 — populate buffers
        _run_turn(self.agent, user_id, conv_id, "hello, remember this")

        assert conv_id in self.plugin._injected_convs

        # Turn 2 with end_session=True — triggers background summarization + cleanup
        _run_turn(self.agent, user_id, conv_id, "goodbye", end_session=True)

        # Wait for the background thread to complete (up to 30 s)
        deadline = time.time() + 30
        while time.time() < deadline:
            if conv_id not in self.plugin._conv_buffer and conv_id not in self.plugin._injected_convs:
                break
            time.sleep(0.5)

        assert conv_id not in self.plugin._conv_buffer, (
            "conv_id should be removed from _conv_buffer after end_session"
        )
        assert conv_id not in self.plugin._injected_convs, (
            "conv_id should be removed from _injected_convs after end_session"
        )

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
        """Req 8.2: when all retrieved memories have similarity < 0.5, system_prompt == BASE_PROMPT."""
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

        assert self.agent.system_prompt == BASE_PROMPT

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

        assert count_after_turn2 > count_after_turn1, (
            f"Expected message count to grow: turn1={count_after_turn1}, turn2={count_after_turn2}"
        )

    # ------------------------------------------------------------------
    # 5.10 — two conversations have separate buffers
    # ------------------------------------------------------------------

    def test_two_conversations_have_separate_buffers(self):
        """Req 9.2: two distinct conversation_ids must have independent _conv_buffer entries."""
        user_id   = f"user_{RUN_ID}_5_10"
        conv_id_1 = f"conv_{RUN_ID}_5_10a"
        conv_id_2 = f"conv_{RUN_ID}_5_10b"

        _run_turn(self.agent, user_id, conv_id_1, "conversation one")
        _run_turn(self.agent, user_id, conv_id_2, "conversation two")

        assert conv_id_1 in self.plugin._conv_buffer, "conv_id_1 should be in _conv_buffer"
        assert conv_id_2 in self.plugin._conv_buffer, "conv_id_2 should be in _conv_buffer"

        # Verify they are independent — modifying one does not affect the other
        original_buf_2 = list(self.plugin._conv_buffer[conv_id_2])
        self.plugin._conv_buffer[conv_id_1] = []
        assert list(self.plugin._conv_buffer[conv_id_2]) == original_buf_2, (
            "Modifying conv_id_1 buffer must not affect conv_id_2 buffer"
        )

    # ------------------------------------------------------------------
    # 5.11 — closed conversation restarts fresh
    # ------------------------------------------------------------------

    def test_closed_conversation_restarts_fresh(self):
        """Req 9.3: after end_session=True, restarting same conv_id is treated as fresh."""
        user_id = f"user_{RUN_ID}_5_11"
        conv_id = f"conv_{RUN_ID}_5_11"

        # Turn 1 — establish conversation
        _run_turn(self.agent, user_id, conv_id, "initial message")
        assert conv_id in self.plugin._injected_convs

        # Close the session
        _run_turn(self.agent, user_id, conv_id, "closing message", end_session=True)

        # Wait for background cleanup
        deadline = time.time() + 30
        while time.time() < deadline:
            if conv_id not in self.plugin._conv_buffer and conv_id not in self.plugin._injected_convs:
                break
            time.sleep(0.5)

        assert conv_id not in self.plugin._injected_convs, (
            "conv_id should be cleared from _injected_convs after end_session"
        )
        assert conv_id not in self.plugin._conv_buffer, (
            "conv_id should be cleared from _conv_buffer after end_session"
        )

        # Restart the same conversation — should be treated as fresh (first turn)
        _run_turn(self.agent, user_id, conv_id, "restarted conversation")

        # After restart, conv_id should be back in _injected_convs (fresh injection)
        assert conv_id in self.plugin._injected_convs, (
            "Restarted conversation should be treated as fresh (added to _injected_convs)"
        )
        # Buffer should only contain the new turn's messages (fresh start)
        assert conv_id in self.plugin._conv_buffer, (
            "Restarted conversation should have a new buffer entry"
        )


# ---------------------------------------------------------------------------
# BASE_PROMPT with {memory_context} placeholder — required for summary injection tests
# ---------------------------------------------------------------------------
SUMMARY_BASE_PROMPT = (
    "You are a helpful assistant.\n\n"
    "{memory_context}\n\n"
    "Be concise and cite prior context when relevant."
)


class TestConversationSummary:
    """
    End-to-end summary storage and recall tests (Reqs 6.1–6.4).

    Each test creates its own plugin instance with SUMMARY_BASE_PROMPT so that
    the {memory_context} placeholder is available for memory injection.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, memory_index):
        """Ensure the 'memory' index exists and create a fresh plugin per test."""
        store = S3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
        )
        self.store  = store
        self.plugin = S3VectorMemoryPlugin(store=store, base_prompt=SUMMARY_BASE_PROMPT)
        from strands import Agent
        from strands.models import BedrockModel
        self.agent  = Agent(
            model=BedrockModel(
                model_id=BEDROCK_MODEL_ID,
                region_name=AWS_REGION,
            ),
            system_prompt=SUMMARY_BASE_PROMPT,
            plugins=[self.plugin],
            callback_handler=None,
        )

    # ------------------------------------------------------------------
    # 6.1 — close_session_with_data writes summary within 30 seconds
    # ------------------------------------------------------------------

    def test_end_session_writes_summary_within_30s(self):
        """Req 6.1: close_session_with_data (synchronous) must complete within 30 s
        and the summary vector must be retrievable from the index."""
        user_id = f"user_{RUN_ID}_6_1"
        conv_id = f"conv_{RUN_ID}_6_1"

        # Build a minimal conversation transcript
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

        assert elapsed <= 30, (
            f"close_session_with_data took {elapsed:.1f}s — must complete within 30 s"
        )
        assert summary is not None, "Expected a non-None summary to be returned"

        # Verify the summary vector was written to the index
        results = self.store.retrieve_memories(user_id=user_id, query=summary, top_k=5)
        assert len(results) >= 1, (
            "Expected at least one result when querying with the summary text"
        )
        assert results[0]["similarity"] >= 0.5, (
            f"Expected similarity >= 0.5 for the stored summary, got {results[0]['similarity']}"
        )

    # ------------------------------------------------------------------
    # 6.2 — new conversation injects prior summary into system_prompt
    # ------------------------------------------------------------------

    def test_new_conversation_injects_prior_summary(self):
        """Req 6.2: after closing a session, a new conversation with a semantically
        related query must have {memory_context} replaced with actual content."""
        user_id  = f"user_{RUN_ID}_6_2"
        conv_id1 = f"conv_{RUN_ID}_6_2_first"
        conv_id2 = f"conv_{RUN_ID}_6_2_second"

        # Build a conversation about a specific topic
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
        _run_turn(self.agent, user_id, conv_id2, "What do you know about my pet?")

        # The {memory_context} placeholder must have been replaced with actual content
        assert "{memory_context}" not in self.agent.system_prompt, (
            "Expected {memory_context} placeholder to be replaced with actual memory content"
        )

    # ------------------------------------------------------------------
    # 6.3 — summary length constraint: <= 500 characters
    # ------------------------------------------------------------------

    def test_summary_length_constraint(self):
        """Req 6.3: the summary returned by close_session_with_data must be <= 500 chars."""
        user_id = f"user_{RUN_ID}_6_3"
        conv_id = f"conv_{RUN_ID}_6_3"

        # Use a longer conversation to exercise the truncation path
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

        assert summary is not None, "Expected a non-None summary"
        assert len(summary) <= 500, (
            f"Summary length {len(summary)} exceeds 500 characters: {summary!r}"
        )

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

        assert summary is not None, "Expected a non-None summary"
        encoded = summary.encode("utf-8")
        assert len(encoded) <= 4096, (
            f"Summary UTF-8 byte length {len(encoded)} exceeds 4096 bytes"
        )


# ---------------------------------------------------------------------------
# TestMemoryTool — integration tests for plugin.memory_tool
# ---------------------------------------------------------------------------

import time as _time
from strands_s3_vectors_memory.s3_vector_memory_plugin import S3VectorMemoryPlugin


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

        # Store a memory directly
        self.store.store_memory(user_id, content)

        # Set ContextVars as before_invocation would
        self.plugin._cv_user_id.set(user_id)
        self.plugin._cv_tenant.set(None)

        result = self.plugin.memory_tool(query="What database do I prefer?", top_k=3)

        assert "PostgreSQL" in result, (
            f"Expected stored memory content in tool result, got: {result!r}"
        )

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

        assert "cycling" not in result.lower(), (
            f"Expected no results for user_b, but got: {result!r}"
        )

    def test_memory_tool_returns_error_when_user_id_not_set(self):
        """memory_tool returns an error message when user_id ContextVar is empty."""
        self.plugin._cv_user_id.set("")
        self.plugin._cv_tenant.set(None)

        result = self.plugin.memory_tool(query="anything")

        assert "user_id not set" in result
