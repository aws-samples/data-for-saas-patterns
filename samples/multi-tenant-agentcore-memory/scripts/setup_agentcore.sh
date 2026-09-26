#!/usr/bin/env bash
# setup_agentcore.sh -- Deploys the multi-tenant agent example to AgentCore Runtime.
#
# Usage: bash setup_agentcore.sh
#
# Required env vars:
#   AGENTCORE_MEMORY_ID
#   AGENTCORE_TVM_ROLE_ARN
#   AGENTCORE_MEMORY_ARN     (or it is derived from AGENTCORE_MEMORY_ID)
#   COGNITO_USER_POOL_ID
#   COGNITO_CLIENT_ID
#   AWS_REGION               (default: us-east-1)
#
# Optional env vars:
#   EXAMPLE_FILE             (default: multi_tenant_agent.py)
#   AGENT_NAME               (default: multi_tenant_agentcore_memory_agent)
#
# Outputs:
#   export AGENT_ARN=arn:aws:bedrock-agentcore:...
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
: "${AGENTCORE_MEMORY_ID:?Set AGENTCORE_MEMORY_ID before running}"
: "${AGENTCORE_TVM_ROLE_ARN:?Set AGENTCORE_TVM_ROLE_ARN before running}"
: "${COGNITO_USER_POOL_ID:?Set COGNITO_USER_POOL_ID before running}"
: "${COGNITO_CLIENT_ID:?Set COGNITO_CLIENT_ID before running}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
MEMORY_ARN="${AGENTCORE_MEMORY_ARN:-arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:memory/${AGENTCORE_MEMORY_ID}}"

EXAMPLE_FILE="${EXAMPLE_FILE:-multi_tenant_agent.py}"
AGENT_NAME="${AGENT_NAME:-multi_tenant_agentcore_memory_agent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLES_DIR="$(cd "$SCRIPT_DIR/../examples" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/../src" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Remove any stale toolkit config. It pins the agent_id from a previous deploy;
# if that runtime was deleted, the toolkit tries to UPDATE a runtime that no
# longer exists and fails with ResourceNotFoundException. Deleting it forces a
# fresh CREATE.
rm -rf "$REPO_ROOT/.bedrock_agentcore.yaml" "$REPO_ROOT/.bedrock_agentcore"

python3 - <<PYEOF
import os, sys
from pathlib import Path

sys.path.insert(0, "$SRC_DIR")

from bedrock_agentcore_starter_toolkit.operations.runtime import (
    configure_bedrock_agentcore,
    launch_bedrock_agentcore,
)

REGION       = "$REGION"
POOL_ID      = "$COGNITO_USER_POOL_ID"
CLIENT_ID    = "$COGNITO_CLIENT_ID"
TVM_ARN      = "$AGENTCORE_TVM_ROLE_ARN"
MEMORY_ID    = "$AGENTCORE_MEMORY_ID"
MEMORY_ARN   = "$MEMORY_ARN"
AGENT_NAME   = "$AGENT_NAME"
EXAMPLES     = Path("$EXAMPLES_DIR")
EXAMPLE_FILE = "$EXAMPLE_FILE"

DISCOVERY_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}/.well-known/openid-configuration"

print(f"Configuring {AGENT_NAME} (entrypoint: {EXAMPLE_FILE}) ...")
configure_bedrock_agentcore(
    agent_name=AGENT_NAME,
    entrypoint_path=EXAMPLES / EXAMPLE_FILE,
    source_path=str(Path("$REPO_ROOT")),   # include src/ alongside examples/
    authorizer_configuration={
        "customJWTAuthorizer": {
            "discoveryUrl": DISCOVERY_URL,
            "allowedAudience": [CLIENT_ID],
        }
    },
    request_header_configuration={"requestHeaderAllowlist": ["Authorization"]},
    region=REGION,
    non_interactive=True,
    runtime_type="PYTHON_3_13",
)

print(f"Launching {AGENT_NAME} (CodeBuild) ...")
result = launch_bedrock_agentcore(
    config_path=Path("$REPO_ROOT") / ".bedrock_agentcore.yaml",
    agent_name=AGENT_NAME,
    use_codebuild=True,
    auto_update_on_conflict=True,
    env_vars={
        "AGENTCORE_MEMORY_ID": MEMORY_ID,
        "AGENTCORE_TVM_ROLE_ARN": TVM_ARN,
        "AWS_REGION": REGION,
    },
)

print(f"Deployed: agent_id={result.agent_id}")
print(f"  export AGENT_ARN={result.agent_arn}")

# Attach Bedrock + memory + STS permissions to the execution role.
import boto3, json
ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
agent_detail   = ctrl.get_agent_runtime(agentRuntimeId=result.agent_id)
exec_role_arn  = agent_detail.get("roleArn", "")
exec_role_name = exec_role_arn.split("/")[-1]

if exec_role_name:
    iam = boto3.client("iam")
    iam.put_role_policy(
        RoleName=exec_role_name,
        PolicyName="AgentCoreMemoryAndTVMPolicy",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "BedrockInvoke",
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                               "bedrock:Converse", "bedrock:ConverseStream"],
                    "Resource": "*",
                },
                {
                    "Sid": "STSAssumeAndTagTVMRole",
                    "Effect": "Allow",
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                    "Resource": TVM_ARN,
                },
                {
                    "Sid": "AgentCoreMemory",
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent",
                               "bedrock-agentcore:ListEvents", "bedrock-agentcore:RetrieveMemoryRecords",
                               "bedrock-agentcore:ListMemoryRecords"],
                    "Resource": MEMORY_ARN,
                },
            ]
        })
    )
    print(f"Attached permissions to {exec_role_name}")

    # Allow the execution role to assume the TVM role.
    account_id    = boto3.client("sts").get_caller_identity()["Account"]
    tvm_role_name = TVM_ARN.split("/")[-1]
    iam.update_assume_role_policy(
        RoleName=tvm_role_name,
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": [f"arn:aws:iam::{account_id}:root", exec_role_arn]},
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                },
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                },
            ],
        })
    )
    print(f"TVM trust policy updated to allow {exec_role_name}")
PYEOF
