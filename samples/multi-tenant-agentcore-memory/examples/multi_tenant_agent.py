"""
multi_tenant_agent.py — Multi-tenant agent with tenant-isolated Bedrock
AgentCore Memory, deployed on Amazon Bedrock AgentCore Runtime.

Multiple tenants share a single AgentCore Memory resource, but isolation is
enforced at two independent layers:
  Application : actor_id embeds tenantId
  IAM         : the TVM role condition
                bedrock-agentcore:actorId = "${aws:PrincipalTag/TenantID}:*"
                enforces it at the AWS control plane

The MultiTenantAgentCoreMemorySessionManager registers a BeforeInvocationEvent
hook that reads tenant identity from invocation_state and updates the config and
boto3 clients before each request — no separate plugin is needed.

Tenant identity is extracted from the JWT bearer token injected by AgentCore
Runtime's built-in JWT authorizer (configured at deploy time with Cognito).

HTTP contract (required by AgentCore Runtime):
  GET  /ping         — health check
  POST /invocations  — agent call, payload: {"message": str, "conversation_id": str}

Env vars: AGENTCORE_MEMORY_ID, AGENTCORE_TVM_ROLE_ARN, AWS_REGION, BEDROCK_MODEL_ID

Local test:
  pip install -e ".[agentcore]"
  AGENTCORE_MEMORY_ID=... AGENTCORE_TVM_ROLE_ARN=... \
    python3 examples/multi_tenant_agent.py

Deploy:
  See README.md — "Run the examples → Deploy to AgentCore Runtime"
"""

import base64
import json
import logging
import os
import sys

# Ensure the library src/ is on the path when running inside AgentCore Runtime
# (the toolkit packages the repo root, so src/ is at /var/task/src/).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from agentcore_memory import MultiTenantAgentCoreMemorySessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# Singletons — created once during container startup, not per handler call.
# The session manager registers a BeforeInvocationEvent hook that rebuilds
# tenant-scoped actor_id/session_id and swaps boto3 clients on every turn.
# ---------------------------------------------------------------------------
_session_manager = MultiTenantAgentCoreMemorySessionManager(
    memory_id    = os.environ["AGENTCORE_MEMORY_ID"],
    tvm_role_arn = os.environ.get("AGENTCORE_TVM_ROLE_ARN"),
    region_name  = AWS_REGION,
)
_model = BedrockModel(
    model_id    = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0"),
    region_name = AWS_REGION,
)
_agent = Agent(
    model           = _model,
    system_prompt   = "You are a helpful assistant.",
    session_manager = _session_manager,
    callback_handler = None,
)

app = BedrockAgentCoreApp()


@app.ping
def health_check():
    from bedrock_agentcore.runtime import PingStatus
    return PingStatus.HEALTHY


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload — signature already verified by AgentCore Runtime."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _extract_identity(context) -> dict | None:
    """
    Extract and validate tenant identity from the AgentCore request context.

    Returns a dict with tenant_context + user_id, or None if the JWT is missing
    or the tenant_id claim is absent. user_id is always taken from the JWT `sub`
    claim — never overridable by the request payload.
    """
    headers     = context.request_headers or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    claims      = _decode_jwt_payload(auth_header.removeprefix("Bearer "))
    tenant_id   = claims.get("custom:tenant_id", "")
    if not tenant_id:
        return None
    return {
        "tenant_context": {
            "tenantId":   tenant_id,
            "tenantName": claims.get("custom:tenant_name", ""),
            "tier":       claims.get("custom:tier", "standard"),
        },
        "user_id": claims.get("sub", "unknown"),
    }


@app.entrypoint
def invoke(payload: dict, context) -> dict:
    """Called by AgentCore Runtime for every /invocations request."""
    try:
        identity = _extract_identity(context)
        if not identity:
            return {"error": "JWT missing custom:tenant_id claim"}

        tenant_id       = identity["tenant_context"]["tenantId"]
        conversation_id = payload.get("conversation_id") or context.session_id or "default"
        message         = payload.get("message", "")

        response = _agent(
            message,
            invocation_state={
                **identity,
                "conversation_id": conversation_id,
            },
        )
        response_str = str(response)

        logger.info(
            "[request] tenant=%s conv=%s user=%s\n  USER : %s\n  AGENT: %s",
            tenant_id, conversation_id, identity["user_id"],
            message[:200], response_str[:200],
        )

        return {
            "response":        response_str,
            "tenant_id":       tenant_id,
            "conversation_id": conversation_id,
        }
    except Exception as e:
        import traceback
        # Full details go to server-side logs only. Exception messages can
        # contain internal identifiers (role ARNs, tenant ids, etc. — see
        # IsolationError in token_vending_machine.py), so the HTTP response
        # returns a generic error rather than str(e).
        logger.error("invoke error: %s: %s", type(e).__name__, e)
        logger.error(traceback.format_exc())
        return {"error": "internal error processing request"}


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", "8080")))
