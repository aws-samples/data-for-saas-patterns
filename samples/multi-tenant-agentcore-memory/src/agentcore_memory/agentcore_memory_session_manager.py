"""
agentcore_memory_session_manager.py — Tenant-Isolated Bedrock AgentCore Memory

A drop-in Strands `SessionManager` that scopes every AgentCore Memory operation
to a single tenant. It implements BOTH memory tiers:

  Short-term memory (STM)  The ordered conversation transcript for one session_id.
                           Written per turn as events, and restored into
                           agent.messages on a session change in _prepare() via
                           list_messages(). agent.messages is only a warm-microVM
                           cache; the durable record lives in AgentCore Memory, so
                           recall survives a fresh microVM. Scoped to ONE conversation.
  Long-term memory (LTM)   Durable facts/preferences extracted asynchronously by
                           the memory strategies into namespaces, and retrieved by
                           semantic search on every turn via config.retrieval_config
                           (injected as a <user_context> block). Spans MULTIPLE
                           conversations for the same actor.

Isolation is enforced at two independent layers, over both tiers:

  Application : actor_id / session_id embed tenantId, agentName, userId, conversationId
  IAM         : TVM (STS AssumeRole + TenantID session tag) enforces at the control plane

Why memory is the most dangerous surface: memory is *implicit*. The agent never
explicitly asks to "load tenant-001's memories" — the framework does it
automatically on every turn. Isolation must therefore be baked into the session
manager configuration and the IAM role, not left to application logic.

AgentCore Memory identifiers:

  actorId    (STM + LTM) Identifies who the memory belongs to. Constructed as
             `{tenantId}:{agentName}:{userId}`. The IAM condition
             `bedrock-agentcore:actorId` enforces that a session tagged
             TenantID=tenant-001 can only touch actors starting with `tenant-001:`.

  sessionId  (STM) Scopes the short-term transcript to one conversation.
             Constructed as `{tenantId}-{agentName}-{userId}-{conversationId}`.

  namespace  (LTM) Scopes long-term (semantic) memory retrieval. Scoped as
             `/{actorId}/...`. The IAM condition `bedrock-agentcore:namespacePath`
             (StringLike) restricts prefix retrieval to the current tenant's
             namespace. Note: the SDK's LTM retrieval issues a `namespacePath`
             (prefix) request, so the TVM role MUST condition on `namespacePath`,
             not `namespace` — conditioning on `namespace` silently returns zero
             records.

Required invocation_state keys per request:
    tenant_context  (dict)  — must contain 'tenantId'
    user_id         (str)
    conversation_id (str)

Env vars: AGENTCORE_MEMORY_ID, AGENTCORE_TVM_ROLE_ARN, AWS_REGION
"""

import logging
import os
from typing import Dict

from strands.hooks import BeforeInvocationEvent
from strands.hooks.registry import HookRegistry
from bedrock_agentcore.memory.integrations.strands.config import RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemoryConfig,
    AgentCoreMemorySessionManager,
)

from .token_vending_machine import TokenVendingMachine, IsolationError

logger = logging.getLogger(__name__)

# Stable agent name embedded into actor_id / session_id. Must remain constant
# across process restarts so stored memories stay retrievable.
AGENT_NAME = "assistant"

# Long-term memory namespace templates. The base session manager resolves
# {actorId} to the current tenant-scoped actor_id at retrieval time, so each
# resolved path (e.g. /tenant-001:assistant:user-456/preferences) always starts
# with the tenant id — matching the TVM role's bedrock-agentcore:namespacePath
# ABAC condition (/${aws:PrincipalTag/TenantID}:*). These MUST match the
# namespaces the memory strategies write extracted records to
# (see scripts/setup_memory.sh): a USER_PREFERENCE strategy writes to
# .../preferences and a SEMANTIC strategy writes to .../facts.
MEMORY_NAMESPACE_PREFERENCES = "/{actorId}/preferences"
MEMORY_NAMESPACE_FACTS       = "/{actorId}/facts"


class MultiTenantAgentCoreMemorySessionManager(AgentCoreMemorySessionManager):
    """
    Multi-tenant AgentCoreMemorySessionManager singleton.

    Extends AgentCoreMemorySessionManager with a BeforeInvocationEvent hook that
    reads tenant identity from invocation_state and updates the config and boto3
    clients before each request. All existing AgentCore hooks are preserved via
    super().register_hooks().
    """

    def __init__(
        self,
        memory_id: str,
        tvm_role_arn: str = None,
        region_name: str = None,
        agent_name: str = AGENT_NAME,
        **kwargs,
    ) -> None:
        if not memory_id:
            raise ValueError("memory_id must be a non-empty string.")

        tvm_role_arn = tvm_role_arn or os.environ.get("AGENTCORE_TVM_ROLE_ARN")
        if not tvm_role_arn:
            # Fail fast — omitting the TVM role would silently bypass IAM ABAC isolation.
            raise ValueError(
                "tvm_role_arn is required (pass it or set AGENTCORE_TVM_ROLE_ARN). "
                "Without it, memory operations run with ambient credentials and are "
                "not isolated at the IAM control plane."
            )

        self.memory_id   = memory_id
        self.agent_name  = agent_name
        self.region_name = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._tvm        = TokenVendingMachine(
            role_arn=tvm_role_arn,
            region_name=self.region_name,
        )

        # Tracks the last restored memory record per agent so a session rehydrate
        # does not re-persist history it just read back from AgentCore Memory.
        self._latest_agent_message: Dict[str, object] = {}

        # retrieval_config drives long-term memory recall: before each turn the
        # base session manager resolves these namespaces against the current
        # actor_id and injects any matching long-term records into the prompt as
        # a <user_context> block. Two strategies back these namespaces
        # (see scripts/setup_memory.sh):
        #   /{actorId}/preferences — USER_PREFERENCE strategy
        #   /{actorId}/facts       — SEMANTIC strategy
        # Without retrieval_config the SDK's per-turn LTM hook is a no-op.
        self._retrieval_config = {
            MEMORY_NAMESPACE_PREFERENCES: RetrievalConfig(top_k=5, relevance_score=0.3),
            MEMORY_NAMESPACE_FACTS:       RetrievalConfig(top_k=5, relevance_score=0.4),
        }

        # Placeholder config — actor_id/session_id are overwritten per request in
        # _prepare(). retrieval_config is static (the {actorId} template resolves
        # per turn), so it is safe to set here once.
        placeholder = AgentCoreMemoryConfig(
            memory_id=memory_id,
            actor_id="init",
            session_id="init",
            retrieval_config=self._retrieval_config,
        )
        super().__init__(
            agentcore_memory_config=placeholder,
            region_name=self.region_name,
            **kwargs,
        )
        logger.debug(
            "[memory] session manager init: memory_id=%s region=%s agent=%s",
            memory_id, self.region_name, agent_name,
        )

    def _build_actor_id(self, tenant_context: Dict, user_id: str) -> str:
        """Return '{tenantId}:{agentName}:{userId}' — scopes all memory ops to tenant+agent+user."""
        tenant_id = tenant_context.get("tenantId") if tenant_context else None
        if not tenant_id:
            raise IsolationError("tenant_context must contain a non-empty 'tenantId'.")
        return f"{tenant_id}:{self.agent_name}:{user_id}"

    def _build_session_id(self, tenant_context: Dict, user_id: str, conversation_id: str) -> str:
        """Return '{tenantId}-{agentName}-{userId}-{conversationId}' — scopes memory to one conversation."""
        tenant_id = tenant_context.get("tenantId") if tenant_context else None
        if not tenant_id:
            raise IsolationError("tenant_context must contain a non-empty 'tenantId'.")
        return f"{tenant_id}-{self.agent_name}-{user_id}-{conversation_id}"

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        """Register all AgentCore hooks plus the per-request tenant injection hook."""
        super().register_hooks(registry, **kwargs)
        registry.add_callback(
            BeforeInvocationEvent,
            lambda event: self._prepare(
                event.agent,
                event.invocation_state["tenant_context"],
                event.invocation_state["user_id"],
                event.invocation_state["conversation_id"],
            ),
        )

    def _prepare(self, agent, tenant_context: Dict, user_id: str, conversation_id: str) -> None:
        """
        Mutate this session manager for the current request.

        Order matters: swap credentials BEFORE any read so list_messages runs
        under TVM credentials (IAM ABAC), not ambient container credentials.

        (a) Get TVM-scoped credentials for this tenant (STS AssumeRole + TenantID tag).
        (b) Build tenant-scoped actor_id/session_id and detect whether the
            conversation changed since the last request on this singleton.
        (c) Bind config.actor_id, config.session_id, and self.session_id (the
            RepositorySessionManager base field used by append_message/create_agent).
        (d) Set config.retrieval_config so long-term memory is semantically
            retrieved every turn and injected as a <user_context> block.
        (e) Swap the boto3 clients on the singleton to the scoped credentials.
        (f) On session change, rehydrate agent.messages (short-term memory) from
            AgentCore Memory. This is what makes recall survive a fresh microVM:
            on AgentCore Runtime the Agent is a module-level singleton and a
            "known" session can land on a fresh microVM (idle/8h termination)
            with an empty agent.messages. Assigning agent.messages directly does
            not re-emit MessageAddedEvent, so restored history is not re-persisted.

        Raises IsolationError if tenantId is missing or STS AssumeRole fails.
        """
        # (a) TVM-scoped credentials for this tenant
        scoped_session = self._tvm.get_session(tenant_context)

        # (b) tenant-scoped identifiers; detect whether the conversation changed
        actor_id   = self._build_actor_id(tenant_context, user_id)
        session_id = self._build_session_id(tenant_context, user_id, conversation_id)
        switching  = session_id != self.config.session_id

        # (c) bind the singleton to this request
        self.config.actor_id   = actor_id
        self.config.session_id = session_id
        self.session_id        = session_id

        # (d) enable per-turn LTM retrieval (both strategy namespaces)
        self.config.retrieval_config = self._retrieval_config

        # (e) swap clients to scoped credentials BEFORE any read
        self.memory_client.gmcp_client = scoped_session.client(
            "bedrock-agentcore-control", region_name=self.region_name
        )
        self.memory_client.gmdp_client = scoped_session.client(
            "bedrock-agentcore", region_name=self.region_name
        )

        logger.debug(
            "[memory] _prepare: tenant=%s actor_id=%s session_id=%s switching=%s",
            tenant_context.get("tenantId"), actor_id, session_id, switching,
        )

        # (f) STM: rehydrate the transcript from AgentCore Memory only on session
        #     change. A fresh microVM under a known session has empty
        #     agent.messages; the durable record must be read back.
        if switching:
            restored = self.list_messages(session_id, agent.agent_id)
            agent.messages = [m.to_message() for m in restored]
            self._latest_agent_message[agent.agent_id] = restored[-1] if restored else None
            logger.debug(
                "[memory] _prepare: rehydrated %d message(s) for session=%s",
                len(restored), session_id,
            )
