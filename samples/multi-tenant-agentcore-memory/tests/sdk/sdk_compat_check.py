# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
sdk_compat_check.py — Run the session manager against the REAL strands and
bedrock_agentcore packages (no stubs), with AWS calls replaced by fakes that
enforce the TVM role's ABAC rule: a client built from a session tagged
TenantID=X may only touch actors starting with "X:". Anything else raises
AccessDeniedException, as IAM would. A small in-memory event store stands in
for AgentCore Memory, so stored state and history are really restored.

It checks that:
  * the session manager can be constructed, twice (a second boot);
  * every AgentCore Memory client is built from a TVM session, never from the
    container's default credentials;
  * no SDK call touches an actor outside its session's tenant;
  * a request through the real BeforeInvocationEvent hook restores the stored
    agent state and transcript of its own session;
  * agent state and transcript from tenant A do not reach tenant B;
  * a second boot uses the state the code builds, not an earlier boot's;
  * a failed history read fails the request and unbinds the agent;
  * the SDK internals the session manager relies on still exist.

Run by tests/sdk/test_sdk_compat.py in a separate process, so the stubs in
tests/unit/conftest.py cannot leak in. Prints OK and exits 0 on success.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_REGION", "us-east-1")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from bedrock_agentcore.memory.client import MemoryClient  # noqa: E402
from strands import Agent  # noqa: E402
from strands.hooks import BeforeInvocationEvent  # noqa: E402
from strands.models import BedrockModel  # noqa: E402
from strands.types.session import SessionAgent, SessionMessage  # noqa: E402

from agentcore_memory import (  # noqa: E402
    AGENT_NAME,
    BOOTSTRAP_TENANT_ID,
    IsolationError,
    MultiTenantAgentCoreMemorySessionManager,
    TokenVendingMachine,
)

memory_clients = []   # (tenant tag or None, service name)
denied = []           # (tag, actor) pairs IAM would have refused
store = {}            # (actor, session) -> {"agent": [events], "messages": [events]}
failing_actors = set()


def _deny(op):
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, op)


def _allowed(tag, actor_id):
    return tag is not None and isinstance(actor_id, str) and actor_id.startswith(f"{tag}:")


def fake_client(self, service_name, *args, **kwargs):
    client = MagicMock()
    tag = getattr(self, "_tvm_tag", None)
    client._tvm_tag = tag
    if service_name.startswith("bedrock-agentcore"):
        memory_clients.append((tag, service_name))

        def create_event(**kw):
            if not _allowed(tag, kw.get("actorId")):
                denied.append((tag, kw.get("actorId")))
                raise _deny("CreateEvent")
            return {"event": {"eventId": "e-1"}}

        client.create_event.side_effect = create_event
    return client


def fake_list_events(self, memory_id, actor_id, session_id, event_metadata=None, **kwargs):
    tag = getattr(self.gmdp_client, "_tvm_tag", None)
    if not _allowed(tag, actor_id):
        denied.append((tag, actor_id))
        raise _deny("ListEvents")
    if actor_id in failing_actors:
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ListEvents")
    entry = store.get((actor_id, session_id), {})
    return list(entry.get("agent" if event_metadata else "messages", []))


def fake_assume(self, tenant_id):
    session = boto3.Session(region_name="us-east-1")
    session._tvm_tag = tenant_id
    return session


def seed(tenant, user, conv, state, text):
    actor = f"{tenant}:{AGENT_NAME}:{user}"
    session = f"{tenant}-{AGENT_NAME}-{user}-{conv}"
    record = SessionAgent(agent_id="default", state=state, conversation_manager_state={
        "__name__": "SlidingWindowConversationManager", "removed_message_count": 0,
    })
    message = SessionMessage(message={"role": "user", "content": [{"text": text}]}, message_id=0)
    store[(actor, session)] = {
        "agent": [{"payload": [{"blob": json.dumps(record.to_dict())}]}],
        "messages": [{"payload": [{"conversational": {
            "content": {"text": json.dumps(message.to_dict())}, "role": "USER"}}]}],
    }


def build(initial_state):
    mgr = MultiTenantAgentCoreMemorySessionManager(
        memory_id="mem-test-000",
        tvm_role_arn="arn:aws:iam::123456789012:role/TestTvmRole",
        region_name="us-east-1",
    )
    agent = Agent(
        model=BedrockModel(model_id="amazon.nova-pro-v1:0", region_name="us-east-1"),
        session_manager=mgr,
        callback_handler=None,
        state=initial_state,
    )
    return mgr, agent


def invoke_hook(agent, tenant, user, conv):
    agent.hooks.invoke_callbacks(BeforeInvocationEvent(agent=agent, invocation_state={
        "tenant_context": {"tenantId": tenant}, "user_id": user, "conversation_id": conv,
    }))


def texts(agent):
    return [c.get("text") for m in agent.messages for c in m.get("content", [])]


problems = []
with patch.object(boto3.session.Session, "client", fake_client), \
     patch.object(TokenVendingMachine, "_assume", fake_assume), \
     patch.object(MemoryClient, "list_events", fake_list_events):
    seed("tenant-001", "user-1", "conv-1", {"lang": "fr"}, "tenant-001 history")
    try:
        mgr, agent = build({"v": 1})
    except Exception as e:  # construction must succeed under ABAC
        print(f"FAIL: construction failed: {type(e).__name__}: {e}")
        sys.exit(1)

    invoke_hook(agent, "tenant-001", "user-1", "conv-1")
    if agent.state.get("lang") != "fr":
        problems.append(f"stored state not restored: {agent.state.get()}")
    if texts(agent) != ["tenant-001 history"]:
        problems.append(f"stored transcript not restored: {texts(agent)}")

    invoke_hook(agent, "tenant-002", "user-1", "conv-1")
    if agent.state.get("lang") is not None or texts(agent):
        problems.append("tenant-001 state or transcript reached tenant-002")
    if agent.state.get("v") != 1:
        problems.append("new session did not get the clean start-up state")
    if not mgr.config.actor_id.startswith("tenant-002:"):
        problems.append(f"unexpected actor id {mgr.config.actor_id}")

    failing_actors.add(f"tenant-003:{AGENT_NAME}:user-1")
    try:
        invoke_hook(agent, "tenant-003", "user-1", "conv-1")
        problems.append("a failed history read did not fail the request")
    except IsolationError:
        if not mgr.config.session_id.startswith(f"{BOOTSTRAP_TENANT_ID}-") or agent.messages:
            problems.append("agent not unbound after a failed history read")

    try:
        mgr2, agent2 = build({"v": 2})
    except Exception as e:
        problems.append(f"second boot failed: {type(e).__name__}: {e}")
    else:
        invoke_hook(agent2, "tenant-004", "user-1", "conv-1")
        if agent2.state.get("v") != 2:
            problems.append(f"second boot used an earlier boot's state: {agent2.state.get()}")

    untagged = [svc for tag, svc in memory_clients if tag is None]
    if untagged:
        problems.append(f"memory clients built from default credentials: {untagged}")
    if not any(tag == BOOTSTRAP_TENANT_ID for tag, _ in memory_clients):
        problems.append("start-up placeholder did not use the bootstrap TVM session")
    if denied:
        problems.append(f"calls IAM would deny: {denied}")
    for name in ("_held_records", "_latest_agent_message", "_last_synced_internal_state",
                 "_fix_broken_tool_use", "_agent_created_at_cache"):
        if not hasattr(mgr, name):
            problems.append(f"SDK no longer has {name}")

if problems:
    for p in problems:
        print(f"FAIL: {p}")
    sys.exit(1)
print("OK")
