"""
s3_vector_memory_plugin.py — Strands Plugin for long-term semantic memory

Works with both S3VectorMemory (single-tenant) and MultiTenantS3VectorMemory.
Decoupled from any SessionManager — composes independently with Valkey or any
other short-term session store.

BASE_PROMPT must contain a {memory_context} placeholder. The plugin fills it
with retrieved conversation summaries on the first turn of each conversation,
or replaces it with an empty string when no relevant memories are found.

Example BASE_PROMPT:
    BASE_PROMPT = \"\"\"You are a helpful assistant.

    {memory_context}

    Be concise and cite prior context when relevant.\"\"\"

invocation_state keys:
    user_id         (str)           — required
    conversation_id (str)           — required
    end_session     (bool)          — optional; triggers non-blocking session close
    tenant_context  (dict)          — required for MultiTenantS3VectorMemory only

Debug logging:
  import logging
  logging.getLogger("strands_s3_vectors_memory.s3_vector_memory_plugin").setLevel(logging.DEBUG)
"""

import hashlib
import logging
import contextvars
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from cachetools import TTLCache
from strands import Agent, Plugin, tool
from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent
from strands.plugins import hook

from .s3_vector_memory import S3VectorMemory

logger = logging.getLogger(__name__)

_MEMORY_PLACEHOLDER   = "{memory_context}"
_MEMORY_TOP_K         = 5
_SIMILARITY_THRESHOLD = 0.5
# The Strands framework sets agent.name to this string when the developer does not
# provide an explicit name. We treat it as "not set" — it is not a meaningful
# agent identity and must not be used as a memory namespace key.
_STRANDS_DEFAULT_AGENT_NAME = "Strands Agents"
_SUMMARIZE_PROMPT     = (
    "Summarize the following conversation in 2000 characters or fewer. "
    "Capture the key facts, decisions, and context. "
    "Be concise and factual. "
    "Do not use markdown formatting, headers, or bullet points — plain text only.\n\n"
)
_BUFFER_TTL     = 7200    # 2 hours — evict abandoned conversations
_BUFFER_MAXSIZE = 10_000  # max concurrent conversations in-process
_executor = ThreadPoolExecutor(max_workers=4)


class S3VectorMemoryPlugin(Plugin):
    """
    Strands Plugin for long-term semantic memory via S3 Vectors.

    Works with S3VectorMemory (single-tenant) or MultiTenantS3VectorMemory.
    Identity and lifecycle are passed via invocation_state — no explicit
    prepare() or close_session() calls required.

    BASE_PROMPT must contain {memory_context}. A warning is logged at
    construction time if the placeholder is missing.
    """

    name = "s3-vector-memory"

    def __init__(self, store: S3VectorMemory, base_prompt: str) -> None:
        super().__init__()
        self._store       = store
        self._base_prompt = base_prompt

        if _MEMORY_PLACEHOLDER not in base_prompt:
            logger.warning(
                "[s3-vector-memory] BASE_PROMPT does not contain '%s'. "
                "Retrieved memories will not be injected into the system prompt. "
                "Add {memory_context} to your BASE_PROMPT where memories should appear.",
                _MEMORY_PLACEHOLDER,
            )

        self._cv_tenant:  contextvars.ContextVar[Optional[Dict]] = contextvars.ContextVar("tenant", default=None)
        self._cv_user_id:    contextvars.ContextVar[str]            = contextvars.ContextVar("user_id", default="")
        self._cv_conv_id:    contextvars.ContextVar[str]            = contextvars.ContextVar("conv_id", default="")
        self._cv_has_sm:     contextvars.ContextVar[bool]           = contextvars.ContextVar("has_session_manager", default=False)
        self._cv_agent_name: contextvars.ContextVar[Optional[str]]  = contextvars.ContextVar("agent_name", default=None)
        self._conv_buffer:    TTLCache = TTLCache(maxsize=_BUFFER_MAXSIZE, ttl=_BUFFER_TTL)
        # Use a TTLCache instead of a plain set so entries are evicted automatically (#9)
        self._injected_convs: TTLCache = TTLCache(maxsize=_BUFFER_MAXSIZE, ttl=_BUFFER_TTL)

        logger.debug(
            "[s3-vector-memory] plugin init: store=%s buffer_ttl=%ds buffer_maxsize=%d",
            type(store).__name__, _BUFFER_TTL, _BUFFER_MAXSIZE,
        )

    # -----------------------------------------------------------------------
    # Hook-driven lifecycle — fired automatically by Strands
    # -----------------------------------------------------------------------

    def init_agent(self, agent: Agent) -> None:
        """Enforce agent.name is explicitly set at wiring time.

        The Strands framework sets agent.name to 'Strands Agents' when the
        developer does not provide a name. We treat this as unset — it is not
        a meaningful identity and must not be used as a memory namespace key.
        """
        if not agent.name or agent.name == _STRANDS_DEFAULT_AGENT_NAME:
            raise ValueError(
                "S3VectorMemoryPlugin requires agent.name to be explicitly set. "
                "Provide a stable, unique name that identifies this agent's role:\n\n"
                "    Agent(model=..., name='orchestrator', plugins=[plugin])\n\n"
                "The name is used as the memory namespace — it must be consistent "
                "across restarts so stored memories remain retrievable."
            )
        logger.debug("[s3-vector-memory] init_agent: agent_name=%s validated", agent.name)

    @hook
    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        """
        Fired before every agent() call. Reads identity from invocation_state,
        restores conversation buffer, resets system_prompt, and injects
        long-term memories into the {memory_context} placeholder on the first
        turn of each conversation.

        Required invocation_state keys: user_id, conversation_id
        Optional: tenant_context (multi-tenant), end_session (handled after)
        """
        state = event.invocation_state
        if "user_id" not in state:
            logger.debug("[s3-vector-memory] before_invocation: no user_id in state, skipping")
            return
        if "conversation_id" not in state:  # guard for missing key (#10)
            logger.warning(
                "[s3-vector-memory] before_invocation: 'conversation_id' missing from "
                "invocation_state — skipping memory setup."
            )
            return

        message = " ".join(
            b.get("text", "") for m in (event.messages or [])
            for b in m.get("content", []) if isinstance(b, dict) and "text" in b
        ).strip()

        self._setup(
            tenant_context  = state.get("tenant_context"),
            user_id         = state["user_id"],
            conversation_id = state["conversation_id"],
            message         = message,
            agent           = event.agent,
        )

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """
        Fired after every agent() call.
        - Snapshots agent.messages into the buffer (no-SessionManager mode).
        - If end_session=True, offloads summarization to a background thread.
        """
        state   = event.invocation_state
        agent   = event.agent
        conv_id = self._cv_conv_id.get("")

        if not self._cv_has_sm.get(False) and conv_id and agent:
            msg_count = len(agent.messages)
            self._conv_buffer[conv_id] = list(agent.messages)
            logger.debug(
                "[s3-vector-memory] after_invocation: buffered conv=%s messages=%d",
                conv_id, msg_count,
            )

        if state.get("end_session") and agent and conv_id:
            logger.debug(
                "[s3-vector-memory] after_invocation: end_session=True conv=%s submitting background close",
                conv_id,
            )
            future = _executor.submit(
                self.close_session_with_data,
                self._cv_tenant.get(None),
                self._cv_user_id.get(""),
                conv_id,
                list(self._conv_buffer.get(conv_id, agent.messages)),
                agent.model,
                self._cv_agent_name.get(None),  # capture before thread boundary
            )
            # Log any exception from the background task (#8)
            future.add_done_callback(
                lambda f: f.exception() and logger.error(
                    "[s3-vector-memory] background close_session_with_data failed: %s",
                    f.exception(),
                )
            )

    # -----------------------------------------------------------------------
    # Memory retrieval tool — expose to the agent for mid-turn recall
    # -----------------------------------------------------------------------

    @property
    def memory_tool(self):
        """
        Returns a Strands tool that lets the agent retrieve memories mid-turn.

        Identity (user_id, tenant_context) is read from the plugin's ContextVars,
        which are set by before_invocation before the LLM runs. The LLM only
        needs to provide the search query.

        Wire it at agent construction time:

            agent = Agent(
                model   = BedrockModel(),
                tools   = [plugin.memory_tool],
                plugins = [plugin],
            )
        """
        plugin_self = self  # capture for closure

        @tool
        def retrieve_memory(query: str, top_k: int = 3) -> str:
            """
            Retrieve relevant memories from past conversations for the current user.

            Use this when the user references something from a previous session
            that was not injected into the current system prompt, or when you
            need to recall a specific fact mid-conversation.

            Args:
                query:  What to search for — describe the topic or question in
                        natural language.
                top_k:  Maximum number of memories to return (default 3).

            Returns:
                Formatted string of relevant memories, or a message if none found.
            """
            user_id        = plugin_self._cv_user_id.get("")
            tenant_context = plugin_self._cv_tenant.get(None)

            if not user_id:
                return "Memory retrieval unavailable — user_id not set in current context."

            logger.debug(
                "[s3-vector-memory] retrieve_memory tool: user=%s query_len=%d top_k=%d",
                user_id, len(query), top_k,
            )

            try:
                results = plugin_self._store.retrieve_memories(
                    user_id        = user_id,
                    query          = query,
                    top_k          = top_k,
                    tenant_context = tenant_context,
                    agent_name     = plugin_self._cv_agent_name.get(None),
                )
                relevant = [r for r in results if r["similarity"] >= _SIMILARITY_THRESHOLD]
                if not relevant:
                    logger.debug(
                        "[s3-vector-memory] retrieve_memory tool: no results above threshold"
                    )
                    return "No relevant memories found."
                lines = "\n".join(
                    f"- [{r['stored_at']}] {r['content']}" for r in relevant
                )
                logger.debug(
                    "[s3-vector-memory] retrieve_memory tool: returning %d memories",
                    len(relevant),
                )
                return f"Relevant memories:\n{lines}"
            except Exception as exc:
                logger.warning(
                    "[s3-vector-memory] retrieve_memory tool failed: %s", exc
                )
                return f"Memory retrieval failed: {exc}"

        return retrieve_memory

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _setup(self, tenant_context: Optional[Dict], user_id: str,
               conversation_id: str, message: str, agent: Agent) -> None:
        """Bind identity, restore buffer, reset prompt, inject memories."""
        self._cv_tenant.set(tenant_context)
        self._cv_user_id.set(user_id)
        self._cv_conv_id.set(conversation_id)
        self._cv_agent_name.set(agent.name or None)

        has_sm = agent._session_manager is not None
        self._cv_has_sm.set(has_sm)

        if not has_sm:
            buffered = self._conv_buffer.get(conversation_id, [])
            agent.messages = list(buffered)
            logger.debug(
                "[s3-vector-memory] _setup: restored %d messages for conv=%s",
                len(buffered), conversation_id,
            )

        if conversation_id in self._injected_convs:
            # Subsequent turn — restore the already-injected prompt
            agent.system_prompt = self._conv_buffer.get(
                f"_prompt_{conversation_id}", self._base_prompt
            )
            logger.debug(
                "[s3-vector-memory] _setup: subsequent turn conv=%s prompt restored from cache",
                conversation_id,
            )
        else:
            # First turn — inject memories into placeholder
            logger.debug(
                "[s3-vector-memory] _setup: first turn conv=%s user=%s building prompt",
                conversation_id, user_id,
            )
            agent.system_prompt = self._build_prompt(message, tenant_context, user_id)
            self._injected_convs[conversation_id] = True  # TTLCache entry
            self._conv_buffer[f"_prompt_{conversation_id}"] = agent.system_prompt

    def _build_prompt(self, query: str, tenant_context: Optional[Dict],
                      user_id: str) -> str:
        """Retrieve memories and fill the {memory_context} placeholder."""
        if _MEMORY_PLACEHOLDER not in self._base_prompt:
            return self._base_prompt

        if not query or not query.strip():  # short-circuit on empty message (#11)
            logger.debug("[s3-vector-memory] _build_prompt: empty query, skipping retrieval")
            return self._base_prompt.replace(_MEMORY_PLACEHOLDER, "").strip()

        memory_section = ""
        try:
            memories = self._store.retrieve_memories(
                user_id        = user_id,
                query          = query,
                top_k          = _MEMORY_TOP_K,
                tenant_context = tenant_context,
                agent_name     = self._cv_agent_name.get(None),
            )
            relevant = [m for m in memories if m["similarity"] >= _SIMILARITY_THRESHOLD]
            if relevant:
                lines          = "\n".join(f"- {m['content']}" for m in relevant)
                memory_section = f"Relevant context from previous conversations:\n{lines}"
                logger.debug(
                    "[s3-vector-memory] _build_prompt: injecting %d memories user=%s",
                    len(relevant), user_id,
                )
            else:
                logger.debug(
                    "[s3-vector-memory] _build_prompt: %d results below threshold=%.2f user=%s",
                    len(memories), _SIMILARITY_THRESHOLD, user_id,
                )
        except Exception as exc:
            logger.warning("[s3-vector-memory] retrieve failed: %s", exc)

        return self._base_prompt.replace(_MEMORY_PLACEHOLDER, memory_section).strip()

    def close_session_with_data(
        self,
        tenant_context: Optional[Dict],
        user_id: str,
        conv_id: str,
        messages: List,
        model,
        agent_name: Optional[str] = None,
    ) -> Optional[str]:
        """Summarize and store. Safe to call from a background thread."""
        logger.debug(
            "[s3-vector-memory] close_session_with_data: conv=%s user=%s agent=%s messages=%d",
            conv_id, user_id, agent_name, len(messages),
        )

        if not messages:
            logger.debug("[s3-vector-memory] close_session_with_data: no messages, returning None")
            return None

        transcript_lines = []
        for m in messages:
            role = m.get("role", "unknown").upper()
            text = " ".join(
                b.get("text", "") for b in m.get("content", [])
                if isinstance(b, dict) and "text" in b
            ).strip()
            if text:
                transcript_lines.append(f"{role}: {text}")

        if not transcript_lines:
            logger.debug(
                "[s3-vector-memory] close_session_with_data: no text content in messages conv=%s",
                conv_id,
            )
            return None

        logger.debug(
            "[s3-vector-memory] close_session_with_data: transcript lines=%d conv=%s",
            len(transcript_lines), conv_id,
        )

        from strands import Agent as _Agent
        summary = str(
            _Agent(model=model, system_prompt=_SUMMARIZE_PROMPT, callback_handler=None)(
                "\n".join(transcript_lines)
            )
        ).strip()

        # Truncate at the last sentence boundary <= 2000 chars (#12)
        if len(summary) > 2000:
            truncated = summary[:2000]
            last_boundary = max(
                truncated.rfind(". "),
                truncated.rfind("! "),
                truncated.rfind("? "),
            )
            if last_boundary > 0:
                summary = truncated[:last_boundary + 1]
            else:
                summary = truncated  # no sentence boundary found — hard truncate

        if not summary:
            return None

        logger.debug(
            "[s3-vector-memory] close_session_with_data: summary_len=%d conv=%s",
            len(summary), conv_id,
        )

        key = f"{user_id}_{agent_name or 'default'}_summary_{hashlib.sha256(conv_id.encode()).hexdigest()[:16]}"
        # _embed is inside try so that the finally always clears buffers (#7)
        try:
            embedding = self._store._embed(summary, purpose="GENERIC_INDEX")
            client    = self._store._get_s3vectors_client(tenant_context)
            client.put_vectors(
                vectorBucketName=self._store.bucket_name,
                indexName=self._store._build_index_name(tenant_context),
                vectors=[{
                    "key":  key,
                    "data": {"float32": [float(x) for x in embedding]},
                    "metadata": {
                        "user_id":         user_id,
                        "agent_name":      agent_name or "default",
                        "content":         summary[:4096],
                        "conversation_id": conv_id,
                        "type":            "summary",
                    },
                }],
            )
            logger.debug(
                "[s3-vector-memory] close_session_with_data: stored summary key=%s conv=%s",
                key, conv_id,
            )
        finally:
            self._conv_buffer.pop(conv_id, None)
            self._conv_buffer.pop(f"_prompt_{conv_id}", None)
            self._injected_convs.pop(conv_id, None)
            logger.debug(
                "[s3-vector-memory] close_session_with_data: buffers cleared conv=%s", conv_id,
            )

        return summary
