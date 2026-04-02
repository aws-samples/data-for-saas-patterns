"""
multi_tenant_agent.py — Example: multi-tenant agent with S3 Vector long-term memory

One index per tenant. TVM credentials scoped per tenant via STS AssumeRole.
Identity extracted from a JWT Authorization header.

Install the library first:
    pip install strands-s3-vectors-memory

Env vars: S3_VECTOR_BUCKET_NAME, S3_VECTOR_TVM_ROLE_ARN, AWS_REGION, BEDROCK_MODEL_ID
"""

import base64
import json
import logging
import os
import uuid

from strands import Agent
from strands.models import BedrockModel

from strands_s3_vectors_memory import MultiTenantS3VectorMemory, S3VectorMemoryPlugin

logger = logging.getLogger(__name__)

# {memory_context} is filled by the plugin on the first turn of each conversation.
# If no memories are found, the placeholder is replaced with an empty string.
BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses without explicitly announcing
that you are recalling past information, unless the user asks."""

_store  = MultiTenantS3VectorMemory(
    bucket_name  = os.environ["S3_VECTOR_BUCKET_NAME"],
    tvm_role_arn = os.environ.get("S3_VECTOR_TVM_ROLE_ARN"),
)
_plugin = S3VectorMemoryPlugin(store=_store, base_prompt=BASE_PROMPT)
_agent  = Agent(
    model            = BedrockModel(model_id=os.environ.get("BEDROCK_MODEL_ID",
                                   "us.anthropic.claude-sonnet-4-5-20250929-v1:0")),
    system_prompt    = BASE_PROMPT,
    tools            = [_plugin.memory_tool],  # mid-turn recall on demand
    plugins          = [_plugin],
    callback_handler = None,   # suppress streaming output — we print ourselves
)


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload (signature already verified upstream).

    In ECS deployments, the API Gateway Lambda authorizer validates the token
    before the request reaches this function. In AgentCore Runtime deployments,
    AgentCore Runtime performs JWT validation. This function only decodes the
    already-verified payload — it does not need to re-verify the signature.
    """
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _extract_tenant_identity(auth_header: str):
    """Returns (tenant_context, user_id) or (None, None) on failure."""
    claims    = _decode_jwt_payload(auth_header.removeprefix("Bearer "))
    tenant_id = claims.get("custom:tenant_id", "")
    if not tenant_id:
        return None, None
    tenant_context = {
        "tenantId":   tenant_id,
        "tenantName": claims.get("custom:tenant_name", ""),
        "tier":       claims.get("custom:tier", "standard"),
        "status":     "active",
        "features":   [],
        "limits":     {},
    }
    return tenant_context, claims.get("sub", "unknown")


def invoke(payload: dict, auth_header: str = "", session_id: str = "") -> dict:
    """
    Process a single request turn.

    Args:
        payload:     Request body — keys: message (str), end_session (bool).
        auth_header: Authorization header, e.g. "Bearer <jwt>".
        session_id:  Session ID from infrastructure. Used as conversation_id.

    Returns:
        dict with 'response', 'tenant_id', and 'conversation_id'.
    """
    tenant_context, user_id = _extract_tenant_identity(auth_header)
    if tenant_context is None:
        return {"error": "JWT required — custom:tenant_id claim missing"}

    conversation_id = session_id or payload.get("conversation_id") or str(uuid.uuid4())

    response = _agent(payload.get("message", ""), invocation_state={
        "tenant_context":  tenant_context,
        "user_id":         user_id,
        "conversation_id": conversation_id,
        "end_session":     payload.get("end_session", False),
    })

    return {
        "response":        str(response),
        "tenant_id":       tenant_context["tenantId"],
        "conversation_id": conversation_id,
    }


def _print_turn(label, message, response):
    print(f"\n  {label}: {message}")
    print(f"  AGENT: {response}")


if __name__ == "__main__":
    import time
    tenant_id = "tenant-001"
    user_id   = "user-001"

    def _dev_invoke(message, tenant_id, user_id, session_id, end_session=False):
        """Dev-only helper that bypasses JWT verification for local testing."""
        tenant_context = {
            "tenantId":   tenant_id,
            "tenantName": "",
            "tier":       "standard",
            "status":     "active",
            "features":   [],
            "limits":     {},
        }
        response = _agent(message, invocation_state={
            "tenant_context":  tenant_context,
            "user_id":         user_id,
            "conversation_id": session_id,
            "end_session":     end_session,
        })
        return {"response": str(response), "tenant_id": tenant_id, "conversation_id": session_id}

    print("=" * 60)
    print(f"SESSION 1 — tenant={tenant_id}  storing a fact")
    print("=" * 60)

    msg1 = "Our Q4 budget is $2M and it is confidential."
    r1 = _dev_invoke(msg1, tenant_id, user_id, "conv-001")
    _print_turn("USER", msg1, r1["response"])

    msg2 = "Got it, thanks."
    r2 = _dev_invoke(msg2, tenant_id, user_id, "conv-001", end_session=True)
    _print_turn("USER", f"{msg2} [end_session=True]", r2["response"])

    print("\n[waiting 5s for background summary store to complete...]")
    time.sleep(5)

    print("\n" + "=" * 60)
    print(f"SESSION 2 — tenant={tenant_id}  memory should be recalled")
    print("=" * 60)

    msg3 = "What did I tell you about our budget?"
    r3 = _dev_invoke(msg3, tenant_id, user_id, "conv-002")
    _print_turn("USER", msg3, r3["response"])

    # -----------------------------------------------------------------------
    # Tenant isolation demo — tenant-002 asks the same question.
    # It has its own index (memory-tenant-002) with no data in it.
    # TVM credentials for tenant-002 are physically blocked from reading
    # tenant-001's index by IAM ABAC — the agent has no memory to inject.
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SESSION 3 — tenant=tenant-002  isolation check")
    print("  tenant-002 asks the same question about the budget.")
    print("  Expected: agent has NO memory — cannot see tenant-001's data.")
    print("=" * 60)

    r4 = _dev_invoke(msg3, "tenant-002", "user-002", "conv-003")
    _print_turn("USER (tenant-002)", msg3, r4["response"])
    print("\n  👆 Review the response above — tenant-002 should have no knowledge of")
    print("     tenant-001's Q4 budget. If the response mentions '$2M', isolation has failed.")

    print("\n" + "=" * 60)
    print(f"SESSION 4 — tenant={tenant_id}  memory_tool: mid-turn recall on demand")
    print("  The agent uses the memory_tool when it discovers mid-reasoning")
    print("  that it needs a specific fact from a previous session.")
    print("=" * 60)

    msg5 = ("We're planning Q1 now. Can you remind me what our Q4 budget was "
            "and whether there were any constraints I mentioned?")
    r5 = _dev_invoke(msg5, tenant_id, user_id, "conv-004")
    _print_turn("USER", msg5, r5["response"])
