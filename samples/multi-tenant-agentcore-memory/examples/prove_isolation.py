#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
prove_isolation.py — Prove IAM-enforced tenant memory isolation.

This script calls the AgentCore Memory API directly with TVM-scoped credentials
to show that tenant-002's credentials are denied by IAM when they ask for
tenant-001's short-term or long-term memory. The check is IAM authorisation on
the AgentCore Memory data plane, not application filtering: code that builds a
wrong actorId or namespace is denied by AWS.

What this does not prove: IAM trusts the TenantID tag the TVM sets, so it cannot
catch a wrong tenant id coming in. That is why the tenant id must come from an
attribute the user cannot change (see scripts/setup_cognito.sh).

Checks:
  1. tenant-001 lists its own events                     -> allowed
  2. tenant-002 lists tenant-001's events                -> AccessDeniedException
  3. tenant-002 writes an event as a tenant-001 actor    -> AccessDeniedException
  4. tenant-002 retrieves tenant-001's long-term records -> AccessDeniedException

Exits 0 only if every check behaves as expected.

Run it with the same AWS identity that ran scripts/setup_tvm_role.sh: the TVM
role trusts that identity and the runtime execution role only.

Usage:
  export AGENTCORE_MEMORY_ID=...
  export AGENTCORE_TVM_ROLE_ARN=...
  export AWS_REGION=us-east-1
  python3 examples/prove_isolation.py
"""

import os
import sys
from datetime import datetime, timezone

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agentcore_memory import AGENT_NAME, TokenVendingMachine  # noqa: E402

REGION       = os.environ.get("AWS_REGION", "us-east-1")
TVM_ROLE_ARN = os.environ.get("AGENTCORE_TVM_ROLE_ARN", "")
MEMORY_ID    = os.environ.get("AGENTCORE_MEMORY_ID", "")
USER_ID      = os.environ.get("TEST_USER_ID", "user-001")

if not TVM_ROLE_ARN or not MEMORY_ID:
    print("ERROR: set AGENTCORE_TVM_ROLE_ARN and AGENTCORE_MEMORY_ID")
    sys.exit(1)

# Same actor_id / session_id / namespace format as the session manager.
ACTOR_ID_T1     = f"tenant-001:{AGENT_NAME}:{USER_ID}"
SESSION_ID_T1   = f"tenant-001-{AGENT_NAME}-{USER_ID}-conv-001"
NAMESPACE_T1    = f"/{ACTOR_ID_T1}/"

tvm = TokenVendingMachine(role_arn=TVM_ROLE_ARN, region_name=REGION)


def _client(tenant_id: str):
    return tvm.get_client("bedrock-agentcore", {"tenantId": tenant_id})


def _expect_allowed(name, call):
    try:
        call()
        print(f"  ✓ {name}: allowed")
        return True
    except Exception as e:
        print(f"  ✗ {name}: expected success, got {type(e).__name__}: {e}")
        return False


def _expect_denied(name, call):
    try:
        call()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "AccessDeniedException":
            print(f"  ✓ {name}: denied by IAM (AccessDeniedException)")
            return True
        print(f"  ✗ {name}: expected AccessDeniedException, got {code}: {e}")
        return False
    except Exception as e:
        print(f"  ✗ {name}: expected AccessDeniedException, got {type(e).__name__}: {e}")
        return False
    print(f"  ✗ {name}: ISOLATION FAILURE — the call succeeded. Check the TVM role policy.")
    return False


print("=" * 64)
print("  Tenant Memory Isolation Proof")
print("=" * 64)

results = [
    _expect_allowed(
        "1. tenant-001 lists its own events",
        lambda: _client("tenant-001").list_events(
            memoryId=MEMORY_ID, actorId=ACTOR_ID_T1, sessionId=SESSION_ID_T1,
        ),
    ),
    _expect_denied(
        "2. tenant-002 lists tenant-001's events",
        lambda: _client("tenant-002").list_events(
            memoryId=MEMORY_ID, actorId=ACTOR_ID_T1, sessionId=SESSION_ID_T1,
        ),
    ),
    _expect_denied(
        "3. tenant-002 writes an event as tenant-001",
        lambda: _client("tenant-002").create_event(
            memoryId=MEMORY_ID, actorId=ACTOR_ID_T1, sessionId=SESSION_ID_T1,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[{"conversational": {"content": {"text": "isolation probe"}, "role": "USER"}}],
        ),
    ),
    _expect_denied(
        "4. tenant-002 retrieves tenant-001's long-term records",
        lambda: _client("tenant-002").retrieve_memory_records(
            memoryId=MEMORY_ID, namespacePath=NAMESPACE_T1,
            searchCriteria={"searchQuery": "preferences"},
        ),
    ),
]

passed = sum(results)
print()
print(f"-- Summary: {passed}/{len(results)} checks as expected --")
if passed != len(results):
    print("  ISOLATION NOT PROVEN — see the ✗ lines above.")
    sys.exit(1)
print("  Cross-tenant reads and writes are denied by IAM.")
