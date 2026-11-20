#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tvm_trust.py — Create the TVM role, or add or remove principals in its trust policy.

The TVM role trusts only the principals named here (the operator who ran the
setup, and the AgentCore Runtime execution role), and only when the caller sets
exactly one session tag, TenantID, whose value holds no ':' or '/' (the actorId
and namespace separators). Principals already trusted are kept, so re-running a
setup script never drops the execution role.

Usage:
  python3 tvm_trust.py <role-name> <principal-arn> [<principal-arn> ...]
  python3 tvm_trust.py --remove <role-name> <principal-arn> [<principal-arn> ...]

<principal-arn> may be an IAM role or user ARN, or an STS assumed-role ARN
(it is resolved to the role's IAM ARN, which is what aws:PrincipalArn holds).
Use --remove to take your own identity off the trust policy once you have run
examples/prove_isolation.py.
"""

import json
import sys

import boto3

SAMPLE_TAG = {"Key": "sample", "Value": "multi-tenant-agentcore-memory"}
SID = "TenantScopedAssume"


def resolve_principal(arn: str, iam) -> str:
    """Turn arn:aws:sts::<acct>:assumed-role/<name>/<session> into the role's IAM ARN."""
    if ":assumed-role/" in arn:
        role_name = arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
        return iam.get_role(RoleName=role_name)["Role"]["Arn"]
    return arn


def trust_policy(account_id: str, principals: list) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": SID,
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": ["sts:AssumeRole", "sts:TagSession"],
                "Condition": {
                    "ArnEquals": {"aws:PrincipalArn": sorted(principals)},
                    "Null": {"aws:RequestTag/TenantID": "false"},
                    "ForAllValues:StringEquals": {"aws:TagKeys": ["TenantID"]},
                    "StringNotLike": {"aws:RequestTag/TenantID": ["*:*", "*/*"]},
                },
            }
        ],
    }


def current_principals(role: dict) -> list:
    for stmt in role.get("AssumeRolePolicyDocument", {}).get("Statement", []):
        if stmt.get("Sid") == SID:
            value = stmt.get("Condition", {}).get("ArnEquals", {}).get("aws:PrincipalArn", [])
            return [value] if isinstance(value, str) else list(value)
    return []


def _clients():
    return boto3.client("iam"), boto3.client("sts").get_caller_identity()["Account"]


def ensure_trust(role_name: str, principal_arns: list, description: str = "") -> str:
    """Create the role or merge principals into its trust policy. Returns the role ARN."""
    iam, account_id = _clients()
    wanted = {resolve_principal(a, iam) for a in principal_arns}
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = None

    if role is None:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy(account_id, list(wanted))),
            Description=description or role_name,
            Tags=[SAMPLE_TAG],
        )
        return iam.get_role(RoleName=role_name)["Role"]["Arn"]

    tags = {t["Key"]: t["Value"] for t in role.get("Tags", [])}
    if tags.get(SAMPLE_TAG["Key"]) != SAMPLE_TAG["Value"]:
        raise SystemExit(
            f"ERROR: role {role_name} exists but was not created by this sample "
            f"(no tag {SAMPLE_TAG['Key']}={SAMPLE_TAG['Value']}). Not changing it."
        )
    merged = wanted | set(current_principals(role))
    iam.update_assume_role_policy(
        RoleName=role_name,
        PolicyDocument=json.dumps(trust_policy(account_id, list(merged))),
    )
    return role["Arn"]


def remove_trust(role_name: str, principal_arns: list) -> list:
    """Remove principals from the trust policy. Returns the principals left."""
    iam, account_id = _clients()
    role = iam.get_role(RoleName=role_name)["Role"]
    drop = {resolve_principal(a, iam) for a in principal_arns}
    left = [p for p in current_principals(role) if p not in drop]
    if not left:
        raise SystemExit("ERROR: that would leave no trusted principal; delete the role instead.")
    iam.update_assume_role_policy(
        RoleName=role_name,
        PolicyDocument=json.dumps(trust_policy(account_id, left)),
    )
    return left


if __name__ == "__main__":
    args = sys.argv[1:]
    remove = bool(args) and args[0] == "--remove"
    if remove:
        args = args[1:]
    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    if remove:
        for p in remove_trust(args[0], args[1:]):
            print(f"still trusted: {p}")
    else:
        print(ensure_trust(
            args[0], args[1:],
            description="TVM role for AgentCore Memory tenant isolation",
        ))
