#!/usr/bin/env bash
# setup_memory.sh — Creates an AgentCore Memory resource with two long-term
# memory strategies (USER_PREFERENCE + SEMANTIC) and waits for it to become
# ACTIVE.
#
# Long-term memory requires a memory execution role: AgentCore Memory assumes it
# to invoke a Bedrock model that extracts durable facts/preferences from raw
# conversation events and writes them to the strategy namespaces. If you don't
# pass one, this script creates a correctly-scoped role for you.
#
# The strategy namespaces are /{actorId}/preferences (USER_PREFERENCE) and
# /{actorId}/facts (SEMANTIC). These MUST match the retrieval namespaces the
# session manager reads (agentcore_memory_session_manager.py). Because actor_id
# is {tenantId}:{agentName}:{userId}, each resolved namespace always starts with
# the tenant id, so it stays within the TVM role's
# bedrock-agentcore:namespacePath ABAC condition (/${aws:PrincipalTag/TenantID}:*)
# — long-term memory is tenant-isolated.
#
# Usage:
#   bash setup_memory.sh [<memory-execution-role-arn>]
#
# Outputs:
#   export AGENTCORE_MEMORY_ID=...
#   export AGENTCORE_MEMORY_ARN=...
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
MEMORY_NAME="${MEMORY_NAME:-multi_tenant_agentcore_memory}"
EXEC_ROLE_NAME="${MEMORY_EXEC_ROLE_NAME:-agentcore-memory-exec-role}"
MEMORY_EXEC_ROLE_ARN="${1:-}"

echo "→ Region : $REGION"
echo "→ Name   : $MEMORY_NAME"
echo ""

# ---------------------------------------------------------------------------
# 1. Ensure a memory execution role exists (create a scoped one if not supplied)
# ---------------------------------------------------------------------------
if [ -z "$MEMORY_EXEC_ROLE_ARN" ]; then
  echo "→ Ensuring memory execution role '$EXEC_ROLE_NAME' ..."

  # Trust policy: only the AgentCore service in THIS account may assume the role.
  TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "${ACCOUNT_ID}" },
        "ArnLike": { "aws:SourceArn": "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:memory/*" }
      }
    }
  ]
}
EOF
)

  # Permissions: invoke only the Claude models AgentCore uses for extraction,
  # via both foundation-model and inference-profile ARNs. No wildcard on all
  # Bedrock models.
  PERMISSIONS_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "MemoryExtractionModelInvoke",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:${ACCOUNT_ID}:inference-profile/*.anthropic.claude-*"
      ]
    }
  ]
}
EOF
)

  if aws iam get-role --role-name "$EXEC_ROLE_NAME" &>/dev/null; then
    aws iam update-assume-role-policy --role-name "$EXEC_ROLE_NAME" \
      --policy-document "$TRUST_POLICY" >/dev/null
  else
    aws iam create-role --role-name "$EXEC_ROLE_NAME" \
      --assume-role-policy-document "$TRUST_POLICY" \
      --description "AgentCore Memory long-term extraction execution role" >/dev/null
    sleep 10  # allow IAM propagation before CreateMemory references it
  fi

  aws iam put-role-policy --role-name "$EXEC_ROLE_NAME" \
    --policy-name "MemoryExtractionModelInvoke" \
    --policy-document "$PERMISSIONS_POLICY" >/dev/null

  MEMORY_EXEC_ROLE_ARN=$(aws iam get-role --role-name "$EXEC_ROLE_NAME" --query Role.Arn --output text)
  echo "  Exec role: $MEMORY_EXEC_ROLE_ARN"
fi

# ---------------------------------------------------------------------------
# 2. Create the memory resource with a long-term USER_PREFERENCE strategy
# ---------------------------------------------------------------------------
MEMORY_ID=$(python3 - "$MEMORY_NAME" "$REGION" "$MEMORY_EXEC_ROLE_ARN" <<'PYEOF'
import sys, boto3

name, region, exec_role = sys.argv[1], sys.argv[2], sys.argv[3]
client = boto3.client("bedrock-agentcore-control", region_name=region)

# Reuse an existing memory with this name if present, but only if it already
# has BOTH long-term strategies (USER_PREFERENCE + SEMANTIC). Reusing a memory
# created without them would silently disable long-term recall with no clear error.
for m in client.list_memories().get("memories", []):
    memory_id = m.get("id", "")
    if not memory_id.startswith(f"{name}-"):
        continue
    detail = client.get_memory(memoryId=memory_id).get("memory", {})
    strategies = detail.get("strategies") or detail.get("memoryStrategies") or []
    has_pref = any(
        "userPreferenceMemoryStrategy" in s or s.get("type") == "USER_PREFERENCE"
        for s in strategies
    )
    has_semantic = any(
        "semanticMemoryStrategy" in s or s.get("type") == "SEMANTIC"
        for s in strategies
    )
    if not (has_pref and has_semantic):
        print(
            f"ERROR: found existing memory '{memory_id}' named '{name}' but it is "
            "missing the USER_PREFERENCE and/or SEMANTIC strategy (likely created by "
            "an older version of this script). Long-term recall will not work fully "
            "with it. Delete it with "
            f"'aws bedrock-agentcore-control delete-memory --memory-id {memory_id}' "
            "or set MEMORY_NAME to a new value and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(memory_id)
    sys.exit(0)

resp = client.create_memory(
    name=name,
    eventExpiryDuration=90,
    memoryExecutionRoleArn=exec_role,
    memoryStrategies=[
        {
            "userPreferenceMemoryStrategy": {
                "name": "TenantUserPreferences",
                "description": "Durable per-tenant user preferences extracted from conversations",
                "namespaces": ["/{actorId}/preferences"],
            }
        },
        {
            "semanticMemoryStrategy": {
                "name": "TenantSemanticFacts",
                "description": "Durable per-tenant semantic facts extracted from conversations",
                "namespaces": ["/{actorId}/facts"],
            }
        },
    ],
)
# create_memory returns the resource nested under a "memory" key.
memory = resp.get("memory", resp)
memory_id = memory.get("id") or memory.get("memoryId", "")
if not memory_id:
    arn = memory.get("arn") or memory.get("memoryArn", "")
    memory_id = arn.split("/")[-1] if arn else ""
print(memory_id)
PYEOF
)

if [ -z "$MEMORY_ID" ]; then
  echo "  ERROR: Failed to create or find memory."
  exit 1
fi

echo "→ Memory ID: $MEMORY_ID — waiting for ACTIVE ..."
for i in $(seq 1 40); do
  STATUS=$(aws bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
    --region "$REGION" --query "memory.status" --output text 2>/dev/null || echo "UNKNOWN")
  if [ "$STATUS" = "ACTIVE" ]; then
    echo "  Memory ACTIVE ✓"
    break
  fi
  printf "  Status: %s (%ds)\r" "$STATUS" $((i * 5))
  sleep 5
done

MEMORY_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:memory/${MEMORY_ID}"

echo ""
echo "✓ Done. Set these environment variables before running the demo:"
echo ""
echo "  export AGENTCORE_MEMORY_ID=${MEMORY_ID}"
echo "  export AGENTCORE_MEMORY_ARN=${MEMORY_ARN}"
echo ""
