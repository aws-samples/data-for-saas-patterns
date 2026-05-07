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
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from strands import Agent
from strands.models import BedrockModel

from strands_s3_vectors_memory import MultiTenantS3VectorMemory, S3VectorMemoryPlugin

BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses."""

TENANT_CONTEXT = {"tenantId": "tenant-001"}
USER_ID        = "user-multi-agent-demo"
MODEL_ID       = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
AWS_REGION     = os.environ.get("AWS_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# Shared store and plugin — both agents use the same plugin instance.
# agent.name is the only thing that separates their memory namespaces.
# No S3SessionManager needed — this example runs as a script where each
# agent is a singleton and agent.messages persists in-process.
# ---------------------------------------------------------------------------
store = MultiTenantS3VectorMemory(
    bucket_name  = os.environ["S3_VECTOR_BUCKET_NAME"],
    tvm_role_arn = os.environ["S3_VECTOR_TVM_ROLE_ARN"],
)
plugin = S3VectorMemoryPlugin(store=store, base_prompt=BASE_PROMPT)

orchestrator = Agent(
    model            = BedrockModel(model_id=MODEL_ID),
    name             = "orchestrator",   # required — unique per agent role
    plugins          = [plugin],
    tools            = [plugin.memory_tool],
    system_prompt    = BASE_PROMPT,
    callback_handler = None,
)

researcher = Agent(
    model            = BedrockModel(model_id=MODEL_ID),
    name             = "researcher",     # different namespace from orchestrator
    plugins          = [plugin],
    tools            = [plugin.memory_tool],
    system_prompt    = BASE_PROMPT,
    callback_handler = None,
)


def _turn(agent, agent_label, message, conv_id, end_session=False):
    # Detect conversation change and reset messages (simulates microVM isolation)
    if getattr(agent, "_current_conv_id", None) != conv_id:
        agent.messages = []
        agent._current_conv_id = conv_id

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
    # -----------------------------------------------------------------------
    # STEP 1 — orchestrator stores a budget decision (finance domain)
    # -----------------------------------------------------------------------
    print("=" * 60)
    print("STEP 1 — orchestrator stores a budget decision")
    print("=" * 60)

    _turn(orchestrator, "orchestrator",
          "The Q4 budget has been approved at $2M. This is confidential.",
          conv_id="orch-conv-001")
    _turn(orchestrator, "orchestrator",
          "Got it, thanks.",
          conv_id="orch-conv-001", end_session=True)

    print("\n[waiting 5s for background summary store to complete...]")
    time.sleep(5)

    # -----------------------------------------------------------------------
    # STEP 2 — researcher stores a finding about a completely different topic
    # (marine biology — no semantic overlap with budget/finance)
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("STEP 2 — researcher stores an unrelated finding (marine biology)")
    print("=" * 60)

    _turn(researcher, "researcher",
          "Research finding: coral reef bleaching has accelerated by 40% since 2020.",
          conv_id="res-conv-001")
    _turn(researcher, "researcher",
          "Thanks, wrapping up.",
          conv_id="res-conv-001", end_session=True)

    print("\n[waiting 5s for background summary store to complete...]")
    time.sleep(5)

    # -----------------------------------------------------------------------
    # STEP 3 — isolation check: researcher queries about the budget
    # It has NO memory of the orchestrator's budget decision.
    # Expected: agent says it has no information about a budget.
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("STEP 3 — isolation check: researcher queries the budget decision")
    print("  Expected: researcher has NO memory of the $2M budget.")
    print("  If it mentions '$2M' or 'Q4 budget', isolation has FAILED.")
    print("=" * 60)

    _turn(researcher, "researcher",
          "What budget was approved for Q4?",
          conv_id="res-conv-002")

    print("\n  👆 Researcher should say it has no information about a Q4 budget.")

    # -----------------------------------------------------------------------
    # STEP 4 — orchestrator recalls its own budget decision
    # Expected: orchestrator remembers the $2M budget.
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("STEP 4 — orchestrator recalls its own budget decision")
    print("  Expected: orchestrator remembers the $2M Q4 budget.")
    print("=" * 60)

    _turn(orchestrator, "orchestrator",
          "What budget was approved for Q4?",
          conv_id="orch-conv-002")

    # -----------------------------------------------------------------------
    # STEP 5 — cross-agent access: supervisor reads all memories
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("STEP 5 — cross-agent access: supervisor reads all memories")
    print("  Calling store.retrieve_memories with agent_name=None.")
    print("  Expected: returns memories from BOTH orchestrator and researcher.")
    print("=" * 60)

    all_memories = store.retrieve_memories(
        user_id        = USER_ID,
        query          = "Q4 budget approval",
        tenant_context = TENANT_CONTEXT,
        agent_name     = None,   # no agent filter — returns all agents' memories
    )
    print(f"\n  Cross-agent retrieval returned {len(all_memories)} result(s):")
    for m in all_memories:
        print(f"    - [{m['stored_at']}] (similarity={m['similarity']}) {m['content'][:80]}")
