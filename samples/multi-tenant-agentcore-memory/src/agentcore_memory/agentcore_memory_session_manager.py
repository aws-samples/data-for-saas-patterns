# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
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

Isolation is enforced in two places, over both tiers:

  Application : actor_id / session_id embed tenantId, agentName, userId, conversationId
  IAM         : every memory call is signed with TVM credentials (STS AssumeRole +
                TenantID session tag); the TVM role's ABAC conditions make AWS deny
                any actorId or namespace outside the tagged tenant

What IAM catches and what it does not: both places take the tenant from the same
tenant_context, so IAM stops a request for another tenant's actor or namespace
(a bug in identifier construction, or code that bypasses the session manager),
but it cannot catch a wrong tenantId in tenant_context itself. The tenantId must
come from a source the caller cannot change (here, an immutable Cognito
attribute in a JWT verified by AgentCore Runtime). Isolation between users of
the same tenant is application-only: the IAM condition is `${TenantID}:*`.

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

Start-up placeholder: the base SDK writes an initial session when the Agent is
constructed, before any tenant is known. That placeholder runs under TVM
credentials tagged with the reserved BOOTSTRAP_TENANT_ID, so the container's own
role needs no AgentCore Memory permissions at all. It is never read back, so
every start-up begins from the state the code builds.

Legacy lookups: when a record is missing, the SDK also looks for it under a
legacy actor id ("session_<id>" / "agent_<id>") that no tenant owns. This class
skips those lookups: IAM would deny them, and they would read outside the tenant.

Required invocation_state keys per request:
    tenant_context  (dict)  — must contain 'tenantId'
    user_id         (str)
    conversation_id (str)
Calls that fire the hook without them (for example Agent.structured_output) are
refused with IsolationError.

Env vars: AGENTCORE_MEMORY_ID, AGENTCORE_TVM_ROLE_ARN, AWS_REGION
"""

import contextlib
import copy
import logging
import os
import re
from typing import Dict, Optional

from strands.agent.state import AgentState
from strands.hooks import BeforeInvocationEvent
from strands.hooks.registry import HookRegistry
from strands.types.session import SessionAgent
from bedrock_agentcore.memory.integrations.strands.config import RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemoryConfig,
    AgentCoreMemorySessionManager,
)

from .token_vending_machine import (
    BOOTSTRAP_TENANT_ID,
    IsolationError,
    TokenVendingMachine,
    validate_tenant_id,
)

logger = logging.getLogger(__name__)

# Stable agent name embedded into actor_id / session_id. Must remain constant
# across process restarts so stored memories stay retrievable.
AGENT_NAME = "assistant"

# user_id and conversation_id must fit the AgentCore sessionId character set.
_ID_PART_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_SESSION_ID_LENGTH = 100   # AgentCore Memory sessionId limit

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


def _validate_id_part(name: str, value) -> str:
    if not isinstance(value, str) or not _ID_PART_PATTERN.match(value):
        raise IsolationError(
            f"{name} must match ^[A-Za-z0-9][A-Za-z0-9_-]{{0,127}}$."
        )
    return value


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
                "not isolated by IAM."
            )
        if "boto_session" in kwargs:
            raise ValueError(
                "boto_session is not supported: every memory call must use TVM credentials."
            )

        self.memory_id   = memory_id
        self.agent_name  = agent_name
        self.region_name = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._tvm        = TokenVendingMachine(
            role_arn=tvm_role_arn,
            region_name=self.region_name,
        )

        # Clean agent snapshot taken at start-up, applied when a request switches
        # to a session that has no stored agent state (see _restore_agent_state).
        self._clean_agent: Dict[str, SessionAgent] = {}

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
        # _prepare(). The placeholder lives under the reserved bootstrap tenant
        # and runs under bootstrap TVM credentials, never the container's own.
        self._placeholder_actor   = f"{BOOTSTRAP_TENANT_ID}:{agent_name}:init"
        self._placeholder_session = f"{BOOTSTRAP_TENANT_ID}-{agent_name}-init"
        placeholder = AgentCoreMemoryConfig(
            memory_id=memory_id,
            actor_id=self._placeholder_actor,
            session_id=self._placeholder_session,
            retrieval_config=self._retrieval_config,
        )
        super().__init__(
            agentcore_memory_config=placeholder,
            region_name=self.region_name,
            boto_session=self._tvm.get_bootstrap_session(),
            **kwargs,
        )
        # Reuse the base client config (user agent, retries) on every client swap.
        self._client_config = getattr(
            getattr(self.memory_client.gmdp_client, "meta", None), "config", None
        )
        logger.debug(
            "[memory] session manager init: memory_id=%s region=%s agent=%s",
            memory_id, self.region_name, agent_name,
        )

    # -- identifiers ---------------------------------------------------------

    def _build_actor_id(self, tenant_context: Dict, user_id: str) -> str:
        """Return '{tenantId}:{agentName}:{userId}' — scopes all memory ops to tenant+agent+user."""
        tenant_id = validate_tenant_id(tenant_context)
        _validate_id_part("user_id", user_id)
        return f"{tenant_id}:{self.agent_name}:{user_id}"

    def _build_session_id(self, tenant_context: Dict, user_id: str, conversation_id: str) -> str:
        """Return '{tenantId}-{agentName}-{userId}-{conversationId}' — scopes memory to one conversation."""
        tenant_id = validate_tenant_id(tenant_context)
        _validate_id_part("user_id", user_id)
        _validate_id_part("conversation_id", conversation_id)
        session_id = f"{tenant_id}-{self.agent_name}-{user_id}-{conversation_id}"
        if len(session_id) > _MAX_SESSION_ID_LENGTH:
            raise IsolationError(
                f"session id would be {len(session_id)} characters; AgentCore allows "
                f"{_MAX_SESSION_ID_LENGTH}. tenant id, agent name, user id and "
                "conversation id share that limit."
            )
        return session_id

    # -- SDK overrides -------------------------------------------------------

    @contextlib.contextmanager
    def _current_actor_only(self):
        """
        While active, list_events calls for any actor other than the current one
        return no events without calling AWS. This skips the SDK's legacy
        "session_<id>" / "agent_<id>" lookups. It also records whether a lookup
        for the current actor failed or returned events, so a read error or an
        unreadable record is not mistaken for "no stored record".
        """
        client   = self.memory_client
        original = client.list_events
        status   = {"failed": False, "returned": False}

        def list_events(*args, **kwargs):
            actor_id = kwargs.get("actor_id", args[1] if len(args) > 1 else None)
            if actor_id != self.config.actor_id:
                return []
            try:
                events = original(*args, **kwargs)
            except Exception:
                status["failed"] = True
                raise
            if events:
                status["returned"] = True
            return events

        client.list_events = list_events
        try:
            yield status
        finally:
            client.list_events = original

    def _is_placeholder(self, session_id: str) -> bool:
        return session_id == self._placeholder_session

    def read_session(self, session_id: str, **kwargs):
        # The start-up placeholder is never read back: each boot starts clean,
        # whatever an earlier deploy stored there.
        if self._is_placeholder(session_id):
            return None
        with self._current_actor_only():
            return super().read_session(session_id, **kwargs)

    def read_agent(self, session_id: str, agent_id: str, **kwargs):
        if self._is_placeholder(session_id):
            return None
        with self._current_actor_only() as status:
            result = super().read_agent(session_id, agent_id, **kwargs)
        if result is None and status["failed"]:
            raise IsolationError("could not read the stored agent record; not resetting it.")
        if result is None and status["returned"]:
            raise IsolationError("the stored agent record could not be parsed; not overwriting it.")
        return result

    def _read_messages(self, session_id: str, agent_id: str, offset: int) -> list:
        """list_messages, but a read error raises instead of returning []."""
        with self._current_actor_only() as status:
            messages = self.list_messages(session_id, agent_id, offset=offset)
        if status["failed"]:
            raise IsolationError("could not read the conversation history.")
        return messages

    def initialize(self, agent, **kwargs) -> None:
        """Keep a clean copy of the agent's state as built by the code, then run the base start-up."""
        self._clean_agent[agent.agent_id] = copy.deepcopy(SessionAgent.from_agent(agent))
        super().initialize(agent, **kwargs)

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        """Register all AgentCore hooks plus the per-request tenant injection hook."""
        super().register_hooks(registry, **kwargs)
        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)

    # -- per-request binding -------------------------------------------------

    def _on_before_invocation(self, event) -> None:
        state = event.invocation_state or {}
        missing = [k for k in ("tenant_context", "user_id", "conversation_id") if k not in state]
        if missing:
            raise IsolationError(
                f"invocation_state is missing {', '.join(missing)}; every call must "
                "carry the tenant identity (structured_output is not supported)."
            )
        self._prepare(event.agent, state["tenant_context"], state["user_id"], state["conversation_id"])

    def _prepare(self, agent, tenant_context: Dict, user_id: str, conversation_id: str) -> None:
        """
        Mutate this session manager for the current request.

        Order matters: swap credentials BEFORE any read so every read runs
        under TVM credentials (IAM ABAC).

        (a) Get TVM-scoped credentials for this tenant (STS AssumeRole + TenantID tag).
        (b) Build tenant-scoped actor_id/session_id and detect whether the actor
            or conversation changed since the last request on this singleton.
        (c) Bind config.actor_id, config.session_id, and self.session_id (the
            RepositorySessionManager base field used by append_message/create_agent).
        (d) Set config.retrieval_config so long-term memory is semantically
            retrieved every turn and injected as a <user_context> block.
        (e) Swap the boto3 clients on the singleton to the scoped credentials.
        (f) On a switch, replace everything the agent carries from the previous
            session: agent.state, conversation-manager state, internal state and
            agent.messages. Stored state for the new session is read back from
            AgentCore Memory; a session with none gets the clean start-up state.
            If that fails, the agent is cleared and unbound before the error is
            raised, so no later request can run on the previous transcript.
            This is also what makes recall survive a fresh microVM: on
            AgentCore Runtime the Agent is a module-level singleton and a
            "known" session can land on a fresh microVM (idle/8h termination)
            with an empty agent.messages. Assigning agent.messages directly does
            not re-emit MessageAddedEvent, so restored history is not re-persisted.

        Raises IsolationError if an identifier is missing or invalid, STS
        AssumeRole fails, or the stored record cannot be read.
        """
        # (a) TVM-scoped credentials for this tenant (validates the tenant id)
        scoped_session = self._tvm.get_session(tenant_context)

        # (b) tenant-scoped identifiers; detect a switch on actor OR session
        actor_id   = self._build_actor_id(tenant_context, user_id)
        session_id = self._build_session_id(tenant_context, user_id, conversation_id)
        switching  = (actor_id, session_id) != (self.config.actor_id, self.config.session_id)

        # (c) bind the singleton to this request
        self.config.actor_id   = actor_id
        self.config.session_id = session_id
        self.session_id        = session_id

        # (d) enable per-turn LTM retrieval (both strategy namespaces)
        self.config.retrieval_config = self._retrieval_config

        # (e) swap clients to scoped credentials BEFORE any read
        client_kwargs = {"region_name": self.region_name}
        if self._client_config is not None:
            client_kwargs["config"] = self._client_config
        self.memory_client.gmcp_client = scoped_session.client("bedrock-agentcore-control", **client_kwargs)
        self.memory_client.gmdp_client = scoped_session.client("bedrock-agentcore", **client_kwargs)

        logger.debug(
            "[memory] _prepare: tenant=%s actor_id=%s session_id=%s switching=%s",
            tenant_context.get("tenantId"), actor_id, session_id, switching,
        )

        # (f) on a switch, drop the previous session's state and restore this one's
        if switching:
            try:
                self._switch_to(agent, session_id)
            except Exception:
                self._unbind(agent)
                raise

    def _switch_to(self, agent, session_id: str) -> None:
        prepend  = self._restore_agent_state(agent, session_id)
        restored = self._read_messages(
            session_id, agent.agent_id,
            offset=agent.conversation_manager.removed_message_count,
        )
        if getattr(agent.model, "stateful", False):
            # Server-managed conversation: nothing to restore locally.
            agent.messages = []
        else:
            agent.messages = self._fix_broken_tool_use(prepend + [m.to_message() for m in restored])
        self._latest_agent_message[agent.agent_id] = restored[-1] if restored else None
        self._held_records[agent.agent_id] = list(restored)
        logger.debug(
            "[memory] _prepare: rehydrated %d message(s) for session=%s",
            len(restored), session_id,
        )

    def _restore_agent_state(self, agent, session_id: str) -> list:
        """
        Replace the agent's state with the new session's stored state, or a clean one.

        Returns any messages the conversation manager asks to prepend (for
        example a stored summary).
        """
        stored: Optional[SessionAgent] = self.read_agent(session_id, agent.agent_id)
        source = stored or self._clean_agent.get(agent.agent_id)
        if source is None:
            raise IsolationError("agent was not initialised by this session manager.")
        self._apply(agent, source)
        prepend = agent.conversation_manager.restore_from_session(
            copy.deepcopy(source.conversation_manager_state)
        )
        if stored is None:
            # New session: record the agent with its own created_at, as the base
            # initialize() does, instead of inheriting the previous session's.
            self.create_agent(session_id, SessionAgent.from_agent(agent))
        # Force the next sync to write this session's state under its own actor.
        self._last_synced_internal_state.pop(agent.agent_id, None)
        return prepend or []

    @staticmethod
    def _apply(agent, source: SessionAgent) -> None:
        source = copy.deepcopy(source)
        agent.state = AgentState(source.state)
        source.initialize_internal_state(agent)

    def _unbind(self, agent) -> None:
        """After a failed switch: drop everything and bind to the placeholder ids."""
        agent.messages = []
        clean = self._clean_agent.get(agent.agent_id)
        if clean is not None:
            self._apply(agent, clean)
            try:
                agent.conversation_manager.restore_from_session(
                    copy.deepcopy(clean.conversation_manager_state)
                )
            except Exception:  # best effort; the agent is unbound either way
                logger.warning("[memory] could not reset conversation manager after a failed switch")
        self.config.actor_id   = self._placeholder_actor
        self.config.session_id = self._placeholder_session
        self.session_id        = self._placeholder_session
        self._latest_agent_message[agent.agent_id] = None
        self._held_records[agent.agent_id] = []
        self._last_synced_internal_state.pop(agent.agent_id, None)
