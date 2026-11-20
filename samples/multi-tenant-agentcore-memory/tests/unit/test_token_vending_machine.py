# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
test_token_vending_machine.py — Unit tests for TokenVendingMachine.

All boto3 calls are patched at the token_vending_machine module level.
No real AWS calls are made.
"""

import os
import pytest
from unittest.mock import MagicMock, patch

from agentcore_memory.token_vending_machine import (
    BOOTSTRAP_TENANT_ID,
    IsolationError,
    TokenVendingMachine,
    validate_tenant_id,
)

VALID_ARN = "arn:aws:iam::123:role/R"

STS_CREDENTIALS = {
    "AccessKeyId": "AKIA_TEST",
    "SecretAccessKey": "secret",  # pragma: allowlist secret — test fixture, not a credential
    "SessionToken": "token",
}


def _make_sts_mock():
    sts = MagicMock()
    sts.assume_role.return_value = {"Credentials": STS_CREDENTIALS}
    return sts


class TestTVMConstruction:
    def test_empty_role_arn_raises_isolation_error(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client"):
            with pytest.raises(IsolationError, match="role_arn"):
                TokenVendingMachine(role_arn="")

    def test_none_role_arn_raises_isolation_error(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client"):
            with pytest.raises(IsolationError):
                TokenVendingMachine(role_arn=None)

    def test_valid_arn_stores_role_arn_and_creates_sts_client(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client:
            mock_boto_client.return_value = _make_sts_mock()
            tvm = TokenVendingMachine(role_arn=VALID_ARN)
            assert tvm.role_arn == VALID_ARN
            mock_boto_client.assert_called_once_with("sts", region_name="us-east-1")

    def test_region_defaults_to_aws_region_env_var(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client"):
            with patch.dict(os.environ, {"AWS_REGION": "eu-west-1"}):
                tvm = TokenVendingMachine(role_arn=VALID_ARN)
                assert tvm.region_name == "eu-west-1"

    def test_explicit_region_name_is_used(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client"):
            tvm = TokenVendingMachine(role_arn=VALID_ARN, region_name="ap-southeast-1")
            assert tvm.region_name == "ap-southeast-1"


class TestTVMGetSession:
    def _make_tvm(self, mock_boto_client):
        mock_sts = _make_sts_mock()
        mock_boto_client.return_value = mock_sts
        return TokenVendingMachine(role_arn=VALID_ARN), mock_sts

    def test_first_call_invokes_assume_role_with_correct_args(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("agentcore_memory.token_vending_machine.boto3.Session"):
            tvm, mock_sts = self._make_tvm(mock_boto_client)
            tvm.get_session({"tenantId": "tenant-001"})
            mock_sts.assume_role.assert_called_once_with(
                RoleArn=VALID_ARN,
                RoleSessionName="tenant-tenant-001",
                Tags=[{"Key": "TenantID", "Value": "tenant-001"}],
                DurationSeconds=900,
            )

    def test_second_call_within_ttl_does_not_re_call_assume_role(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("agentcore_memory.token_vending_machine.boto3.Session"):
            tvm, mock_sts = self._make_tvm(mock_boto_client)
            tvm.get_session({"tenantId": "tenant-001"})
            tvm.get_session({"tenantId": "tenant-001"})
            assert mock_sts.assume_role.call_count == 1

    def test_missing_tenant_id_raises_isolation_error(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("agentcore_memory.token_vending_machine.boto3.Session"):
            tvm, _ = self._make_tvm(mock_boto_client)
            with pytest.raises(IsolationError, match="tenantId"):
                tvm.get_session({})

    def test_assume_role_exception_wrapped_in_isolation_error(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("agentcore_memory.token_vending_machine.boto3.Session"):
            mock_sts = MagicMock()
            mock_sts.assume_role.side_effect = RuntimeError("STS unavailable")
            mock_boto_client.return_value = mock_sts
            tvm = TokenVendingMachine(role_arn=VALID_ARN)
            with pytest.raises(IsolationError):
                tvm.get_session({"tenantId": "tenant-001"})

    def test_successful_call_returns_boto3_session(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("agentcore_memory.token_vending_machine.boto3.Session") as mock_session_cls:
            tvm, _ = self._make_tvm(mock_boto_client)
            mock_session_instance = MagicMock()
            mock_session_cls.return_value = mock_session_instance
            result = tvm.get_session({"tenantId": "tenant-001"})
            mock_session_cls.assert_called_once_with(
                aws_access_key_id=STS_CREDENTIALS["AccessKeyId"],
                aws_secret_access_key=STS_CREDENTIALS["SecretAccessKey"],
                aws_session_token=STS_CREDENTIALS["SessionToken"],
            )
            assert result is mock_session_instance


class TestTVMNegativeCache:
    def _failing_tvm(self, mock_boto_client):
        mock_sts = MagicMock()
        mock_sts.assume_role.side_effect = RuntimeError("STS throttled")
        mock_boto_client.return_value = mock_sts
        return TokenVendingMachine(role_arn=VALID_ARN), mock_sts

    def test_sts_failure_backs_off_briefly(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("agentcore_memory.token_vending_machine.boto3.Session"):
            tvm, mock_sts = self._failing_tvm(mock_boto_client)
            with pytest.raises(IsolationError):
                tvm.get_session({"tenantId": "tenant-001"})
            with pytest.raises(IsolationError, match="recently failed"):
                tvm.get_session({"tenantId": "tenant-001"})
            assert mock_sts.assume_role.call_count == 1

    def test_sts_failure_is_retried_after_back_off(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("agentcore_memory.token_vending_machine.boto3.Session"), \
             patch("agentcore_memory.token_vending_machine.time.monotonic") as clock:
            tvm, mock_sts = self._failing_tvm(mock_boto_client)
            clock.return_value = 1000.0
            with pytest.raises(IsolationError):
                tvm.get_session({"tenantId": "tenant-001"})
            # Back-off is seconds, not the 12-minute credential TTL.
            clock.return_value = 1006.0
            mock_sts.assume_role.side_effect = None
            mock_sts.assume_role.return_value = {"Credentials": STS_CREDENTIALS}
            tvm.get_session({"tenantId": "tenant-001"})
            assert mock_sts.assume_role.call_count == 2


class TestTenantIdValidation:
    @pytest.mark.parametrize("good", ["tenant-001", "acme", "A_b-9", "a" * 57])
    def test_valid_tenant_ids_accepted(self, good):
        assert validate_tenant_id({"tenantId": good}) == good

    @pytest.mark.parametrize("bad", [
        "acme:corp",   # ':' is the actorId separator
        "acme/x",      # '/' is the namespace separator
        "-leading",
        "_bootstrap",  # reserved for the start-up placeholder
        "a" * 58,      # RoleSessionName limit
        "tenant 1",
        123,
    ])
    def test_invalid_tenant_ids_rejected(self, bad):
        with pytest.raises(IsolationError):
            validate_tenant_id({"tenantId": bad})

    def test_get_session_rejects_invalid_tenant_before_sts(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client:
            mock_sts = _make_sts_mock()
            mock_boto_client.return_value = mock_sts
            tvm = TokenVendingMachine(role_arn=VALID_ARN)
            with pytest.raises(IsolationError):
                tvm.get_session({"tenantId": "acme:corp"})
            mock_sts.assume_role.assert_not_called()


class TestBootstrapSession:
    def test_bootstrap_session_uses_reserved_tag(self):
        with patch("agentcore_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("agentcore_memory.token_vending_machine.boto3.Session"):
            mock_sts = _make_sts_mock()
            mock_boto_client.return_value = mock_sts
            TokenVendingMachine(role_arn=VALID_ARN).get_bootstrap_session()
            mock_sts.assume_role.assert_called_once_with(
                RoleArn=VALID_ARN,
                RoleSessionName=f"tenant-{BOOTSTRAP_TENANT_ID}",
                Tags=[{"Key": "TenantID", "Value": BOOTSTRAP_TENANT_ID}],
                DurationSeconds=900,
            )
