"""
single_tenant_agent.py — Example: single-tenant agent with S3 Vector long-term memory

No JWT, no TVM role. One shared index, ambient AWS credentials.
Identity passed directly via invoke().

Install the library first:
    pip install strands-s3-vectors-memory

Env vars: S3_VECTOR_BUCKET_NAME, AWS_REGION, BEDROCK_MODEL_ID
"""

import os

from strands import Agent
from strands.models import BedrockModel

from strands_s3_vectors_memory import S3VectorMemory, S3VectorMemoryPlugin

# {memory_context} is filled by the plugin on the first turn of each conversation.
# If no memories are found, the placeholder is replaced with an empty string.
BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses without explicitly announcing
that you are recalling past information, unless the user asks."""

_store  = S3VectorMemory(bucket_name=os.environ["S3_VECTOR_BUCKET_NAME"])
_plugin = S3VectorMemoryPlugin(store=_store, base_prompt=BASE_PROMPT)
_agent  = Agent(
    model            = BedrockModel(model_id=os.environ.get("BEDROCK_MODEL_ID",
                                   "us.anthropic.claude-sonnet-4-5-20250929-v1:0")),
    name             = "assistant",
    system_prompt    = BASE_PROMPT,
    tools            = [_plugin.memory_tool],  # mid-turn recall on demand
    plugins          = [_plugin],
    callback_handler = None,   # suppress streaming output — we print ourselves
)


def invoke(user_id: str, conversation_id: str, message: str,
           end_session: bool = False) -> str:
    """
    Process a single request.

    Args:
        user_id:         User identifier.
        conversation_id: Unique conversation ID.
        message:         The user's message.
        end_session:     If True, summarize and store the conversation after response.

    Returns:
        The agent's response as a string.
    """
    return str(_agent(message, invocation_state={
        "user_id":         user_id,
        "conversation_id": conversation_id,
        "end_session":     end_session,
    }))


def _turn(user_id, conv_id, message, end_session=False):
    """Print a labelled turn and return the response."""
    print(f"\n  USER: {message}")
    response = invoke(user_id, conv_id, message, end_session=end_session)
    print(f" AGENT: {response}")
    if end_session:
        print("        [end_session=True — summarizing in background]")
    return response


if __name__ == "__main__":
    import time
    user_id = "user-001"

    print("=" * 60)
    print("SESSION 1 — storing a fact")
    print("=" * 60)
    _turn(user_id, "conv-001", "My favourite framework is Strands Agents.")
    _turn(user_id, "conv-001", "What framework did I mention?", end_session=True)

    print("\n[waiting 5s for background summary store to complete...]")
    time.sleep(5)

    print("\n" + "=" * 60)
    print("SESSION 2 — memory injected automatically on first turn")
    print("=" * 60)
    _turn(user_id, "conv-002", "What do you know about my preferences?")

    print("\n" + "=" * 60)
    print("SESSION 3 — memory_tool: mid-turn recall on demand")
    print("  The agent uses the memory_tool when it needs to recall")
    print("  something specific mid-conversation.")
    print("=" * 60)
    _turn(user_id, "conv-003",
          "I'm evaluating some new tools. By the way, remind me — "
          "what framework did I mention I liked in a previous session?")
