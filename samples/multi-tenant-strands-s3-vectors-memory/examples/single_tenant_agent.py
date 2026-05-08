"""
single_tenant_agent.py — Single-tenant agent with S3 Vector long-term memory,
deployed on Amazon Bedrock AgentCore Runtime.

AgentCore Runtime provides session isolation at the infrastructure level:
each runtimeSessionId is routed to a dedicated microVM that persists for up
to 8 hours. All turns of a conversation arrive at the SAME microVM, so
agent.messages is maintained in memory across turns automatically.

No S3SessionManager is needed — the microVM IS the session store.
The plugin uses len(agent.messages) == 0 to detect the first turn of a
conversation (fresh microVM) and retrieves long-term memories from S3 Vectors.

HTTP contract (required by AgentCore Runtime):
  GET  /ping         — health check, returns {"status": "Healthy"}
  POST /invocations  — agent call, payload: {"prompt": str, "end_session": bool}

AgentCore Runtime injects:
  runtimeSessionId  → used as conversation_id  (via X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header)

Env vars: S3_VECTOR_BUCKET_NAME, AWS_REGION, BEDROCK_MODEL_ID

Local test:
  pip install bedrock-agentcore-starter-toolkit
  S3_VECTOR_BUCKET_NAME=... python3 single_tenant_agent.py

Deploy:
  See README.md — "Run the examples → Deploy to AgentCore Runtime"
"""

import logging
import os
import sys

# Ensure the library src/ is on the path when running inside AgentCore Runtime
# (the toolkit packages the repo root, so src/ is at /var/task/src/)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

from strands_s3_vectors_memory import S3VectorMemory, S3VectorMemoryPlugin

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
_store  = S3VectorMemory(bucket_name=os.environ["S3_VECTOR_BUCKET_NAME"])
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

# ---------------------------------------------------------------------------
# AgentCore Runtime app — wraps the agent in the required HTTP server
# ---------------------------------------------------------------------------
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context) -> dict:
    """
    Called by AgentCore Runtime for every /invocations request.

    context.session_id  -- runtimeSessionId, scoped to this conversation by
                           AgentCore Runtime's session isolation infrastructure.
                           All turns of the same conversation arrive at this
                           same microVM, so agent.messages is already populated
                           on turns 2+.

    For local testing (single process serving multiple conversations), we detect
    conversation changes and reset agent.messages to simulate microVM isolation.
    """
    conversation_id = context.session_id or "default"
    user_id         = payload.get("user_id", conversation_id)
    message         = payload.get("prompt", "")
    end_session     = payload.get("end_session", False)

    # Detect conversation change — on AgentCore Runtime this never fires because
    # each conversation gets its own microVM. Needed for local testing only.
    if getattr(_agent, "_current_conv_id", None) != conversation_id:
        _agent.messages = []
        _agent._current_conv_id = conversation_id

    response     = _agent(message, invocation_state={
        "user_id":         user_id,
        "conversation_id": conversation_id,
        "end_session":     end_session,
    })
    response_str = str(response)

    logger.info(
        "[request] conv=%s user=%s end_session=%s\n  USER : %s\n  AGENT: %s",
        conversation_id, user_id, end_session,
        message[:200], response_str[:200],
    )

    return {"response": response_str}


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", "8080")))
