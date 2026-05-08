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
- **Cross-session recall is limited.** When a user returns days later, the previous conversation is gone unless it was explicitly persisted.

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
│   ├── single_tenant_agent.py         # Single-tenant AgentCore Runtime handler
│   ├── multi_tenant_agent.py          # Multi-tenant AgentCore Runtime handler (TVM + isolation)
│   └── multi_agent_orchestrator.py    # Multi-agent memory isolation example
├── tests/
│   ├── unit/                          # 110 tests, no AWS credentials required
│   └── integration/                   # 37 tests, requires live AWS resources
├── scripts/
│   ├── setup_tvm_role.sh              # Creates the TVM IAM role with ABAC policy
│   ├── setup_cognito.sh               # Cognito user pool + test users
│   ├── setup_agentcore.sh             # AgentCore Runtime deployment
│   └── test_examples.sh               # End-to-end example tests (local + AgentCore)
├── docs/
│   ├── strands-s3-vector-memory-plugin.md  # Full plugin reference
│   └── s3-vectors-memory.mdx              # Community plugin listing
├── pyproject.toml                     # Package build config
└── images/
    └── s3_vector_memory.png           # Architecture diagram
```

---

## Requirements

- Python 3.10+
- `strands-agents >= 1.0.0`
- `boto3 >= 1.35`
- `cachetools >= 5.0` (used by TokenVendingMachine for credential caching)
- `bedrock-agentcore-starter-toolkit >= 0.1.0` (for AgentCore Runtime deployment — install via `pip install -e ".[agentcore]"`)
- AWS account with S3 Vectors access and Bedrock Nova Embeddings enabled in your region
  - `amazon.nova-2-multimodal-embeddings-v1:0`
  - A Claude model (e.g. `us.anthropic.claude-sonnet-4-5-20250929-v1:0`)
- AWS CLI v2 configured with credentials that can access IAM, S3 Vectors, Bedrock, and STS

---

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .                    # core library
pip install -e ".[agentcore]"       # + AgentCore Runtime starter toolkit
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

> **Note:** The plugin detects the first turn via `len(agent.messages) == 0`. On AgentCore Runtime, the microVM persists `agent.messages` across turns automatically. On other platforms (ECS, Lambda), attach a `SessionManager` to persist messages across requests.

```python
import os
from strands import Agent
from strands_s3_vectors_memory import S3VectorMemory, S3VectorMemoryPlugin

BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses."""

store  = S3VectorMemory(bucket_name=os.environ["S3_VECTOR_BUCKET_NAME"])
plugin = S3VectorMemoryPlugin(store=store, base_prompt=BASE_PROMPT)
agent  = Agent(
    name          = "assistant",
    plugins       = [plugin],
    system_prompt = BASE_PROMPT,
)

# Turn 1
agent("My favourite framework is Strands Agents.", invocation_state={
    "user_id": "user-001", "conversation_id": "conv-001", "end_session": False,
})

# Turn 2 -- end_session=True triggers background summarization and vector store
agent("Thanks, bye.", invocation_state={
    "user_id": "user-001", "conversation_id": "conv-001", "end_session": True,
})

# Next session -- plugin retrieves the stored summary and injects it into the prompt
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
    name          = "orchestrator",   # required -- unique per agent role
    plugins       = [plugin],
    system_prompt = BASE_PROMPT,
)
researcher = Agent(
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
from strands_s3_vectors_memory import MultiTenantS3VectorMemory, S3VectorMemoryPlugin

BASE_PROMPT = """You are a helpful assistant.

{memory_context}

Use prior context naturally in your responses."""

tenant_context = {
    "tenantId":   "tenant-001",
    "tenantName": "Acme Corp",
}

store  = MultiTenantS3VectorMemory(
    bucket_name  = os.environ["S3_VECTOR_BUCKET_NAME"],
    tvm_role_arn = os.environ["S3_VECTOR_TVM_ROLE_ARN"],
)
plugin = S3VectorMemoryPlugin(store=store, base_prompt=BASE_PROMPT)
agent  = Agent(
    name          = "assistant",
    plugins       = [plugin],
    system_prompt = BASE_PROMPT,
)

# Mid-conversation turn
agent("Our Q4 budget is $2M.", invocation_state={
    "tenant_context":  tenant_context,
    "user_id":         "user-456",
    "conversation_id": "conv-001",
    "end_session":     False,
})

# Final turn -- plugin summarizes and stores to S3 Vectors in a background thread
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
    name          = "assistant",
    plugins       = [plugin],              # handles auto-inject + end_session store
    system_prompt = BASE_PROMPT,
    tools         = [plugin.memory_tool],  # LLM calls this when it needs to recall something
)
```

The tool is **retrieve-only**. The LLM provides a natural language `query`; identity (`user_id`, `tenant_context`) is read automatically from the plugin's context — the LLM never sees or handles credentials.

**When to use it:** the automatic `before_invocation` injection handles broad contextual priming on the first turn. The `memory_tool` handles *specific, targeted recall* the agent discovers it needs mid-reasoning — for example, a topic pivot mid-conversation, a temporally distant memory with different keywords, or a fact the LLM needs to complete a chain-of-thought.

---

See the [plugin reference](docs/strands-s3-vector-memory-plugin.md#testing) for full details on running unit and integration tests, required env vars, and debug logging.

---

## Run the examples

The examples are designed for deployment on [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html) — a serverless runtime that handles HTTP serving, JWT authentication, and true session isolation (one request per microVM). Each example is a self-contained HTTP server using `BedrockAgentCoreApp`.

The automated test script `scripts/test_examples.sh` handles the full lifecycle: infrastructure setup, local testing, optional AgentCore deployment, and end-to-end validation.

### Prerequisites

```bash
pip install -e ".[agentcore]"   # installs bedrock-agentcore + starter toolkit
```

---

### Run tests locally (automated)

The test script starts the server, runs all sessions, validates responses, and stops the server automatically:

```bash
export S3_VECTOR_BUCKET_NAME=my-vector-memory
export AWS_REGION=us-east-1

# Run both single-tenant and multi-tenant tests
bash scripts/test_examples.sh

# Run only single-tenant
bash scripts/test_examples.sh --mode single

# Run only multi-tenant
bash scripts/test_examples.sh --mode multi
```

The script automatically:
1. Runs `setup_tvm_role.sh` to create/update the TVM IAM role
2. Runs `setup_cognito.sh` to create the Cognito user pool and two test users
3. Obtains real JWTs via `cognito-idp initiate-auth` (IdToken with `custom:tenant_id`)
4. Resets vector indexes to clean state
5. Starts the server, runs the session flow, validates responses, stops the server

---

### Run tests manually (local server)

**Single-tenant:**

```bash
export S3_VECTOR_BUCKET_NAME=my-vector-memory
export AWS_REGION=us-east-1

python3 examples/single_tenant_agent.py
# Server starts on http://127.0.0.1:8080

# Health check
curl http://localhost:8080/ping

# Turn 1 — store a fact (user_id passed in payload for single-tenant)
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: conv-001" \
  -d '{"prompt": "My favourite framework is Strands Agents.", "user_id": "user-001", "end_session": false}'

# Turn 2 — end session triggers background summarization
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: conv-001" \
  -d '{"prompt": "Thanks, bye.", "user_id": "user-001", "end_session": true}'

# New session — memory injected automatically on first turn
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: conv-002" \
  -d '{"prompt": "What do you know about my preferences?", "user_id": "user-001"}'
```

**Multi-tenant:**

```bash
export S3_VECTOR_BUCKET_NAME=my-vector-memory
export S3_VECTOR_TVM_ROLE_ARN=arn:aws:iam::<account-id>:role/<tvm-role-name>
export AWS_REGION=us-east-1

# Obtain JWTs from Cognito (IdToken contains custom:tenant_id claim)
export JWT_T1=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id $COGNITO_CLIENT_ID \
  --auth-parameters "USERNAME=tenant001@example.com,PASSWORD=..." \
  --region $AWS_REGION \
  --query "AuthenticationResult.IdToken" --output text)

python3 examples/multi_tenant_agent.py

# Session 1 — tenant-001 stores a fact
# Note: avoid shell-special characters like $ in prompt strings
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $JWT_T1" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: conv-001" \
  -d '{"prompt": "Our Q4 budget is 2 million dollars and it is confidential.", "end_session": true}'
```

> **Important:** The session header name is `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` (not `X-Runtime-Session-Id`). This is the exact header name used by AgentCore Runtime.

> **Important:** Avoid shell-special characters (`$`, backticks) in prompt strings passed via `-d`. Use `2 million dollars` instead of `$2M` to prevent shell variable expansion corrupting the payload.

---

### Deploy to AgentCore Runtime

The `setup_agentcore.sh` script uses the [AgentCore starter toolkit](https://github.com/awslabs/amazon-bedrock-agentcore-samples) to package and deploy via **direct code deploy** (no Docker required).

**Prerequisites for deployment:**

```bash
export COGNITO_USER_POOL_ID=<from setup_cognito.sh>
export COGNITO_CLIENT_ID=<from setup_cognito.sh>
export S3_VECTOR_TVM_ROLE_ARN=<from setup_tvm_role.sh>
```

**Deploy multi-tenant agent:**

```bash
EXAMPLE_FILE=multi_tenant_agent.py \
AGENT_NAME=strands_s3_vector_memory_agent \
bash scripts/setup_agentcore.sh
# Prints: export AGENT_ARN=arn:aws:bedrock-agentcore:...
export AGENT_ARN=<printed-arn>
```

The script:
1. Packages `examples/` + `src/` into a deployment zip (the `src/` directory must be included so `strands_s3_vectors_memory` is importable at runtime)
2. Configures a JWT authorizer using the Cognito discovery URL and `allowedAudience: [COGNITO_CLIENT_ID]`
3. Sets `requestHeaderAllowlist: ["Authorization"]` so the JWT is forwarded to the container
4. Attaches `bedrock:InvokeModel`, `sts:AssumeRole`+`sts:TagSession` (TVM), and S3 session storage permissions to the execution role
5. Updates the TVM role trust policy to allow the execution role to assume it

> **Note on `runtime_type`:** The `.bedrock_agentcore.yaml` config must have `runtime_type: PYTHON_3_12`. If it is `null`, the dependency build will fail with `AttributeError: 'NoneType' object has no attribute 'upper'`.

> **Note on artifact type:** An AgentCore Runtime created with a container (ECR) artifact cannot be updated to use direct code deploy. Delete and recreate the runtime if switching deployment types.

---

### Run tests against AgentCore Runtime (automated)

```bash
export S3_VECTOR_BUCKET_NAME=my-vector-memory
export AWS_REGION=us-east-1
export AGENT_ARN=arn:aws:bedrock-agentcore:...   # skip deploy if already set

# Deploy (if AGENT_ARN not set) then test
bash scripts/test_examples.sh --agentcore --mode multi

# Test only (skip deploy — AGENT_ARN already set)
AGENT_ARN=arn:aws:bedrock-agentcore:... \
  bash scripts/test_examples.sh --agentcore --mode multi
```

---

### Invoke AgentCore Runtime manually

AgentCore Runtime with JWT auth **cannot be invoked via boto3** — use an HTTP client with the `Authorization: Bearer` header ([per AWS docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html)):

```python
import urllib.parse, requests, boto3, json

# Get JWT from Cognito
resp = boto3.client('cognito-idp', region_name='us-east-1').initiate_auth(
    AuthFlow='USER_PASSWORD_AUTH',
    ClientId=COGNITO_CLIENT_ID,
    AuthParameters={'USERNAME': 'tenant001@example.com', 'PASSWORD': '...'}
)
jwt = resp['AuthenticationResult']['IdToken']

# Invoke via HTTPS — runtimeSessionId must be >= 33 characters
agent_arn = 'arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/<id>'
escaped   = urllib.parse.quote(agent_arn, safe='')
url       = f"https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{escaped}/invocations?qualifier=DEFAULT"

r = requests.post(url,
    headers={
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "agentcore-runtime-session-conv-001",
    },
    json={"prompt": "What do you know about my budget?"},
    timeout=120,
)
print(r.json())
```

> **`runtimeSessionId` minimum length:** The session ID must be at least 33 characters. Prefix short IDs (e.g. `agentcore-runtime-session-{conv_id}`).

> **JWT token type:** Use the Cognito **IdToken** (not AccessToken). The IdToken contains `custom:tenant_id` in its claims and its `aud` claim matches the `allowedAudience` configured in the JWT authorizer.

> **JWT forwarding:** The JWT is validated by AgentCore Runtime before reaching the container. The container reads it from `context.request_headers["Authorization"]` (forwarded because `Authorization` is in `requestHeaderAllowlist`). Signature re-validation in the container is not needed.

---

### Singleton agent on AgentCore Runtime

AgentCore Runtime provides **session isolation at the infrastructure level** -- each `runtimeSessionId` is routed to a dedicated microVM that persists for up to 8 hours. All turns of a conversation arrive at the same microVM, so `agent.messages` is maintained in memory across turns automatically.

This means:

- No `SessionManager` is needed -- the microVM IS the session store
- No `_switch_session()` or session restore logic -- the framework handles it
- The plugin's `len(agent.messages) == 0` check correctly detects the first turn (fresh microVM) vs subsequent turns (messages already in memory)
- `end_session=True` on the final turn has the complete conversation transcript for summarisation

This is different from ECS/Lambda deployments where each request may hit a different process and a `SessionManager` is required to persist `agent.messages` across requests.

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

# Delete the S3 Vectors bucket
aws s3vectors delete-vector-bucket \
  --vector-bucket-name $S3_VECTOR_BUCKET_NAME \
  --region $AWS_REGION

# Delete the TVM IAM role
aws iam delete-role-policy \
  --role-name strands-s3-vectors-memory-tvm-role \
  --policy-name S3VectorsTenantPolicy

aws iam delete-role \
  --role-name strands-s3-vectors-memory-tvm-role
```

---

## Further reading

For a deep dive into how the plugin works -- lifecycle hooks, tenant isolation, TVM credential caching, and design decisions -- see the [plugin reference](docs/strands-s3-vector-memory-plugin.md).

- [Amazon S3 Vectors documentation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html)
- [Strands Agents Plugins](https://strandsagents.com/docs/user-guide/concepts/plugins/)
- [Token Vending Machine pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/implement-saas-tenant-isolation-for-amazon-s3-by-using-an-aws-lambda-token-vending-machine.html)
- [AWS STS session tags](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_session-tags.html)
