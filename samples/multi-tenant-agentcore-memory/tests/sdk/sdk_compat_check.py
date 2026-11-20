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
  * start-up makes no AgentCore Memory call and does not assume the TVM role
    (AgentCore Runtime SnapStart runs it at build time), and every memory call
    is made with a TVM session, never the container's credentials;
  * no SDK call touches an actor outside its session's tenant;
  * a request through the real BeforeInvocationEvent hook restores the stored
    agent state and transcript of its own session;
  * agent state and transcript from tenant A do not reach tenant B;
  * a failed agent-record read, a failed history read and a corrupt history
    event each fail the request and unbind the agent;
  * a second boot uses the state the code builds, not a placeholder record an
    older deploy left behind;
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

denied = []           # (tag, actor) pairs IAM would have refused
store = {}            # (actor, session) -> {"agent": [events], "messages": [events]}
failing_record_reads = set()    # actors whose agent-record read is throttled
assumed_tags = []               # TenantID tags the TVM was asked for
memory_calls = []               # (tag, operation) for every AgentCore Memory call
failing_history_reads = set()   # actors whose transcript read is throttled


def _deny(op):
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, op)


def _allowed(tag, actor_id):
    return tag is not None and isinstance(actor_id, str) and actor_id.startswith(f"{tag}:")


def fake_client(self, service_name, *args, **kwargs):
    client = MagicMock()
    tag = getattr(self, "_tvm_tag", None)
    client._tvm_tag = tag
    if service_name.startswith("bedrock-agentcore"):

        def create_event(**kw):
            memory_calls.append((tag, "CreateEvent"))
            if not _allowed(tag, kw.get("actorId")):
                denied.append((tag, kw.get("actorId")))
                raise _deny("CreateEvent")
            state_type = (kw.get("metadata") or {}).get("stateType", {}).get("stringValue")
            kind = {"AGENT": "agent", "SESSION": "session"}.get(state_type, "messages")
            entry = store.setdefault((kw["actorId"], kw.get("sessionId")), {})
            entry.setdefault(kind, []).insert(0, {"payload": kw.get("payload", [])})  # newest first
            return {"event": {"eventId": f"e-{len(entry[kind])}"}}

        client.create_event.side_effect = create_event
    return client


def fake_list_events(self, memory_id, actor_id, session_id, event_metadata=None, **kwargs):
    tag = getattr(self.gmdp_client, "_tvm_tag", None)
    memory_calls.append((tag, "ListEvents"))
    if not _allowed(tag, actor_id):
        denied.append((tag, actor_id))
        raise _deny("ListEvents")
    if event_metadata:
        kind = "session" if '"SESSION"' in json.dumps(event_metadata, default=str) else "agent"
        if actor_id in failing_record_reads:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ListEvents")
    else:
        kind = "messages"
        if actor_id in failing_history_reads:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "ListEvents")
    return list(store.get((actor_id, session_id), {}).get(kind, []))


def fake_assume(self, tenant_id):
    assumed_tags.append(tenant_id)
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

    def expect_unbound(label, tenant):
        try:
            invoke_hook(agent, tenant, "user-1", "conv-1")
            problems.append(f"{label} did not fail the request")
        except IsolationError:
            if not mgr.config.session_id.startswith(f"{BOOTSTRAP_TENANT_ID}-") or agent.messages:
                problems.append(f"agent not unbound after {label}")

    failing_record_reads.add(f"tenant-003:{AGENT_NAME}:user-1")
    expect_unbound("a failed agent-record read", "tenant-003")

    failing_history_reads.add(f"tenant-005:{AGENT_NAME}:user-1")
    expect_unbound("a failed history read", "tenant-005")

    seed("tenant-006", "user-1", "conv-1", {}, "unused")
    store[(f"tenant-006:{AGENT_NAME}:user-1", f"tenant-006-{AGENT_NAME}-user-1-conv-1")]["messages"] = [
        {"payload": [{"conversational": {"content": {"text": "not json"}, "role": "USER"}}]}
    ]
    expect_unbound("a corrupt history event", "tenant-006")

    if store.get((f"{BOOTSTRAP_TENANT_ID}:{AGENT_NAME}:init", f"{BOOTSTRAP_TENANT_ID}-{AGENT_NAME}-init")):
        problems.append("start-up wrote a placeholder record")

    # An older deploy of the sample did write a placeholder; a new boot must ignore it.
    seed(BOOTSTRAP_TENANT_ID, AGENT_NAME, "init", {"v": 1}, "old placeholder")
    store[(f"{BOOTSTRAP_TENANT_ID}:{AGENT_NAME}:init", f"{BOOTSTRAP_TENANT_ID}-{AGENT_NAME}-init")] = \
        store.pop((f"{BOOTSTRAP_TENANT_ID}:{AGENT_NAME}:{AGENT_NAME}",
                   f"{BOOTSTRAP_TENANT_ID}-{AGENT_NAME}-{AGENT_NAME}-init"))
    calls_before_boot2 = len(memory_calls)
    try:
        mgr2, agent2 = build({"v": 2})
    except Exception as e:
        problems.append(f"second boot failed: {type(e).__name__}: {e}")
    else:
        if len(memory_calls) != calls_before_boot2:
            problems.append(f"start-up made AgentCore Memory calls: {memory_calls[calls_before_boot2:]}")
        invoke_hook(agent2, "tenant-004", "user-1", "conv-1")
        if agent2.state.get("v") != 2:
            problems.append(f"second boot used an earlier boot's state: {agent2.state.get()}")

    if BOOTSTRAP_TENANT_ID in assumed_tags:
        problems.append("start-up assumed the TVM role")
    if any(tag is None for tag, _ in memory_calls):
        problems.append("a memory call ran without a TVM session")
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
