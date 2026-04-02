#!/usr/bin/env bash
# setup_agentcore.sh — Thin wrapper; actual deploy logic is in the test script
# using bedrock_agentcore_starter_toolkit Python API directly.
#
# This script is kept for manual use outside the test suite.
# Usage: bash setup_agentcore.sh
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
: "${S3_VECTOR_BUCKET_NAME:?Set S3_VECTOR_BUCKET_NAME before running}"
: "${S3_VECTOR_TVM_ROLE_ARN:?Set S3_VECTOR_TVM_ROLE_ARN before running}"
: "${COGNITO_USER_POOL_ID:?Set COGNITO_USER_POOL_ID before running}"
: "${COGNITO_CLIENT_ID:?Set COGNITO_CLIENT_ID before running}"

python3 - <<PYEOF
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bedrock_agentcore_starter_toolkit.operations.runtime import (
    configure_bedrock_agentcore,
    launch_bedrock_agentcore,
)

CODE_DIR   = Path(__file__).parent
REGION     = os.environ.get("AWS_REGION", "us-east-1")
POOL_ID    = os.environ["COGNITO_USER_POOL_ID"]
CLIENT_ID  = os.environ["COGNITO_CLIENT_ID"]
TVM_ARN    = os.environ["S3_VECTOR_TVM_ROLE_ARN"]
BUCKET     = os.environ["S3_VECTOR_BUCKET_NAME"]
AGENT_NAME = "blog_s3_vector_memory_agent"

DISCOVERY_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}/.well-known/openid-configuration"

print(f"→ Configuring {AGENT_NAME} ...")
configure_bedrock_agentcore(
    agent_name=AGENT_NAME,
    entrypoint_path=CODE_DIR / "agent.py",
    requirements_file=str(CODE_DIR / "requirements.txt"),
    authorizer_configuration={
        "customJWTAuthorizer": {
            "discoveryUrl": DISCOVERY_URL,
            "allowedAudience": [CLIENT_ID],
        }
    },
    request_header_configuration={"requestHeaderAllowlist": ["Authorization"]},
    region=REGION,
)

print(f"→ Launching {AGENT_NAME} (CodeBuild) ...")
result = launch_bedrock_agentcore(
    config_path=CODE_DIR / ".bedrock_agentcore.yaml",
    agent_name=AGENT_NAME,
    use_codebuild=True,
    auto_update_on_conflict=True,
    env_vars={
        "S3_VECTOR_BUCKET_NAME": BUCKET,
        "S3_VECTOR_TVM_ROLE_ARN": TVM_ARN,
        "AWS_REGION": REGION,
    },
)

print(f"✓ Deployed: agent_id={result.agent_id}")
print(f"  export AGENT_ARN={result.agent_arn}")

# Attach Bedrock + STS permissions to the execution role
import boto3, json
ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
agent_detail = ctrl.get_agent_runtime(agentRuntimeId=result.agent_id)
exec_role_arn = agent_detail.get("roleArn", "")
exec_role_name = exec_role_arn.split("/")[-1]

if exec_role_name:
    iam = boto3.client("iam")
    iam.put_role_policy(
        RoleName=exec_role_name,
        PolicyName="BlogAgentBedrockAndTVMPolicy",
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
                }
            ]
        })
    )
    print(f"✓ Attached BedrockInvoke + STS permissions to {exec_role_name}")

    # Update TVM role trust policy to allow the execution role to AssumeRole+TagSession
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
    print(f"✓ TVM trust policy updated to allow {exec_role_name}")
PYEOF
