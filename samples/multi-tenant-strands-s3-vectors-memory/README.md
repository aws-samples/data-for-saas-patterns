# S3 Vector Memory Plugin for Strands Agents

A [Strands Plugin](https://strandsagents.com/docs/user-guide/concepts/plugins/) that gives any Strands Agent long-term semantic memory backed by [Amazon S3 Vectors](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html). At the end of a conversation, the plugin summarizes the full exchange using the agent's own model and stores the summary as a searchable vector. On subsequent conversations, relevant summaries are retrieved and injected into the system prompt before the LLM responds.

Available in two modes:

- **Single-tenant** — one shared index, no extra IAM setup
- **Multi-tenant** — one index per tenant, IAM credentials scoped per tenant via the [Token Vending Machine (TVM)](https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/implement-saas-tenant-isolation-for-amazon-s3-by-using-an-aws-lambda-token-vending-machine.html) pattern

![Architecture diagram](images/s3_vector_memory.png)

---

## Motivation

LLMs are stateless. Every conversation starts from scratch — the model has no memory of who the user is, what they've discussed before, or what decisions were made in past sessions. For most production agents, this is a serious limitation.

The standard workaround is to stuff conversation history into the context window. That works for a single session, but it doesn't scale:

- **Context windows are finite.** Long histories get truncated. Important context from weeks ago disappears.
- **Cost grows linearly.** Sending the full history on every turn means paying to re-process the same tokens repeatedly.
- **Cross-session recall is impossible.** When a user returns days later, the previous conversation is gone.

Vector databases solve the recall problem, but they introduce new complexity — especially in multi-tenant SaaS applications where tenant data must be strictly isolated. Most vector stores don't have native IAM-level isolation, so you end up building custom access control on top, which is error-prone and hard to audit.

This plugin solves all three problems:

1. **Persistent memory across sessions.** At the end of each conversation, the plugin summarizes the exchange and stores it as a vector. On the next conversation, relevant summaries are retrieved and injected into the system prompt — the agent "remembers" without bloating the context window.

2. **Semantic retrieval, not keyword search.** Memories are retrieved by meaning, not exact match. A user asking "what was my budget?" will surface a summary that mentions "Q4 spend" or "financial plan" — even if the words don't match.

3. **Tenant isolation enforced at the credential level.** In multi-tenant mode, each tenant gets a dedicated S3 Vectors index and STS credentials that are physically scoped to that index via IAM ABAC. A bug in application code that constructs the wrong index name is still blocked by IAM — there's no application-layer access control to misconfigure.

The result is an agent that gets smarter with every conversation, scales to millions of users, and keeps tenant data isolated by construction — not by convention.

---

## Repository structure

```
├── src/
│   └── strands_s3_vectors_memory/     # installable library
│       ├── __init__.py
│       ├── s3_vector_memory.py        # S3VectorMemory + MultiTenantS3VectorMemory
│       ├── s3_vector_memory_plugin.py # S3VectorMemoryPlugin (hook-driven)
│       └── token_vending_machine.py   # TVM credential manager (multi-tenant)
├── examples/
│   ├── single_tenant_agent.py         # Single-tenant runnable example
│   └── multi_tenant_agent.py          # Multi-tenant runnable example (TVM + isolation demo)
├── tests/
│   ├── unit/                          # 112 tests, no AWS credentials required
│   └── integration/                   # 28 tests, requires live AWS resources
├── scripts/
│   ├── setup_tvm_role.sh              # Creates the TVM IAM role with ABAC policy
│   ├── setup_cognito.sh               # Cognito user pool setup
│   └── setup_agentcore.sh             # AgentCore deployment setup
├── docs/
│   └── strands-s3-vector-memory-plugin.md  # Full plugin reference
├── pyproject.toml                     # Package build config
└── images/
    └── s3_vector_memory.png           # Architecture diagram
```

---

## Requirements

- Python 3.10+
- `strands-agents >= 1.0.0`
- `boto3 >= 1.35`
- `cachetools >= 5.0` (multi-tenant only)
- AWS account with S3 Vectors access and Bedrock Nova Embeddings enabled in your region
  - `amazon.nova-2-multimodal-embeddings-v1:0`
  - A Claude model (e.g. `us.anthropic.claude-sonnet-4-5-20250929-v1:0`)
- AWS CLI v2 configured with credentials that can access IAM, S3 Vectors, Bedrock, and STS

---

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## AWS setup

### 1. Create the S3 Vectors bucket

```bash
export AWS_REGION=us-east-1
export S3_VECTOR_BUCKET_NAME=my-vector-memory

aws s3vectors create-vector-bucket \
  --vector-bucket-name $S3_VECTOR_BUCKET_NAME \
  --region $AWS_REGION
```

### 2. Create the TVM IAM role (multi-tenant only)

```bash
bash scripts/setup_tvm_role.sh $S3_VECTOR_BUCKET_NAME
# Prints: export S3_VECTOR_TVM_ROLE_ARN=arn:aws:iam::<account-id>:role/...
export S3_VECTOR_TVM_ROLE_ARN=<printed-arn>
```

### 3. Create indexes

**Single-tenant** — one shared index named `memory`:

```bash
aws s3vectors create-index \
  --vector-bucket-name $S3_VECTOR_BUCKET_NAME \
  --index-name memory \
  --data-type float32 \
  --dimension 1024 \
  --distance-metric cosine \
  --metadata-configuration '{"nonFilterableMetadataKeys":["content","stored_at","conversation_id","type"]}' \
  --region $AWS_REGION
```

**Multi-tenant** — one index per tenant (repeat at onboarding time):

```bash
for TENANT in tenant-001 tenant-002; do
  aws s3vectors create-index \
    --vector-bucket-name $S3_VECTOR_BUCKET_NAME \
    --index-name memory-${TENANT} \
    --data-type float32 \
    --dimension 1024 \
    --distance-metric cosine \
    --metadata-configuration '{"nonFilterableMetadataKeys":["content","stored_at","conversation_id","type"]}' \
    --region $AWS_REGION
done
```

---

## Usage

### Single-tenant

```python
import os
from strands import Agent
from strands.models import BedrockModel
from strands_s3_vectors_memory import S3VectorMemory, S3VectorMemoryPlugin

BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses."""

store  = S3VectorMemory(bucket_name=os.environ["S3_VECTOR_BUCKET_NAME"])
plugin = S3VectorMemoryPlugin(store=store, base_prompt=BASE_PROMPT)
agent  = Agent(
    model   = BedrockModel(),
    tools   = [plugin.memory_tool],  # optional: mid-turn recall on demand
    plugins = [plugin],
    system_prompt = BASE_PROMPT,
)

# Turn 1 — agent responds; memory not yet stored
agent("My favourite framework is Strands Agents.", invocation_state={
    "user_id": "user-001", "conversation_id": "conv-001", "end_session": False,
})

# Turn 2 — end_session=True triggers background summarization and vector store
agent("Thanks, bye.", invocation_state={
    "user_id": "user-001", "conversation_id": "conv-001", "end_session": True,
})

# Next session — plugin retrieves the stored summary and injects it into the prompt
agent("What do you know about my preferences?", invocation_state={
    "user_id": "user-001", "conversation_id": "conv-002", "end_session": False,
})
```

`BASE_PROMPT` must contain a `{memory_context}` placeholder. The plugin fills it with retrieved conversation summaries on the first turn of each conversation, or replaces it with an empty string when no relevant memories are found.

### Multi-agent usage

When multiple agents share the same memory store, each agent must have a unique `name` set at construction time. The plugin enforces this at wiring time — if `agent.name` is not set, `Agent(plugins=[plugin])` raises `ValueError`.

The `name` is used as the memory namespace key. It must be stable across process restarts so stored memories remain retrievable.

```python
store  = MultiTenantS3VectorMemory(bucket_name=..., tvm_role_arn=...)
plugin = S3VectorMemoryPlugin(store=store, base_prompt=BASE_PROMPT)

orchestrator = Agent(
    model         = BedrockModel(),
    name          = "orchestrator",   # required — unique per agent role
    plugins       = [plugin],
    system_prompt = BASE_PROMPT,
)
researcher = Agent(
    model         = BedrockModel(),
    name          = "researcher",
    plugins       = [plugin],
    system_prompt = BASE_PROMPT,
)
```

Each agent retrieves only its own memories by default. To retrieve memories across all agents for a user (e.g. a supervisor agent), call the store directly with `agent_name=None`:

```python
all_memories = store.retrieve_memories(
    user_id        = "user-456",
    query          = "what has been decided so far?",
    tenant_context = tenant_context,
    agent_name     = None,   # no agent filter — returns all agents' memories
)
```

See `examples/multi_agent_orchestrator.py` for a runnable end-to-end example.

---

### Multi-tenant

One index per tenant. IAM credentials scoped per tenant via STS AssumeRole + TenantID session tag.

```python
import os
from strands import Agent
from strands.models import BedrockModel
from strands_s3_vectors_memory import MultiTenantS3VectorMemory, S3VectorMemoryPlugin

BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses."""

store  = MultiTenantS3VectorMemory(
    bucket_name  = os.environ["S3_VECTOR_BUCKET_NAME"],
    tvm_role_arn = os.environ["S3_VECTOR_TVM_ROLE_ARN"],
)
plugin = S3VectorMemoryPlugin(store=store, base_prompt=BASE_PROMPT)
agent  = Agent(
    model   = BedrockModel(),
    tools   = [plugin.memory_tool],  # optional: mid-turn recall on demand
    plugins = [plugin],
    system_prompt = BASE_PROMPT,
)

tenant_context = {
    "tenantId":   "tenant-001",
    "tenantName": "Acme Corp",
}

# Mid-conversation turn
agent("Our Q4 budget is $2M.", invocation_state={
    "tenant_context":  tenant_context,
    "user_id":         "user-456",
    "conversation_id": "conv-001",
    "end_session":     False,
})

# Final turn — plugin summarizes and stores to S3 Vectors in a background thread
agent("Thanks, bye.", invocation_state={
    "tenant_context":  tenant_context,
    "user_id":         "user-456",
    "conversation_id": "conv-001",
    "end_session":     True,
})
```

The only difference between the two modes is the `store` class and the presence of `tenant_context` in `invocation_state`. `MultiTenantS3VectorMemory` requires `tvm_role_arn` — omitting it raises `ValueError` to prevent silent bypass of IAM ABAC isolation.

### `invocation_state` keys

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `user_id` | `str` | Yes | User identifier — used as metadata filter on vector operations |
| `conversation_id` | `str` | Yes | Unique conversation ID — scopes buffer and summary key |
| `end_session` | `bool` | No (default `False`) | If `True`, summarize and store conversation after response (non-blocking) |
| `tenant_context` | `dict` | Multi-tenant only | Must contain `tenantId` |

### `memory_tool` — mid-turn recall on demand

The plugin exposes a `memory_tool` property that returns a Strands `@tool`. When wired to the agent, the LLM can call it mid-conversation to retrieve specific memories it discovers it needs during reasoning — without starting a new session.

```python
agent = Agent(
    model   = BedrockModel(),
    tools   = [plugin.memory_tool],  # LLM calls this when it needs to recall something
    plugins = [plugin],              # handles auto-inject + end_session store
    system_prompt = BASE_PROMPT,
)
```

The tool is **retrieve-only**. The LLM provides a natural language `query`; identity (`user_id`, `tenant_context`) is read automatically from the plugin's context — the LLM never sees or handles credentials.

**When to use it:** the automatic `before_invocation` injection handles broad contextual priming on the first turn. The `memory_tool` handles *specific, targeted recall* the agent discovers it needs mid-reasoning — for example, a topic pivot mid-conversation, a temporally distant memory with different keywords, or a fact the LLM needs to complete a chain-of-thought.

---

See the [plugin reference](docs/strands-s3-vector-memory-plugin.md#testing) for full details on running unit and integration tests, required env vars, and debug logging.

---

## Run the examples

The examples require the indexes to exist before running. The integration tests create and delete indexes automatically, but the examples expect them to be present persistently.

### Single-tenant

**Step 1 — create the index** (once, before first run):

```bash
aws s3vectors create-index \
  --vector-bucket-name $S3_VECTOR_BUCKET_NAME \
  --index-name memory \
  --data-type float32 --dimension 1024 --distance-metric cosine \
  --metadata-configuration '{"nonFilterableMetadataKeys":["content","stored_at","conversation_id","type"]}' \
  --region $AWS_REGION
```

**Step 2 — run:**

```bash
source .venv/bin/activate
export S3_VECTOR_BUCKET_NAME=my-vector-memory
export AWS_REGION=us-east-1

cd examples
python3 single_tenant_agent.py
```

**Expected output:**

```
============================================================
SESSION 1 — storing a fact
============================================================

  USER: My favourite framework is Strands Agents.
 AGENT: That's great! Strands Agents is a nice framework to work with...

  USER: What framework did I mention?
 AGENT: You mentioned Strands Agents as your favourite framework.
        [end_session=True — summarizing in background]

[waiting 5s for background summary store to complete...]

============================================================
SESSION 2 — memory injected automatically on first turn
============================================================

  USER: What do you know about my preferences?
 AGENT: Based on our previous conversations, I know that your favorite
        framework is Strands Agents.

============================================================
SESSION 3 — memory_tool: mid-turn recall on demand
  The agent uses the memory_tool when it needs to recall
  something specific mid-conversation.
============================================================

  USER: I'm evaluating some new tools. By the way, remind me — what
        framework did I mention I liked in a previous session?
 AGENT: You mentioned that Strands Agents is your favorite framework.
        Good luck with evaluating the new tools!
```

### Multi-tenant (with tenant isolation demo)

**Step 1 — create indexes for both tenants** (once, before first run):

```bash
for TENANT in tenant-001 tenant-002; do
  aws s3vectors create-index \
    --vector-bucket-name $S3_VECTOR_BUCKET_NAME \
    --index-name memory-${TENANT} \
    --data-type float32 --dimension 1024 --distance-metric cosine \
    --metadata-configuration '{"nonFilterableMetadataKeys":["content","stored_at","conversation_id","type"]}' \
    --region $AWS_REGION
done
```

**Step 2 — run:**

```bash
source .venv/bin/activate
export S3_VECTOR_BUCKET_NAME=my-vector-memory
export S3_VECTOR_TVM_ROLE_ARN=arn:aws:iam::<account-id>:role/<tvm-role-name>
export AWS_REGION=us-east-1

cd examples
python3 multi_tenant_agent.py
```

**Expected output:**

```
============================================================
SESSION 1 — tenant=tenant-001  storing a fact
============================================================

  USER: Our Q4 budget is $2M and it is confidential.
  AGENT: I understand and acknowledge that your Q4 budget is $2M and
         that this information is confidential...

  USER: Got it, thanks. [end_session=True]
  AGENT: You're welcome! Feel free to reach out anytime...

[waiting 5s for background summary store to complete...]

============================================================
SESSION 2 — tenant=tenant-001  memory should be recalled
============================================================

  USER: What did I tell you about our budget?
  AGENT: You shared that your Q4 budget is $2M, and you indicated that
         this information is confidential...

============================================================
SESSION 3 — tenant=tenant-002  isolation check
  tenant-002 asks the same question about the budget.
  Expected: agent has NO memory — cannot see tenant-001's data.
============================================================

  USER (tenant-002): What did I tell you about our budget?
  AGENT: I don't have any record of you telling me about your budget
         in our previous conversations...

  👆 Review the response above — tenant-002 should have no knowledge of
     tenant-001's Q4 budget. If the response mentions '$2M', isolation has failed.

============================================================
SESSION 4 — tenant=tenant-001  memory_tool: mid-turn recall on demand
  The agent uses the memory_tool when it discovers mid-reasoning
  that it needs a specific fact from a previous session.
============================================================

  USER: We're planning Q1 now. Can you remind me what our Q4 budget
        was and whether there were any constraints I mentioned?
  AGENT: Based on our previous conversation, your Q4 budget was $2M.
         You marked this as confidential information...
```

Session 3 demonstrates application-level isolation — `tenant-002` has its own empty index and receives no memory context. The TVM credentials for `tenant-002` are also physically scoped to `memory-tenant-002` by IAM ABAC, so even a direct API call to `memory-tenant-001` would be denied with `AccessDeniedException`.

---

## Clean up

```bash
# Delete indexes
for INDEX in memory memory-tenant-001 memory-tenant-002; do
  aws s3vectors delete-index \
    --vector-bucket-name $S3_VECTOR_BUCKET_NAME \
    --index-name $INDEX \
    --region $AWS_REGION
done

# Delete the bucket
aws s3vectors delete-vector-bucket \
  --vector-bucket-name $S3_VECTOR_BUCKET_NAME \
  --region $AWS_REGION

# Delete the TVM IAM role
aws iam delete-role-policy \
  --role-name workshop-module3-lab1-s3vectors-tvm-role \
  --policy-name S3VectorsTenantPolicy

aws iam delete-role \
  --role-name workshop-module3-lab1-s3vectors-tvm-role
```

---

## Further reading

For a deep dive into how the plugin works — lifecycle hooks, tenant isolation, conversation buffer, TVM credential caching, and design decisions — see the [plugin reference](docs/strands-s3-vector-memory-plugin.md).

- [Amazon S3 Vectors documentation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
- [Strands Agents Plugins](https://strandsagents.com/docs/user-guide/concepts/plugins/)
- [Token Vending Machine pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/implement-saas-tenant-isolation-for-amazon-s3-by-using-an-aws-lambda-token-vending-machine.html)
- [AWS STS session tags](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_session-tags.html)
