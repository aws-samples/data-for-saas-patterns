# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""test_tvm_trust.py — Unit tests for scripts/tvm_trust.py (IAM calls mocked)."""

import json
from unittest.mock import MagicMock, patch

import pytest

import tvm_trust

ACCOUNT = "123456789012"
OPERATOR = f"arn:aws:iam::{ACCOUNT}:role/Operator"
EXEC_ROLE = f"arn:aws:iam::{ACCOUNT}:role/service-role/RuntimeExec"
TAGGED = [{"Key": "sample", "Value": "multi-tenant-agentcore-memory"}]


class _NoSuchEntity(Exception):
    pass


def _iam(existing_role=None):
    iam = MagicMock()
    iam.exceptions.NoSuchEntityException = _NoSuchEntity

    def get_role(RoleName):
        if RoleName == "Operator":
            return {"Role": {"Arn": OPERATOR}}
        if existing_role is None:
            raise _NoSuchEntity()
        return {"Role": existing_role}

    iam.get_role.side_effect = get_role
    return iam


def _patch(iam):
    return patch.object(tvm_trust, "_clients", return_value=(iam, ACCOUNT))


def _existing(principals, tags=TAGGED):
    return {
        "Arn": f"arn:aws:iam::{ACCOUNT}:role/tvm",
        "Tags": tags,
        "AssumeRolePolicyDocument": tvm_trust.trust_policy(ACCOUNT, principals),
    }


def test_trust_policy_conditions():
    stmt = tvm_trust.trust_policy(ACCOUNT, [EXEC_ROLE])["Statement"][0]
    cond = stmt["Condition"]
    assert stmt["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}
    assert cond["ArnEquals"]["aws:PrincipalArn"] == [EXEC_ROLE]
    assert cond["Null"] == {"aws:RequestTag/TenantID": "false"}
    assert cond["ForAllValues:StringEquals"] == {"aws:TagKeys": ["TenantID"]}
    assert cond["StringNotLike"]["aws:RequestTag/TenantID"] == ["*:*", "*/*"]
    assert "Service" not in json.dumps(stmt["Principal"])


def test_assumed_role_arn_is_resolved_to_role_arn():
    iam = _iam()
    arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/Operator/session-1"
    assert tvm_trust.resolve_principal(arn, iam) == OPERATOR


def test_create_role_when_missing():
    iam = _iam()
    calls = iam.get_role.side_effect

    def get_role(RoleName):
        if RoleName == "tvm" and iam.create_role.called:
            return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/tvm"}}
        return calls(RoleName)

    iam.get_role.side_effect = get_role
    with _patch(iam):
        arn = tvm_trust.ensure_trust("tvm", [f"arn:aws:sts::{ACCOUNT}:assumed-role/Operator/s"])
    assert arn.endswith(":role/tvm")
    kwargs = iam.create_role.call_args.kwargs
    assert kwargs["Tags"] == TAGGED
    doc = json.loads(kwargs["AssumeRolePolicyDocument"])
    assert doc["Statement"][0]["Condition"]["ArnEquals"]["aws:PrincipalArn"] == [OPERATOR]


def test_existing_principals_are_kept_when_adding():
    iam = _iam(_existing([OPERATOR]))
    with _patch(iam):
        tvm_trust.ensure_trust("tvm", [EXEC_ROLE])
    doc = json.loads(iam.update_assume_role_policy.call_args.kwargs["PolicyDocument"])
    assert doc["Statement"][0]["Condition"]["ArnEquals"]["aws:PrincipalArn"] == sorted([OPERATOR, EXEC_ROLE])


def test_untagged_existing_role_is_not_changed():
    iam = _iam(_existing([OPERATOR], tags=[]))
    with _patch(iam), pytest.raises(SystemExit):
        tvm_trust.ensure_trust("tvm", [EXEC_ROLE])
    iam.update_assume_role_policy.assert_not_called()


def test_remove_operator_keeps_exec_role():
    iam = _iam(_existing([OPERATOR, EXEC_ROLE]))
    with _patch(iam):
        left = tvm_trust.remove_trust("tvm", [OPERATOR])
    assert left == [EXEC_ROLE]


def test_remove_last_principal_is_refused():
    iam = _iam(_existing([EXEC_ROLE]))
    with _patch(iam), pytest.raises(SystemExit):
        tvm_trust.remove_trust("tvm", [EXEC_ROLE])
    iam.update_assume_role_policy.assert_not_called()


def test_tag_value_must_be_non_empty():
    cond = tvm_trust.trust_policy(ACCOUNT, [EXEC_ROLE])["Statement"][0]["Condition"]
    assert cond["StringLike"] == {"aws:RequestTag/TenantID": "?*"}


def test_other_trust_statements_are_kept():
    other = {"Sid": "SomethingElse", "Effect": "Allow", "Principal": {"AWS": OPERATOR}, "Action": "sts:AssumeRole"}
    existing = _existing([OPERATOR, EXEC_ROLE])
    existing["AssumeRolePolicyDocument"]["Statement"].insert(0, other)
    iam = _iam(existing)
    with _patch(iam):
        tvm_trust.remove_trust("tvm", [OPERATOR])
    doc = json.loads(iam.update_assume_role_policy.call_args.kwargs["PolicyDocument"])
    assert other in doc["Statement"]
    ours = [s for s in doc["Statement"] if s["Sid"] == tvm_trust.SID]
    assert ours[0]["Condition"]["ArnEquals"]["aws:PrincipalArn"] == [EXEC_ROLE]


def test_remove_refuses_untagged_role():
    iam = _iam(_existing([OPERATOR, EXEC_ROLE], tags=[]))
    with _patch(iam), pytest.raises(SystemExit):
        tvm_trust.remove_trust("tvm", [OPERATOR])
    iam.update_assume_role_policy.assert_not_called()


def test_single_statement_object_is_accepted():
    existing = _existing([OPERATOR])
    existing["AssumeRolePolicyDocument"]["Statement"] = existing["AssumeRolePolicyDocument"]["Statement"][0]
    assert tvm_trust.current_principals(existing) == [OPERATOR]
    iam = _iam(existing)
    with _patch(iam):
        tvm_trust.ensure_trust("tvm", [EXEC_ROLE])
    doc = json.loads(iam.update_assume_role_policy.call_args.kwargs["PolicyDocument"])
    assert len(doc["Statement"]) == 1
