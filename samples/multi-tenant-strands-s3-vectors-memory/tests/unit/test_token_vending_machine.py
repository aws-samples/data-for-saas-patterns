"""
test_token_vending_machine.py — Unit tests for TokenVendingMachine.

Covers:
  - Task 4.1: TestTVMConstruction  (Requirements 7.1, 7.2, 7.3, 7.4)
  - Task 4.2: TestTVMGetSession    (Requirements 8.1, 8.2, 8.3, 8.4, 8.5)
  - Task 4.3: TestTVMGetClient     (Requirements 9.1, 9.2)

All boto3 calls are patched at the token_vending_machine module level.
No real AWS calls are made.
"""

import os
import pytest
from unittest.mock import MagicMock, patch, call

from strands_s3_vectors_memory.token_vending_machine import TokenVendingMachine, IsolationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_ARN = "arn:aws:iam::123:role/R"

STS_CREDENTIALS = {
    "AccessKeyId": "AKIA_TEST",
    "SecretAccessKey": "secret",
    "SessionToken": "token",
}

STS_ASSUME_ROLE_RESPONSE = {"Credentials": STS_CREDENTIALS}


def _make_sts_mock():
    """Return a MagicMock STS client pre-configured with assume_role."""
    sts = MagicMock()
    sts.assume_role.return_value = STS_ASSUME_ROLE_RESPONSE
    return sts


# ---------------------------------------------------------------------------
# Task 4.1 — TestTVMConstruction
# ---------------------------------------------------------------------------


class TestTVMConstruction:
    """Requirements 7.1, 7.2, 7.3, 7.4"""

    def test_empty_role_arn_raises_isolation_error(self):
        """Req 7.1 — empty string role_arn raises IsolationError mentioning role_arn."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client"):
            with pytest.raises(IsolationError, match="role_arn"):
                TokenVendingMachine(role_arn="")

    def test_none_role_arn_raises_isolation_error(self):
        """Req 7.2 — None role_arn raises IsolationError."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client"):
            with pytest.raises(IsolationError):
                TokenVendingMachine(role_arn=None)

    def test_valid_arn_stores_role_arn_and_creates_sts_client(self):
        """Req 7.3 — valid ARN is stored and STS client is created."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client:
            mock_sts = _make_sts_mock()
            mock_boto_client.return_value = mock_sts

            tvm = TokenVendingMachine(role_arn=VALID_ARN)

            assert tvm.role_arn == VALID_ARN
            mock_boto_client.assert_called_once_with("sts", region_name="us-east-1")

    def test_region_defaults_to_aws_region_env_var(self):
        """Req 7.4 — region_name defaults to AWS_REGION env var."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client"):
            with patch.dict(os.environ, {"AWS_REGION": "eu-west-1"}):
                tvm = TokenVendingMachine(role_arn=VALID_ARN)
                assert tvm.region_name == "eu-west-1"

    def test_region_defaults_to_us_east_1_when_env_var_absent(self):
        """Req 7.4 — region_name defaults to 'us-east-1' when AWS_REGION is not set."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client"):
            env = {k: v for k, v in os.environ.items() if k != "AWS_REGION"}
            with patch.dict(os.environ, env, clear=True):
                tvm = TokenVendingMachine(role_arn=VALID_ARN)
                assert tvm.region_name == "us-east-1"

    def test_explicit_region_name_is_used(self):
        """Req 7.4 — explicit region_name overrides env var."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client"):
            tvm = TokenVendingMachine(role_arn=VALID_ARN, region_name="ap-southeast-1")
            assert tvm.region_name == "ap-southeast-1"


# ---------------------------------------------------------------------------
# Task 4.2 — TestTVMGetSession
# ---------------------------------------------------------------------------


class TestTVMGetSession:
    """Requirements 8.1, 8.2, 8.3, 8.4, 8.5"""

    def _make_tvm(self, mock_boto_client, mock_session_cls):
        """Construct a TVM with pre-wired mocks."""
        mock_sts = _make_sts_mock()
        mock_boto_client.return_value = mock_sts
        tvm = TokenVendingMachine(role_arn=VALID_ARN)
        return tvm, mock_sts

    def test_first_call_invokes_assume_role_with_correct_args(self):
        """Req 8.1 — first get_session call calls sts.assume_role with correct parameters."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session") as mock_session_cls:

            tvm, mock_sts = self._make_tvm(mock_boto_client, mock_session_cls)
            tvm.get_session({"tenantId": "t1"})

            mock_sts.assume_role.assert_called_once_with(
                RoleArn=VALID_ARN,
                RoleSessionName="tenant-t1",
                Tags=[{"Key": "TenantID", "Value": "t1"}],
                DurationSeconds=900,
            )

    def test_second_call_within_ttl_does_not_re_call_assume_role(self):
        """Req 8.2 — second call within TTL returns cached session without calling assume_role again."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session") as mock_session_cls:

            tvm, mock_sts = self._make_tvm(mock_boto_client, mock_session_cls)

            tvm.get_session({"tenantId": "t1"})
            tvm.get_session({"tenantId": "t1"})

            # assume_role must have been called exactly once despite two get_session calls
            assert mock_sts.assume_role.call_count == 1

    def test_missing_tenant_id_raises_isolation_error(self):
        """Req 8.3 — get_session({}) raises IsolationError mentioning 'tenantId'."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session"):

            tvm, _ = self._make_tvm(mock_boto_client, None)

            with pytest.raises(IsolationError, match="tenantId"):
                tvm.get_session({})

    def test_assume_role_exception_wrapped_in_isolation_error(self):
        """Req 8.4 — sts.assume_role exception is wrapped in IsolationError."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session"):

            mock_sts = MagicMock()
            mock_sts.assume_role.side_effect = RuntimeError("STS unavailable")
            mock_boto_client.return_value = mock_sts

            tvm = TokenVendingMachine(role_arn=VALID_ARN)

            with pytest.raises(IsolationError):
                tvm.get_session({"tenantId": "t1"})

    def test_successful_call_returns_boto3_session(self):
        """Req 8.5 — successful get_session returns a boto3.Session built from STS credentials."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session") as mock_session_cls:

            tvm, mock_sts = self._make_tvm(mock_boto_client, mock_session_cls)
            mock_session_instance = MagicMock()
            mock_session_cls.return_value = mock_session_instance

            result = tvm.get_session({"tenantId": "t1"})

            mock_session_cls.assert_called_once_with(
                aws_access_key_id=STS_CREDENTIALS["AccessKeyId"],
                aws_secret_access_key=STS_CREDENTIALS["SecretAccessKey"],
                aws_session_token=STS_CREDENTIALS["SessionToken"],
            )
            assert result is mock_session_instance


# ---------------------------------------------------------------------------
# Task 4.3 — TestTVMGetClient
# ---------------------------------------------------------------------------


class TestTVMGetClient:
    """Requirements 9.1, 9.2"""

    def test_get_client_calls_get_session_and_returns_session_client(self):
        """Req 9.1 — get_client calls get_session and returns session.client(service, region_name=...)."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session") as mock_session_cls:

            mock_sts = _make_sts_mock()
            mock_boto_client.return_value = mock_sts

            mock_session = MagicMock()
            mock_service_client = MagicMock()
            mock_session.client.return_value = mock_service_client
            mock_session_cls.return_value = mock_session

            tvm = TokenVendingMachine(role_arn=VALID_ARN, region_name="us-east-1")
            result = tvm.get_client("s3vectors", {"tenantId": "t1"})

            mock_session.client.assert_called_once_with("s3vectors", region_name="us-east-1")
            assert result is mock_service_client

    def test_two_different_tenants_return_different_client_instances(self):
        """Req 9.2 — two different tenants produce two different client instances."""
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session") as mock_session_cls:

            # Each STS call returns distinct credentials
            mock_sts = MagicMock()
            mock_sts.assume_role.side_effect = [
                {"Credentials": {"AccessKeyId": "KEY1", "SecretAccessKey": "sec1", "SessionToken": "tok1"}},
                {"Credentials": {"AccessKeyId": "KEY2", "SecretAccessKey": "sec2", "SessionToken": "tok2"}},
            ]
            mock_boto_client.return_value = mock_sts

            # Each Session() call returns a distinct mock session
            session_a = MagicMock()
            session_b = MagicMock()
            client_a = MagicMock()
            client_b = MagicMock()
            session_a.client.return_value = client_a
            session_b.client.return_value = client_b
            mock_session_cls.side_effect = [session_a, session_b]

            tvm = TokenVendingMachine(role_arn=VALID_ARN)
            result_a = tvm.get_client("s3vectors", {"tenantId": "tenant-a"})
            result_b = tvm.get_client("s3vectors", {"tenantId": "tenant-b"})

            assert result_a is not result_b
            assert result_a is client_a
            assert result_b is client_b


# ---------------------------------------------------------------------------
# Issue #14 — Session object cached per tenant (not recreated on every get_client)
# ---------------------------------------------------------------------------

class TestTVMSessionCaching:
    """Issue #14: two get_client calls for the same tenant must reuse one Session."""

    def test_get_client_twice_creates_only_one_session(self):
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session") as mock_session_cls:

            mock_sts = _make_sts_mock()
            mock_boto_client.return_value = mock_sts
            mock_session = MagicMock()
            mock_session.client.return_value = MagicMock()
            mock_session_cls.return_value = mock_session

            tvm = TokenVendingMachine(role_arn=VALID_ARN)
            tvm.get_client("s3vectors", {"tenantId": "t1"})
            tvm.get_client("s3vectors", {"tenantId": "t1"})

            assert mock_session_cls.call_count == 1


# ---------------------------------------------------------------------------
# Issue #15 — STS client created with explicit region_name
# ---------------------------------------------------------------------------

class TestTVMSTSClientRegion:
    """Issue #15: boto3.client('sts') must be called with region_name."""

    def test_sts_client_uses_explicit_region(self):
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client:
            mock_boto_client.return_value = MagicMock()
            TokenVendingMachine(role_arn=VALID_ARN, region_name="us-gov-west-1")
            mock_boto_client.assert_called_once_with("sts", region_name="us-gov-west-1")


# ---------------------------------------------------------------------------
# Issue #16 — negative cache on STS failure prevents thundering herd
# ---------------------------------------------------------------------------

class TestTVMNegativeCache:
    """Issue #16: two rapid get_session calls when STS fails must only call STS once."""

    def test_sts_failure_cached_prevents_second_call(self):
        with patch("strands_s3_vectors_memory.token_vending_machine.boto3.client") as mock_boto_client, \
             patch("strands_s3_vectors_memory.token_vending_machine.boto3.Session"):

            mock_sts = MagicMock()
            mock_sts.assume_role.side_effect = RuntimeError("STS throttled")
            mock_boto_client.return_value = mock_sts

            tvm = TokenVendingMachine(role_arn=VALID_ARN)

            with pytest.raises(IsolationError):
                tvm.get_session({"tenantId": "t1"})
            with pytest.raises(IsolationError):
                tvm.get_session({"tenantId": "t1"})

            assert mock_sts.assume_role.call_count == 1
