#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# setup_agentcore.sh -- Deploys the multi-tenant agent example to AgentCore Runtime.
#
# Usage: bash setup_agentcore.sh
#
# Required env vars:
#   AGENTCORE_MEMORY_ID
#   AGENTCORE_TVM_ROLE_ARN
#   COGNITO_USER_POOL_ID
#   COGNITO_CLIENT_ID
#   AWS_REGION               (default: us-east-1)
#
# Optional env vars:
#   EXAMPLE_FILE             (default: multi_tenant_agent.py)
#   AGENT_NAME               (default: multi_tenant_agentcore_memory_agent)
#   BEDROCK_MODEL_ID         (default: amazon.nova-pro-v1:0)
#
# Outputs:
#   export AGENT_ARN=arn:aws:bedrock-agentcore:...
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
: "${AGENTCORE_MEMORY_ID:?Set AGENTCORE_MEMORY_ID before running}"
: "${AGENTCORE_TVM_ROLE_ARN:?Set AGENTCORE_TVM_ROLE_ARN before running}"
: "${COGNITO_USER_POOL_ID:?Set COGNITO_USER_POOL_ID before running}"
: "${COGNITO_CLIENT_ID:?Set COGNITO_CLIENT_ID before running}"

export AGENTCORE_MEMORY_ID AGENTCORE_TVM_ROLE_ARN COGNITO_USER_POOL_ID COGNITO_CLIENT_ID
export EXAMPLE_FILE="${EXAMPLE_FILE:-multi_tenant_agent.py}"
export AGENT_NAME="${AGENT_NAME:-multi_tenant_agentcore_memory_agent}"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-amazon.nova-pro-v1:0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SCRIPT_DIR
export REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Remove any stale toolkit config. It pins the agent_id from a previous deploy;
# if that runtime was deleted, the toolkit tries to UPDATE a runtime that no
# longer exists and fails with ResourceNotFoundException. Deleting it forces a
# fresh CREATE.
rm -rf "$REPO_ROOT/.bedrock_agentcore.yaml" "$REPO_ROOT/.bedrock_agentcore"

# Quoted heredoc: every value is read from the environment, never pasted into
# the Python source.
python3 - <<'PYEOF'
import json
import os
import sys
from pathlib import Path

import boto3

env = os.environ
REPO_ROOT    = Path(env["REPO_ROOT"])
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, env["SCRIPT_DIR"])

from bedrock_agentcore_starter_toolkit.operations.runtime import (
    configure_bedrock_agentcore,
    launch_bedrock_agentcore,
)
from tvm_trust import ensure_trust

REGION       = env["AWS_REGION"]
POOL_ID      = env["COGNITO_USER_POOL_ID"]
CLIENT_ID    = env["COGNITO_CLIENT_ID"]
TVM_ARN      = env["AGENTCORE_TVM_ROLE_ARN"]
MEMORY_ID    = env["AGENTCORE_MEMORY_ID"]
AGENT_NAME   = env["AGENT_NAME"]
MODEL_ID     = env["BEDROCK_MODEL_ID"]
EXAMPLES     = REPO_ROOT / "examples"
EXAMPLE_FILE = env["EXAMPLE_FILE"]

DISCOVERY_URL = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}/.well-known/openid-configuration"

print(f"Configuring {AGENT_NAME} (entrypoint: {EXAMPLE_FILE}) ...")
configure_bedrock_agentcore(
    agent_name=AGENT_NAME,
    entrypoint_path=EXAMPLES / EXAMPLE_FILE,
    source_path=str(REPO_ROOT),   # include src/ alongside examples/
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
    config_path=REPO_ROOT / ".bedrock_agentcore.yaml",
    agent_name=AGENT_NAME,
    use_codebuild=True,
    auto_update_on_conflict=True,
    env_vars={
        "AGENTCORE_MEMORY_ID": MEMORY_ID,
        "AGENTCORE_TVM_ROLE_ARN": TVM_ARN,
        "AWS_REGION": REGION,
        "BEDROCK_MODEL_ID": MODEL_ID,
    },
)

print(f"Deployed: agent_id={result.agent_id}")
print(f"  export AGENT_ARN={result.agent_arn}")

# Execution role permissions: invoke the configured model and assume the TVM
# role. The execution role gets NO AgentCore Memory permissions: every memory
# call, including the start-up placeholder, is signed with TVM credentials.
ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
exec_role_arn  = ctrl.get_agent_runtime(agentRuntimeId=result.agent_id).get("roleArn", "")
exec_role_name = exec_role_arn.split("/")[-1]
if not exec_role_name:
    sys.exit("ERROR: could not read the runtime execution role")

account_id = boto3.client("sts").get_caller_identity()["Account"]
# A geography inference profile id ("us.amazon.nova-pro-v1:0") routes to the
# base model in several Regions; allow both the profile and the base model.
base_model = MODEL_ID.split(".", 1)[1] if MODEL_ID.split(".", 1)[0] in {"us", "eu", "apac", "global"} else MODEL_ID
model_resources = [
    f"arn:aws:bedrock:*::foundation-model/{base_model}",
    f"arn:aws:bedrock:{REGION}:{account_id}:inference-profile/{MODEL_ID}",
]

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
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": model_resources,
            },
            {
                "Sid": "STSAssumeAndTagTVMRole",
                "Effect": "Allow",
                "Action": ["sts:AssumeRole", "sts:TagSession"],
                "Resource": TVM_ARN,
            },
        ],
    }),
)
print(f"Attached permissions to {exec_role_name}")

# Allow the execution role to assume the TVM role (keeps already-trusted principals).
ensure_trust(TVM_ARN.split("/")[-1], [exec_role_arn])
print(f"TVM trust policy updated to allow {exec_role_name}")
PYEOF
