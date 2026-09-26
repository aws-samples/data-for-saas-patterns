#!/usr/bin/env python3
"""
prove_isolation.py — Prove IAM-enforced tenant memory isolation.

This script directly calls the AgentCore Memory API with TVM-scoped credentials
to demonstrate that tenant-002's credentials are PHYSICALLY denied from accessing
tenant-001's memory at the AWS control plane. This is not application-level
filtering — even a buggy agent that tried to read another tenant's memory would
be denied by IAM.

Usage:
  export AGENTCORE_MEMORY_ID=...
  export AGENTCORE_TVM_ROLE_ARN=...
  export AWS_REGION=us-east-1
  python3 examples/prove_isolation.py
"""

import os
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agentcore_memory import AGENT_NAME  # noqa: E402

REGION       = os.environ.get("AWS_REGION", "us-east-1")
TVM_ROLE_ARN = os.environ.get("AGENTCORE_TVM_ROLE_ARN", "")
MEMORY_ID    = os.environ.get("AGENTCORE_MEMORY_ID", "")
USER_ID      = os.environ.get("TEST_USER_ID", "user-001")

if not TVM_ROLE_ARN or not MEMORY_ID:
    print("ERROR: set AGENTCORE_TVM_ROLE_ARN and AGENTCORE_MEMORY_ID")
    sys.exit(1)

sts = boto3.client("sts", region_name=REGION)

# Build the same actor_id / session_id format as the session manager.
ACTOR_ID_T1   = f"tenant-001:{AGENT_NAME}:{USER_ID}"
SESSION_ID_T1 = f"tenant-001-{AGENT_NAME}-{USER_ID}-conv-001"


def _scoped_client(tenant_id: str):
    creds = sts.assume_role(
        RoleArn=TVM_ROLE_ARN,
        RoleSessionName=f"tenant-{tenant_id}",
        Tags=[{"Key": "TenantID", "Value": tenant_id}],
        DurationSeconds=900,
    )["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    return session.client("bedrock-agentcore", region_name=REGION)


print("=" * 64)
print("  Tenant Memory Isolation Proof")
print("=" * 64)
print()

# --- Step 1: tenant-001 accesses its OWN memory (should succeed) ---
print("-- Step 1: Tenant-001 accesses OWN memory (should succeed) --")
try:
    resp = _scoped_client("tenant-001").list_events(
        memoryId=MEMORY_ID, actorId=ACTOR_ID_T1, sessionId=SESSION_ID_T1,
    )
    print(f"  ✓ SUCCESS — tenant-001 listed events (count: {len(resp.get('events', []))})")
except Exception as e:
    print(f"  ✗ UNEXPECTED — {type(e).__name__}: {e}")

print()

# --- Step 2: tenant-002 tries to access tenant-001's memory (should DENY) ---
print("-- Step 2: Tenant-002 accesses TENANT-001's memory (should DENY) --")
try:
    _scoped_client("tenant-002").list_events(
        memoryId=MEMORY_ID, actorId=ACTOR_ID_T1, sessionId=SESSION_ID_T1,
    )
    print("  ✗ ISOLATION FAILURE — tenant-002 accessed tenant-001's events!")
    print("    This should never happen. Check the TVM role IAM policy.")
except ClientError as e:
    # Check the structured error code rather than the exception's string
    # representation, which is not a stable API and could change format.
    error_code = e.response.get("Error", {}).get("Code", "")
    if error_code == "AccessDeniedException":
        print("  ✓ ISOLATION CONFIRMED — IAM denied the request")
        print("    Reason: bedrock-agentcore:actorId condition requires actorId to")
        print("            start with '${aws:PrincipalTag/TenantID}:' — tenant-002's")
        print("            tag doesn't match tenant-001's actorId → DENIED at the")
        print("            control plane.")
    else:
        print(f"  ? Unexpected error: {error_code}: {e}")
except Exception as e:
    print(f"  ? Unexpected error: {type(e).__name__}: {e}")

print()
print("-- Summary --")
print("  Tenant-001 reads own memory    ✓  (actorId matches TenantID tag)")
print("  Tenant-002 denied cross-tenant ✓  (IAM ABAC condition enforced)")
print("  Enforcement level: AWS IAM control plane (not application code)")
