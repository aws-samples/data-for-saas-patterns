# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
sdk_compat_check.py — Run the session manager against the REAL strands and
bedrock_agentcore packages (no stubs), with AWS calls replaced by fakes that
enforce the TVM role's ABAC rule: a client built from a session tagged
TenantID=X may only touch actors starting with "X:". Anything else raises
AccessDeniedException, as IAM would.

It checks that:
  * the session manager can be constructed (start-up placeholder included);
  * every AgentCore Memory client is built from a TVM session, never from the
    container's default credentials;
  * no SDK call touches an actor outside its session's tenant;
  * agent state from tenant A does not reach tenant B on a session switch;
  * the SDK internals the session manager relies on still exist.

Run by tests/sdk/test_sdk_compat.py in a separate process, so the stubs in
tests/unit/conftest.py cannot leak in. Prints OK and exits 0 on success.
"""

import os
import sys
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_REGION", "us-east-1")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from bedrock_agentcore.memory.client import MemoryClient  # noqa: E402
from strands import Agent  # noqa: E402
from strands.models import BedrockModel  # noqa: E402

from agentcore_memory import BOOTSTRAP_TENANT_ID, MultiTenantAgentCoreMemorySessionManager, TokenVendingMachine  # noqa: E402

memory_clients = []   # (tenant tag or None, service name)
denied = []           # (tag, actor) pairs IAM would have refused


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


def fake_list_events(self, memory_id, actor_id, session_id, **kwargs):
    tag = getattr(self.gmdp_client, "_tvm_tag", None)
    if not _allowed(tag, actor_id):
        denied.append((tag, actor_id))
        raise _deny("ListEvents")
    return []


def fake_assume(self, tenant_id):
    session = boto3.Session(region_name="us-east-1")
    session._tvm_tag = tenant_id
    return session


problems = []
with patch.object(boto3.session.Session, "client", fake_client), \
     patch.object(TokenVendingMachine, "_assume", fake_assume), \
     patch.object(MemoryClient, "list_events", fake_list_events):
    try:
        mgr = MultiTenantAgentCoreMemorySessionManager(
            memory_id="mem-test-000",
            tvm_role_arn="arn:aws:iam::123456789012:role/TestTvmRole",
            region_name="us-east-1",
        )
        agent = Agent(
            model=BedrockModel(model_id="amazon.nova-pro-v1:0", region_name="us-east-1"),
            session_manager=mgr,
            callback_handler=None,
        )
    except Exception as e:  # construction must succeed under ABAC
        print(f"FAIL: construction failed: {type(e).__name__}: {e}")
        sys.exit(1)

    mgr._prepare(agent, {"tenantId": "tenant-001"}, "user-1", "conv-1")
    agent.state.set("secret", "tenant-001 data")
    mgr._prepare(agent, {"tenantId": "tenant-002"}, "user-1", "conv-1")

    untagged = [svc for tag, svc in memory_clients if tag is None]
    if untagged:
        problems.append(f"memory clients built from default credentials: {untagged}")
    if not any(tag == BOOTSTRAP_TENANT_ID for tag, _ in memory_clients):
        problems.append("start-up placeholder did not use the bootstrap TVM session")
    if denied:
        problems.append(f"calls IAM would deny: {denied}")
    if agent.state.get("secret") is not None:
        problems.append("agent.state from tenant-001 reached tenant-002")
    if not mgr.config.actor_id.startswith("tenant-002:"):
        problems.append(f"unexpected actor id {mgr.config.actor_id}")
    for name in ("_held_records", "_latest_agent_message", "_last_synced_internal_state",
                 "_fix_broken_tool_use", "_agent_created_at_cache"):
        if not hasattr(mgr, name):
            problems.append(f"SDK no longer has {name}")

if problems:
    for p in problems:
        print(f"FAIL: {p}")
    sys.exit(1)
print("OK")
