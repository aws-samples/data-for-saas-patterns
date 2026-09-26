#!/usr/bin/env bash
# cleanup.sh — Tears down every AWS resource created by the setup scripts, plus
# local toolkit artifacts. Safe to run repeatedly: missing resources are skipped.
#
# Resolves resources by their fixed names, so no env vars are required. You may
# still override any of them:
#   AGENT_NAME       (default: multi_tenant_agentcore_memory_agent)
#   MEMORY_NAME      (default: multi_tenant_agentcore_memory)
#   TVM_ROLE_NAME    (default: agentcore-memory-tvm-role)
#   COGNITO_POOL_NAME (default: agentcore-memory-user-pool)
#   AWS_REGION       (default: us-east-1)
#
# Usage: bash scripts/cleanup.sh
set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
AGENT_NAME="${AGENT_NAME:-multi_tenant_agentcore_memory_agent}"
MEMORY_NAME="${MEMORY_NAME:-multi_tenant_agentcore_memory}"
TVM_ROLE_NAME="${TVM_ROLE_NAME:-agentcore-memory-tvm-role}"
MEMORY_EXEC_ROLE_NAME="${MEMORY_EXEC_ROLE_NAME:-agentcore-memory-exec-role}"
COGNITO_POOL_NAME="${COGNITO_POOL_NAME:-agentcore-memory-user-pool}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "→ Region : $REGION"
echo ""

# ---------------------------------------------------------------------------
# 1. Delete AgentCore Runtime(s) matching the agent name
# ---------------------------------------------------------------------------
echo "→ Deleting AgentCore Runtime(s) named '$AGENT_NAME' ..."
RUNTIME_IDS=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='${AGENT_NAME}'].agentRuntimeId" \
  --output text 2>/dev/null || echo "")
if [ -n "$RUNTIME_IDS" ]; then
  for RID in $RUNTIME_IDS; do
    echo "  Deleting runtime: $RID"
    aws bedrock-agentcore-control delete-agent-runtime \
      --agent-runtime-id "$RID" --region "$REGION" >/dev/null 2>&1 || true
  done
else
  echo "  None found."
fi

# ---------------------------------------------------------------------------
# 2. Delete AgentCore Memory resource(s) matching the memory name
# ---------------------------------------------------------------------------
echo "→ Deleting AgentCore Memory named '$MEMORY_NAME' ..."
# AgentCore Memory IDs are "{name}-{suffix}" — match on that exact prefix so a
# name like "multi_tenant_agentcore_memory" doesn't also delete an unrelated
# memory such as "multi_tenant_agentcore_memory_v2".
MEMORY_IDS=$(aws bedrock-agentcore-control list-memories --region "$REGION" \
  --query "memories[?starts_with(id, '${MEMORY_NAME}-')].id" \
  --output text 2>/dev/null || echo "")
if [ -n "$MEMORY_IDS" ]; then
  for MID in $MEMORY_IDS; do
    echo "  Deleting memory: $MID"
    aws bedrock-agentcore-control delete-memory \
      --memory-id "$MID" --region "$REGION" >/dev/null 2>&1 || true
  done
else
  echo "  None found."
fi

# ---------------------------------------------------------------------------
# 3. Delete the TVM IAM role (inline policy first)
# ---------------------------------------------------------------------------
echo "→ Deleting TVM role '$TVM_ROLE_NAME' ..."
if aws iam get-role --role-name "$TVM_ROLE_NAME" >/dev/null 2>&1; then
  aws iam delete-role-policy --role-name "$TVM_ROLE_NAME" \
    --policy-name "AgentCoreMemoryTenantPolicy" >/dev/null 2>&1 || true
  aws iam delete-role --role-name "$TVM_ROLE_NAME" >/dev/null 2>&1 \
    && echo "  Deleted." || echo "  Could not delete (check for remaining attached policies)."
else
  echo "  Not found."
fi

# ---------------------------------------------------------------------------
# 3b. Delete the memory execution role (inline policy first)
# ---------------------------------------------------------------------------
echo "→ Deleting memory execution role '$MEMORY_EXEC_ROLE_NAME' ..."
if aws iam get-role --role-name "$MEMORY_EXEC_ROLE_NAME" >/dev/null 2>&1; then
  aws iam delete-role-policy --role-name "$MEMORY_EXEC_ROLE_NAME" \
    --policy-name "MemoryExtractionModelInvoke" >/dev/null 2>&1 || true
  aws iam delete-role --role-name "$MEMORY_EXEC_ROLE_NAME" >/dev/null 2>&1 \
    && echo "  Deleted." || echo "  Could not delete (check for remaining attached policies)."
else
  echo "  Not found."
fi

# ---------------------------------------------------------------------------
# 4. Delete the Cognito user pool
# ---------------------------------------------------------------------------
echo "→ Deleting Cognito user pool '$COGNITO_POOL_NAME' ..."
POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='${COGNITO_POOL_NAME}'].Id" --output text 2>/dev/null || echo "")
if [ -n "$POOL_ID" ]; then
  for PID in $POOL_ID; do
    echo "  Deleting pool: $PID"
    aws cognito-idp delete-user-pool --user-pool-id "$PID" --region "$REGION" >/dev/null 2>&1 || true
  done
else
  echo "  Not found."
fi

# ---------------------------------------------------------------------------
# 5. Remove local toolkit artifacts
# ---------------------------------------------------------------------------
echo "→ Removing local toolkit artifacts ..."
rm -rf "$REPO_ROOT/.bedrock_agentcore.yaml" "$REPO_ROOT/.bedrock_agentcore" \
       "$REPO_ROOT/examples/agent.py" "$REPO_ROOT/examples/requirements.txt"

echo ""
echo "✓ Cleanup complete."
echo ""
echo "Note: AgentCore Memory deletion is asynchronous — a deleted memory may"
echo "briefly remain in DELETING state before it fully disappears."
