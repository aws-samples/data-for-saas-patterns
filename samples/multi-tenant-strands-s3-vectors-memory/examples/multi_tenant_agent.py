"""
multi_tenant_agent.py — Multi-tenant agent with S3 Vector long-term memory,
deployed on Amazon Bedrock AgentCore Runtime.

AgentCore Runtime provides session isolation at the infrastructure level:
each runtimeSessionId is routed to a dedicated microVM that persists for up
to 8 hours. All turns of a conversation arrive at the SAME microVM, so
agent.messages is maintained in memory across turns automatically.

No S3SessionManager needed — the microVM IS the session store.
The plugin uses len(agent.messages) == 0 to detect the first turn of a
conversation (fresh microVM) and retrieves long-term memories from S3 Vectors.

Tenant identity is extracted from the JWT bearer token injected by AgentCore
Runtime's built-in JWT authorizer (configured at deploy time with Cognito).

HTTP contract (required by AgentCore Runtime):
  GET  /ping         — health check, returns {"status": "Healthy"}
  POST /invocations  — agent call, payload: {"prompt": str, "end_session": bool}

AgentCore Runtime injects:
  runtimeSessionId  → used as conversation_id  (via X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header)
  Authorization     → JWT bearer token → tenant_id, user_id

Env vars: S3_VECTOR_BUCKET_NAME, S3_VECTOR_TVM_ROLE_ARN, AWS_REGION, BEDROCK_MODEL_ID

Local test:
  pip install bedrock-agentcore-starter-toolkit
  S3_VECTOR_BUCKET_NAME=... S3_VECTOR_TVM_ROLE_ARN=... \
    python3 multi_tenant_agent.py

Deploy:
  See README.md — "Run the examples → Deploy to AgentCore Runtime"
"""

import base64
import json
import logging
import os
import sys

# Ensure the library src/ is on the path when running inside AgentCore Runtime
# (the toolkit packages the repo root, so src/ is at /var/task/src/)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from strands_s3_vectors_memory import MultiTenantS3VectorMemory, S3VectorMemoryPlugin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses without explicitly announcing
that you are recalling past information, unless the user asks."""

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# Singletons — AgentCore Runtime routes all turns of a conversation to the
# same microVM, so agent.messages persists in memory across turns.
# No S3SessionManager needed — the microVM IS the session store.
# ---------------------------------------------------------------------------
_store  = MultiTenantS3VectorMemory(
    bucket_name  = os.environ["S3_VECTOR_BUCKET_NAME"],
    tvm_role_arn = os.environ.get("S3_VECTOR_TVM_ROLE_ARN"),
)
_plugin = S3VectorMemoryPlugin(store=_store, base_prompt=BASE_PROMPT)
_agent  = Agent(
    model            = BedrockModel(model_id=os.environ.get("BEDROCK_MODEL_ID",
                                   "us.anthropic.claude-sonnet-4-5-20250929-v1:0")),
    name             = "assistant",
    plugins          = [_plugin],
    tools            = [_plugin.memory_tool],
    system_prompt    = BASE_PROMPT,
    callback_handler = None,
)


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload — signature already verified by AgentCore Runtime."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# AgentCore Runtime app
# ---------------------------------------------------------------------------
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context) -> dict:
    """
    Called by AgentCore Runtime for every /invocations request.

    context.session_id       -- runtimeSessionId, scoped to this conversation.
                                All turns arrive at the same microVM so
                                agent.messages is already populated on turns 2+.
    context.request_headers  -- forwarded HTTP headers including Authorization JWT.
    """
    conversation_id = context.session_id or "default"
    headers         = context.request_headers or {}
    auth_header     = headers.get("Authorization") or headers.get("authorization") or ""
    message         = payload.get("prompt", "")
    end_session     = payload.get("end_session", False)

    # Extract tenant identity from the JWT (already verified by AgentCore Runtime)
    claims    = _decode_jwt_payload(auth_header.removeprefix("Bearer "))
    tenant_id = claims.get("custom:tenant_id", "")
    user_id   = claims.get("sub", conversation_id)

    if not tenant_id:
        return {"error": "JWT missing custom:tenant_id claim"}

    tenant_context = {
        "tenantId":   tenant_id,
        "tenantName": claims.get("custom:tenant_name", ""),
        "tier":       claims.get("custom:tier", "standard"),
        "status":     "active",
        "features":   [],
        "limits":     {},
    }

    # Detect conversation change — on AgentCore Runtime this never fires because
    # each conversation gets its own microVM. Needed for local testing only.
    if getattr(_agent, "_current_conv_id", None) != conversation_id:
        _agent.messages = []
        _agent._current_conv_id = conversation_id

    response     = _agent(message, invocation_state={
        "tenant_context":  tenant_context,
        "user_id":         user_id,
        "conversation_id": conversation_id,
        "end_session":     end_session,
    })
    response_str = str(response)

    logger.info(
        "[request] tenant=%s conv=%s user=%s end_session=%s\n  USER : %s\n  AGENT: %s",
        tenant_id, conversation_id, user_id, end_session,
        message[:200], response_str[:200],
    )

    return {
        "response":        response_str,
        "tenant_id":       tenant_id,
        "conversation_id": conversation_id,
    }


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", "8080")))
