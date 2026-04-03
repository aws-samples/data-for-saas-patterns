"""
multi_agent_orchestrator.py — Example: multi-agent memory isolation

Demonstrates how two agents (orchestrator + researcher) share the same
S3VectorMemoryPlugin but maintain isolated memory namespaces via agent.name.

Each agent only retrieves its own memories by default. A supervisor can
read across all agents by calling store.retrieve_memories with agent_name=None.

Install the library first:
    pip install strands-s3-vectors-memory

Env vars: S3_VECTOR_BUCKET_NAME, S3_VECTOR_TVM_ROLE_ARN, AWS_REGION, BEDROCK_MODEL_ID

Index setup (run once before first use):
    aws s3vectors create-index \\
      --vector-bucket-name $S3_VECTOR_BUCKET_NAME \\
      --index-name memory-tenant-001 \\
      --data-type float32 --dimension 1024 --distance-metric cosine \\
      --metadata-configuration '{"nonFilterableMetadataKeys":["content","stored_at","conversation_id","type"]}' \\
      --region $AWS_REGION
"""

import os
import time

from strands import Agent
from strands.models import BedrockModel

from strands_s3_vectors_memory import MultiTenantS3VectorMemory, S3VectorMemoryPlugin

BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses."""

TENANT_CONTEXT = {"tenantId": "tenant-001"}
USER_ID = "user-multi-agent-demo"
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

# ---------------------------------------------------------------------------
# Shared store and plugin — both agents use the same plugin instance.
# agent.name is the only thing that separates their memory namespaces.
# ---------------------------------------------------------------------------
store = MultiTenantS3VectorMemory(
    bucket_name  = os.environ["S3_VECTOR_BUCKET_NAME"],
    tvm_role_arn = os.environ["S3_VECTOR_TVM_ROLE_ARN"],
)
plugin = S3VectorMemoryPlugin(store=store, base_prompt=BASE_PROMPT)

orchestrator = Agent(
    model            = BedrockModel(model_id=MODEL_ID),
    name             = "orchestrator",   # required — unique per agent role
    system_prompt    = BASE_PROMPT,
    tools            = [plugin.memory_tool],
    plugins          = [plugin],
    callback_handler = None,
)

researcher = Agent(
    model            = BedrockModel(model_id=MODEL_ID),
    name             = "researcher",     # different namespace from orchestrator
    system_prompt    = BASE_PROMPT,
    tools            = [plugin.memory_tool],
    plugins          = [plugin],
    callback_handler = None,
)


def _turn(agent, agent_label, message, conv_id, end_session=False):
    response = agent(message, invocation_state={
        "tenant_context":  TENANT_CONTEXT,
        "user_id":         USER_ID,
        "conversation_id": conv_id,
        "end_session":     end_session,
    })
    print(f"\n  [{agent_label}] USER: {message}")
    print(f"  [{agent_label}] AGENT: {response}")
    return str(response)


if __name__ == "__main__":
    print("=" * 60)
    print("STEP 1 — orchestrator stores a decision")
    print("=" * 60)

    _turn(orchestrator, "orchestrator",
          "We have decided to use Python for the backend.",
          conv_id="orch-conv-001")
    _turn(orchestrator, "orchestrator",
          "Got it, thanks.",
          conv_id="orch-conv-001", end_session=True)

    print("\n[waiting 5s for background summary store to complete...]")
    time.sleep(5)

    print("\n" + "=" * 60)
    print("STEP 2 — researcher stores a finding")
    print("=" * 60)

    _turn(researcher, "researcher",
          "Research finding: Python has the best library ecosystem for our use case.",
          conv_id="res-conv-001")
    _turn(researcher, "researcher",
          "Thanks, wrapping up.",
          conv_id="res-conv-001", end_session=True)

    print("\n[waiting 5s for background summary store to complete...]")
    time.sleep(5)

    print("\n" + "=" * 60)
    print("STEP 3 — isolation check: researcher cannot see orchestrator's memories")
    print("  Expected: researcher has NO memory of the Python decision.")
    print("=" * 60)

    _turn(researcher, "researcher",
          "What programming language decision was made?",
          conv_id="res-conv-002")

    print("\n  👆 Researcher should not know about the orchestrator's Python decision.")
    print("     If it mentions 'Python backend decision', isolation has failed.")

    print("\n" + "=" * 60)
    print("STEP 4 — orchestrator recalls its own memory")
    print("  Expected: orchestrator remembers the Python decision.")
    print("=" * 60)

    _turn(orchestrator, "orchestrator",
          "What was the backend language decision we made?",
          conv_id="orch-conv-002")

    print("\n" + "=" * 60)
    print("STEP 5 — cross-agent access: supervisor reads all memories")
    print("  Calling store.retrieve_memories with agent_name=None.")
    print("=" * 60)

    all_memories = store.retrieve_memories(
        user_id        = USER_ID,
        query          = "Python programming language",
        tenant_context = TENANT_CONTEXT,
        agent_name     = None,   # no agent filter — returns all agents' memories
    )
    print(f"\n  Cross-agent retrieval returned {len(all_memories)} result(s):")
    for m in all_memories:
        print(f"    - [{m['stored_at']}] (similarity={m['similarity']}) {m['content'][:80]}")
