#!/usr/bin/env bash
# setup_agentcore.sh -- Deploys a multi-tenant agent example to AgentCore Runtime.
#
# Usage: bash setup_agentcore.sh
#
# Required env vars:
#   S3_VECTOR_BUCKET_NAME
#   S3_VECTOR_TVM_ROLE_ARN
#   COGNITO_USER_POOL_ID
#   COGNITO_CLIENT_ID
#   AWS_REGION              (default: us-east-1)
#
# Optional env vars:
#   EXAMPLE_FILE            (default: multi_tenant_agent.py)
#   AGENT_NAME              (default: strands_s3_vector_memory_agent)
#
# Outputs:
#   export AGENT_ARN=arn:aws:bedrock-agentcore:...
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
: "${S3_VECTOR_BUCKET_NAME:?Set S3_VECTOR_BUCKET_NAME before running}"
: "${S3_VECTOR_TVM_ROLE_ARN:?Set S3_VECTOR_TVM_ROLE_ARN before running}"
: "${COGNITO_USER_POOL_ID:?Set COGNITO_USER_POOL_ID before running}"
: "${COGNITO_CLIENT_ID:?Set COGNITO_CLIENT_ID before running}"

EXAMPLE_FILE="${EXAMPLE_FILE:-multi_tenant_agent.py}"
AGENT_NAME="${AGENT_NAME:-strands_s3_vector_memory_agent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLES_DIR="$(cd "$SCRIPT_DIR/../examples" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/../src" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

python3 - <<PYEOF
import os, sys
from pathlib import Path

sys.path.insert(0, "$SRC_DIR")

from bedrock_agentcore_starter_toolkit.operations.runtime import (
    configure_bedrock_agentcore,
    launch_bedrock_agentcore,
)

REGION     = "$REGION"
POOL_ID    = "$COGNITO_USER_POOL_ID"
CLIENT_ID  = "$COGNITO_CLIENT_ID"
TVM_ARN    = "$S3_VECTOR_TVM_ROLE_ARN"
BUCKET     = "$S3_VECTOR_BUCKET_NAME"
AGENT_NAME = "$AGENT_NAME"
EXAMPLES   = Path("$EXAMPLES_DIR")
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
    runtime_type="PYTHON_3_12",
)

print(f"Launching {AGENT_NAME} (CodeBuild) ...")
result = launch_bedrock_agentcore(
    config_path=Path("$REPO_ROOT") / ".bedrock_agentcore.yaml",
    agent_name=AGENT_NAME,
    use_codebuild=True,
    auto_update_on_conflict=True,
    env_vars={
        "S3_VECTOR_BUCKET_NAME": BUCKET,
        "S3_VECTOR_TVM_ROLE_ARN": TVM_ARN,
        "AWS_REGION": REGION,
    },
)

print(f"Deployed: agent_id={result.agent_id}")
print(f"  export AGENT_ARN={result.agent_arn}")

# Attach Bedrock + S3 + STS permissions to the execution role
import boto3, json
ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
agent_detail = ctrl.get_agent_runtime(agentRuntimeId=result.agent_id)
exec_role_arn  = agent_detail.get("roleArn", "")
exec_role_name = exec_role_arn.split("/")[-1]

if exec_role_name:
    iam = boto3.client("iam")
    iam.put_role_policy(
        RoleName=exec_role_name,
        PolicyName="AgentCoreBedrockAndTVMPolicy",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "BedrockInvoke",
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    "Resource": "*"
                },
                {
                    "Sid": "STSAssumeAndTagTVMRole",
                    "Effect": "Allow",
                    "Action": ["sts:AssumeRole", "sts:TagSession"],
                    "Resource": TVM_ARN,
                },
            ]
        })
    )
    print(f"Attached permissions to {exec_role_name}")

    # Update TVM role trust policy to allow the execution role
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    tvm_role_name = TVM_ARN.split("/")[-1]
    iam.update_assume_role_policy(
        RoleName=tvm_role_name,
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": [
                        f"arn:aws:iam::{account_id}:root",
                        exec_role_arn,
                    ]},
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
