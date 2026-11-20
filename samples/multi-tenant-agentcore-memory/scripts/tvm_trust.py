#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tvm_trust.py — Create the TVM role, or add principals to its trust policy.

The TVM role trusts only the principals named here (the operator who ran the
setup, and the AgentCore Runtime execution role), and only when the caller sets
exactly one session tag, TenantID. Principals already trusted are kept, so
re-running a setup script never drops the execution role.

Usage:
  python3 tvm_trust.py <role-name> <principal-arn> [<principal-arn> ...]

<principal-arn> may be an IAM role or user ARN, or an STS assumed-role ARN
(it is resolved to the role's IAM ARN, which is what aws:PrincipalArn holds).
"""

import json
import sys

import boto3

SAMPLE_TAG = {"Key": "sample", "Value": "multi-tenant-agentcore-memory"}


def resolve_principal(arn: str, iam) -> str:
    """Turn arn:aws:sts::<acct>:assumed-role/<name>/<session> into the role's IAM ARN."""
    if ":assumed-role/" in arn:
        role_name = arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
        return iam.get_role(RoleName=role_name)["Role"]["Arn"]
    return arn


def trust_policy(account_id: str, principals: list[str]) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TenantScopedAssume",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": ["sts:AssumeRole", "sts:TagSession"],
                "Condition": {
                    "ArnEquals": {"aws:PrincipalArn": sorted(principals)},
                    "Null": {"aws:RequestTag/TenantID": "false"},
                    "ForAllValues:StringEquals": {"aws:TagKeys": ["TenantID"]},
                },
            }
        ],
    }


def current_principals(role: dict) -> list[str]:
    for stmt in role.get("AssumeRolePolicyDocument", {}).get("Statement", []):
        if stmt.get("Sid") == "TenantScopedAssume":
            value = stmt.get("Condition", {}).get("ArnEquals", {}).get("aws:PrincipalArn", [])
            return [value] if isinstance(value, str) else list(value)
    return []


def ensure_trust(role_name: str, principal_arns: list[str], description: str = "") -> str:
    """Create the role or merge principals into its trust policy. Returns the role ARN."""
    iam = boto3.client("iam")
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    wanted = {resolve_principal(a, iam) for a in principal_arns}
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = None

    if role is None:
        doc = trust_policy(account_id, list(wanted))
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(doc),
            Description=description or role_name,
            Tags=[SAMPLE_TAG],
        )
        role = iam.get_role(RoleName=role_name)["Role"]
    else:
        merged = wanted | set(current_principals(role))
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(trust_policy(account_id, list(merged))),
        )
        iam.tag_role(RoleName=role_name, Tags=[SAMPLE_TAG])
    return role["Arn"]


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    print(ensure_trust(
        sys.argv[1], sys.argv[2:],
        description="TVM role for AgentCore Memory tenant isolation",
    ))
