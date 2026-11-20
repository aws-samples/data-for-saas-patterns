#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# setup_tvm_role.sh — Creates the AgentCore Memory TVM IAM role
# Usage: bash setup_tvm_role.sh <memory-arn>
#
# <memory-arn> looks like:
#   arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/<memory-id>
set -euo pipefail

MEMORY_ARN="${1:?Usage: bash setup_tvm_role.sh <memory-arn>}"
ROLE_NAME="agentcore-memory-tvm-role"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "→ Account : $ACCOUNT_ID"
echo "→ Region  : $REGION"
echo "→ Memory  : $MEMORY_ARN"
echo "→ Role    : $ROLE_NAME"
echo ""

# ---------------------------------------------------------------------------
# Trust policy — the role trusts only named principals, and only when they set
# exactly one session tag, TenantID. This script adds the identity running it
# (so you can run examples/prove_isolation.py); setup_agentcore.sh adds the
# AgentCore Runtime execution role. Nobody else in the account can assume it.
# ---------------------------------------------------------------------------
echo "→ Creating or updating role and trust policy (trusting $CALLER_ARN) ..."
ROLE_ARN=$(python3 "$SCRIPT_DIR/tvm_trust.py" "$ROLE_NAME" "$CALLER_ARN")

# ---------------------------------------------------------------------------
# Memory policy — scoped to the TenantID session tag via ABAC conditions.
# The actorId condition restricts short-term event operations (STM); the
# namespacePath condition restricts long-term memory retrieval (LTM). A session
# tagged TenantID=tenant-002 can therefore never touch an actorId starting with
# "tenant-001:" nor retrieve records under tenant-001's namespaces.
#
# DeleteEvent is included so the SDK's message redaction works; it carries the
# same actorId condition.
#
# IMPORTANT: LTM retrieval must condition on bedrock-agentcore:namespacePath
# (prefix retrieval), NOT bedrock-agentcore:namespace. The SDK's
# RetrieveMemoryRecords call sends a namespacePath (prefix) request; if this
# conditions on `namespace` instead, IAM never matches, returns
# AccessDeniedException, the SDK swallows it, and the agent silently behaves as
# if it has no long-term memory. STM (actorId) is unaffected.
# ---------------------------------------------------------------------------
MEMORY_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:GetEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:DeleteEvent"
      ],
      "Resource": "${MEMORY_ARN}",
      "Condition": {
        "StringLike": {
          "bedrock-agentcore:actorId": "\${aws:PrincipalTag/TenantID}:*"
        }
      }
    },
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:ListMemoryRecords"
      ],
      "Resource": "${MEMORY_ARN}",
      "Condition": {
        "StringLike": {
          "bedrock-agentcore:namespacePath": "/\${aws:PrincipalTag/TenantID}:*"
        }
      }
    }
  ]
}
EOF
)

echo "→ Attaching memory policy ..."
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "AgentCoreMemoryTenantPolicy" \
  --policy-document "$MEMORY_POLICY"

echo ""
echo "✓ Done. Set these environment variables before running the demo:"
echo ""
echo "  export AGENTCORE_TVM_ROLE_ARN=${ROLE_ARN}"
echo ""
