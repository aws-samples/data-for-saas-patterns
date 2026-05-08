"""
s3_vector_memory_plugin.py — Strands Plugin for long-term semantic memory

The plugin is stateless and relies on the hosting platform (AgentCore Runtime
microVM) or an optional SessionManager to maintain agent.messages across turns.
This plugin owns long-term semantic memory (S3 Vectors summaries).

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
_executor = ThreadPoolExecutor(max_workers=4)


class S3VectorMemoryPlugin(Plugin):
    """
    Strands Plugin for long-term semantic memory via S3 Vectors.

    The plugin is stateless and relies on the hosting platform (AgentCore Runtime
    microVM) or an optional SessionManager to maintain agent.messages across turns.
    This plugin owns long-term semantic memory: it retrieves relevant summaries on
    the first turn of each conversation and stores a new summary when end_session=True.

    Works with S3VectorMemory (single-tenant) or MultiTenantS3VectorMemory.
    Identity and lifecycle are passed via invocation_state.

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

        self._cv_tenant:     contextvars.ContextVar[Optional[Dict]] = contextvars.ContextVar("tenant", default=None)
        self._cv_user_id:    contextvars.ContextVar[str]            = contextvars.ContextVar("user_id", default="")
        self._cv_conv_id:    contextvars.ContextVar[str]            = contextvars.ContextVar("conv_id", default="")
        self._cv_agent_name: contextvars.ContextVar[Optional[str]]  = contextvars.ContextVar("agent_name", default=None)

        logger.debug(
            "[s3-vector-memory] plugin init: store=%s", type(store).__name__,
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
        Fired before every agent() call.

        - Binds per-request identity from invocation_state.
        - Determines turn number from agent.messages:
            - Empty  → turn 1: retrieve long-term memories and inject into prompt.
            - Non-empty → subsequent turn: strip placeholder from prompt.
        """
        state = event.invocation_state
        agent = event.agent

        if "user_id" not in state:
            logger.debug("[s3-vector-memory] before_invocation: no user_id in state, skipping")
            return
        if "conversation_id" not in state:
            logger.warning(
                "[s3-vector-memory] before_invocation: 'conversation_id' missing from "
                "invocation_state — skipping memory setup."
            )
            return

        if agent._session_manager is None:
            # No SessionManager attached. This is correct on AgentCore Runtime
            # where the microVM persists agent.messages across turns of the same
            # session. On long-running servers (FastAPI, ECS) a SessionManager
            # is required for durability — without it, agent.messages is lost
            # when the process restarts.
            logger.debug(
                "[s3-vector-memory] before_invocation: no SessionManager — "
                "relying on in-process agent.messages (AgentCore Runtime mode)"
            )

        self._cv_tenant.set(state.get("tenant_context"))
        self._cv_user_id.set(state["user_id"])
        self._cv_conv_id.set(state["conversation_id"])
        self._cv_agent_name.set(agent.name or None)

        message = " ".join(
            b.get("text", "") for m in (event.messages or [])
            for b in m.get("content", []) if isinstance(b, dict) and "text" in b
        ).strip()

        # agent.messages is maintained by the hosting platform (AgentCore microVM)
        # or an optional SessionManager.
        # Empty  → no prior turns exist → this is turn 1.
        # Non-empty → prior turns present → subsequent turn.
        is_first_turn = len(agent.messages) == 0

        if is_first_turn:
            logger.debug(
                "[s3-vector-memory] before_invocation: turn=1 conv=%s user=%s "
                "(agent.messages empty — querying S3 Vectors)",
                state["conversation_id"], state["user_id"],
            )
            agent.system_prompt = self._build_prompt(
                message,
                state.get("tenant_context"),
                state["user_id"],
            )
        else:
            logger.debug(
                "[s3-vector-memory] before_invocation: turn>1 conv=%s "
                "(agent.messages has %d messages — skipping S3 Vectors query)",
                state["conversation_id"], len(agent.messages),
            )
            # Subsequent turn — strip the {memory_context} placeholder so the LLM
            # doesn't see the literal placeholder string. Memories were injected on
            # turn 1 and are already in the conversation history via agent.messages.
            agent.system_prompt = self._base_prompt.replace(
                _MEMORY_PLACEHOLDER, ""
            ).strip()

    @hook
    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """
        Fired after every agent() call.

        If end_session=True, reads agent.messages and offloads summarization
        + S3 Vectors storage to a background thread.
        """
        state   = event.invocation_state
        agent   = event.agent
        conv_id = self._cv_conv_id.get("")

        if state.get("end_session") and agent and conv_id:
            logger.debug(
                "[s3-vector-memory] after_invocation: end_session=True conv=%s "
                "messages=%d submitting background close",
                conv_id, len(agent.messages),
            )
            # agent.messages is the authoritative conversation transcript.
            future = _executor.submit(
                self.close_session_with_data,
                self._cv_tenant.get(None),
                self._cv_user_id.get(""),
                conv_id,
                list(agent.messages),   # snapshot before background thread runs
                agent.model,
                self._cv_agent_name.get(None),
            )
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

    def _build_prompt(self, query: str, tenant_context: Optional[Dict],
                      user_id: str) -> str:
        """Retrieve memories and fill the {memory_context} placeholder."""
        if _MEMORY_PLACEHOLDER not in self._base_prompt:
            return self._base_prompt

        if not query or not query.strip():  # short-circuit on empty message
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
        """Summarize and store the conversation. Safe to call from a background thread."""
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
        # Call the model directly rather than constructing a new Agent — avoids
        # re-initialising the Strands framework and hook registry on every end_session.
        summary = str(
            _Agent(model=model, system_prompt=_SUMMARIZE_PROMPT, callback_handler=None)(
                "\n".join(transcript_lines)
            )
        ).strip()

        # Truncate at the last sentence boundary <= 2000 chars
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
        try:
            embedding = self._store._embed(summary, purpose="GENERIC_INDEX")
            client    = self._store._get_s3vectors_client(tenant_context)
            client.put_vectors(
                vectorBucketName=self._store.bucket_name,
                indexName=self._store._build_index_name(tenant_context),
                vectors=[{
                    "key":  key,
                    "data": {"float32": embedding},
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
        except Exception as exc:
            logger.error(
                "[s3-vector-memory] close_session_with_data: failed to store summary "
                "conv=%s error=%s", conv_id, exc,
            )
            raise

        return summary
