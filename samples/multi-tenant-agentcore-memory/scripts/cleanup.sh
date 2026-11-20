#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# cleanup.sh — Tears down the AWS resources created by the setup scripts, plus
# local toolkit artifacts. Safe to run repeatedly: missing resources are skipped.
#
# Ownership: the setup scripts tag what they create with
# sample=multi-tenant-agentcore-memory. This script deletes a memory, IAM role or
# user pool only when it carries that tag, so a same-named resource you made
# yourself is left alone (and reported).
#
# The AgentCore Runtime is removed with the starter toolkit's destroy operation,
# which also removes what the toolkit created on deploy: the runtime execution
# role (with its inline policies) and the S3 code artifacts.
# That needs .bedrock_agentcore.yaml from the last deploy; without it the
# runtime is deleted by name and the toolkit resources are listed for you to
# check by hand.
#
# Resolves resources by their fixed names, so no env vars are required. You may
# still override any of them:
#   AGENT_NAME       (default: mt_memory_agent)
#   MEMORY_NAME      (default: multi_tenant_agentcore_memory)
#   TVM_ROLE_NAME    (default: agentcore-memory-tvm-role)
#   COGNITO_POOL_NAME (default: agentcore-memory-user-pool)
#   AWS_REGION       (default: us-east-1)
#
# Usage: bash scripts/cleanup.sh
set -uo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
REGION="$AWS_REGION"
export AGENT_NAME="${AGENT_NAME:-mt_memory_agent}"
MEMORY_NAME="${MEMORY_NAME:-multi_tenant_agentcore_memory}"
TVM_ROLE_NAME="${TVM_ROLE_NAME:-agentcore-memory-tvm-role}"
MEMORY_EXEC_ROLE_NAME="${MEMORY_EXEC_ROLE_NAME:-agentcore-memory-exec-role}"
COGNITO_POOL_NAME="${COGNITO_POOL_NAME:-agentcore-memory-user-pool}"
SAMPLE_TAG_VALUE="multi-tenant-agentcore-memory"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "→ Region : $REGION"
echo ""

# ---------------------------------------------------------------------------
# 1. AgentCore Runtime and the toolkit's deploy resources
# ---------------------------------------------------------------------------
echo "→ Removing AgentCore Runtime '$AGENT_NAME' ..."
if [ -f "$REPO_ROOT/.bedrock_agentcore.yaml" ]; then
  python3 - <<'PYEOF' || echo "  Toolkit destroy failed — check the messages above."
import os
from pathlib import Path

from bedrock_agentcore_starter_toolkit.operations.runtime import destroy_bedrock_agentcore

result = destroy_bedrock_agentcore(
    config_path=Path(os.environ["REPO_ROOT"]) / ".bedrock_agentcore.yaml",
    agent_name=os.environ["AGENT_NAME"],
    force=True,
)
for item in result.resources_removed:
    print(f"  Removed: {item}")
for item in result.warnings:
    print(f"  Note: {item}")
for item in result.errors:
    print(f"  Error: {item}")
PYEOF
else
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
  echo "  No .bedrock_agentcore.yaml found, so the toolkit's deploy resources were"
  echo "  not removed. Check for and delete by hand if present: the IAM role"
  echo "  'AmazonBedrockAgentCoreSDKRuntime-${REGION}-*' and the code zip the"
  echo "  toolkit uploaded to its S3 bucket for '${AGENT_NAME}'."
fi

# ---------------------------------------------------------------------------
# 2. AgentCore Memory resource(s) this sample created
# ---------------------------------------------------------------------------
echo "→ Deleting AgentCore Memory named '$MEMORY_NAME' ..."
# AgentCore Memory IDs are "{name}-{suffix}" — match on that exact prefix so a
# name like "multi_tenant_agentcore_memory" doesn't also match
# "multi_tenant_agentcore_memory_v2". Then require the sample tag.
MEMORY_IDS=$(aws bedrock-agentcore-control list-memories --region "$REGION" \
  --query "memories[?starts_with(id, '${MEMORY_NAME}-')].id" \
  --output text 2>/dev/null || echo "")
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [ -n "$MEMORY_IDS" ]; then
  for MID in $MEMORY_IDS; do
    MARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:memory/${MID}"
    OWNED=$(aws bedrock-agentcore-control list-tags-for-resource --resource-arn "$MARN" \
      --region "$REGION" --query "tags.sample" --output text 2>/dev/null || echo "")
    if [ "$OWNED" != "$SAMPLE_TAG_VALUE" ]; then
      echo "  Skipping memory $MID: not tagged sample=$SAMPLE_TAG_VALUE."
      continue
    fi
    echo "  Deleting memory: $MID"
    aws bedrock-agentcore-control delete-memory \
      --memory-id "$MID" --region "$REGION" >/dev/null 2>&1 || true
  done
else
  echo "  None found."
fi

# ---------------------------------------------------------------------------
# 3. IAM roles this sample created (inline policy first)
# ---------------------------------------------------------------------------
delete_owned_role() {
  local ROLE="$1"
  local POLICY="$2"
  echo "→ Deleting role '$ROLE' ..."
  if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    echo "  Not found."
    return
  fi
  local OWNED
  OWNED=$(aws iam list-role-tags --role-name "$ROLE" \
    --query "Tags[?Key=='sample'].Value | [0]" --output text 2>/dev/null || echo "")
  if [ "$OWNED" != "$SAMPLE_TAG_VALUE" ]; then
    echo "  Skipping: not tagged sample=$SAMPLE_TAG_VALUE."
    return
  fi
  aws iam delete-role-policy --role-name "$ROLE" --policy-name "$POLICY" >/dev/null 2>&1 || true
  aws iam delete-role --role-name "$ROLE" >/dev/null 2>&1 \
    && echo "  Deleted." || echo "  Could not delete (check for remaining attached policies)."
}

delete_owned_role "$TVM_ROLE_NAME" "AgentCoreMemoryTenantPolicy"
delete_owned_role "$MEMORY_EXEC_ROLE_NAME" "MemoryExtractionModelInvoke"

# ---------------------------------------------------------------------------
# 4. Cognito user pool this sample created
# ---------------------------------------------------------------------------
echo "→ Deleting Cognito user pool '$COGNITO_POOL_NAME' ..."
POOL_IDS=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='${COGNITO_POOL_NAME}'].Id" --output text 2>/dev/null || echo "")
if [ -n "$POOL_IDS" ]; then
  for PID in $POOL_IDS; do
    PARN=$(aws cognito-idp describe-user-pool --user-pool-id "$PID" --region "$REGION" \
      --query "UserPool.Arn" --output text 2>/dev/null || echo "")
    OWNED=$(aws cognito-idp list-tags-for-resource --resource-arn "$PARN" --region "$REGION" \
      --query "Tags.sample" --output text 2>/dev/null || echo "")
    if [ "$OWNED" != "$SAMPLE_TAG_VALUE" ]; then
      echo "  Skipping pool $PID: not tagged sample=$SAMPLE_TAG_VALUE."
      continue
    fi
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
echo "briefly remain in DELETING state before it fully disappears. Log groups"
echo "under /aws/bedrock-agentcore/runtimes/ are kept; delete them if you no"
echo "longer need the logs."
