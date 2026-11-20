# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
multi_tenant_agent.py — Multi-tenant agent with tenant-isolated Bedrock
AgentCore Memory, deployed on Amazon Bedrock AgentCore Runtime.

Multiple tenants share a single AgentCore Memory resource. Isolation is
enforced in two places:
  Application : actor_id embeds tenantId
  IAM         : memory calls are signed with TVM credentials, and the TVM role
                condition bedrock-agentcore:actorId = "${aws:PrincipalTag/TenantID}:*"
                makes AWS deny any other tenant's actor

Both take the tenant from the same JWT claim, so the claim must be one the user
cannot change: setup_cognito.sh creates custom:tenant_id as an immutable
attribute that the app client cannot write.

The MultiTenantAgentCoreMemorySessionManager registers a BeforeInvocationEvent
hook that reads tenant identity from invocation_state and updates the config and
boto3 clients before each request — no separate plugin is needed.

Tenant identity is extracted from the JWT bearer token injected by AgentCore
Runtime's built-in JWT authorizer (configured at deploy time with Cognito).

HTTP contract (required by AgentCore Runtime):
  GET  /ping         — health check
  POST /invocations  — agent call, payload: {"message": str, "conversation_id": str}

Env vars: AGENTCORE_MEMORY_ID, AGENTCORE_TVM_ROLE_ARN, AWS_REGION, BEDROCK_MODEL_ID

Local test (NOT for anything but your own machine: run locally, nothing
verifies the JWT, so any hand-made token picks any tenant):
  pip install -e ".[agentcore]"
  AGENTCORE_MEMORY_ID=... AGENTCORE_TVM_ROLE_ARN=... \
    python3 examples/multi_tenant_agent.py

Deploy:
  See README.md — "Run the examples → Deploy to AgentCore Runtime"
"""

import hashlib
import logging
import os
import sys

# Ensure the library src/ is on the path when running inside AgentCore Runtime
# (the toolkit packages the repo root, so src/ is at /var/task/src/).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from agentcore_memory import IsolationError, MultiTenantAgentCoreMemorySessionManager
from agentcore_memory.identity import extract_identity

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


def _conversation_from_session(runtime_session_id) -> str:
    """
    Default conversation id when the payload has none: a short digest of the
    runtime session id (33-64 characters), so the memory session id stays
    within the AgentCore limit of 100 characters.
    """
    if not runtime_session_id:
        return "default"
    return "rs-" + hashlib.sha256(runtime_session_id.encode()).hexdigest()[:16]


@app.entrypoint
def invoke(payload: dict, context) -> dict:
    """Called by AgentCore Runtime for every /invocations request."""
    try:
        identity = extract_identity(context.request_headers)
        if not identity:
            return {"error": "JWT missing custom:tenant_id or sub claim"}

        tenant_id       = identity["tenant_context"]["tenantId"]
        conversation_id = payload.get("conversation_id") or _conversation_from_session(context.session_id)
        message         = payload.get("message", "")

        response = _agent(
            message,
            invocation_state={
                **identity,
                "conversation_id": conversation_id,
            },
        )
        response_str = str(response)

        # Log identifiers and sizes only — never conversation content.
        logger.info(
            "[request] tenant=%s conv=%s user=%s message_chars=%d response_chars=%d",
            tenant_id, conversation_id, identity["user_id"],
            len(message), len(response_str),
        )

        return {
            "response":        response_str,
            "tenant_id":       tenant_id,
            "conversation_id": conversation_id,
        }
    except IsolationError as e:
        # Bad identifiers, a TVM failure, or a memory read error.
        logger.warning("invoke rejected: %s", e)
        return {"error": "request rejected: invalid identity, or memory or credentials unavailable"}
    except Exception as e:
        # Full details go to server-side logs only. Exception messages can
        # contain internal identifiers (role ARNs, tenant ids, etc. — see
        # IsolationError in token_vending_machine.py), so the HTTP response
        # returns a generic error rather than str(e).
        logger.exception("invoke error: %s", type(e).__name__)
        return {"error": "internal error processing request"}


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", "8080")))
