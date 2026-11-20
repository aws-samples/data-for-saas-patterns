# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
token_vending_machine.py — TVM credential manager

Provides tenant-scoped boto3 sessions via STS AssumeRole + TenantID session tag.
Credentials are cached per tenant (TTL = 12 min) to avoid redundant STS calls.

The TenantID session tag activates IAM ABAC conditions on the TVM role. For
AgentCore Memory, the role restricts `bedrock-agentcore:actorId` and
`bedrock-agentcore:namespacePath` to the caller's TenantID, so a session tagged
`TenantID=tenant-002` is denied access to `tenant-001:*` actors.

Tenant ids are validated here, in one place, before they reach STS, the session
tag or any memory identifier (see TENANT_ID_PATTERN).

Usage:
    mgr    = TokenVendingMachine(role_arn="arn:aws:iam::123456789012:role/MyTvmRole")
    client = mgr.get_client("bedrock-agentcore", tenant_context)

Debug logging:
  import logging
  logging.getLogger("agentcore_memory.token_vending_machine").setLevel(logging.DEBUG)
"""

import logging
import os
import re
from typing import Any, Dict

import boto3
from cachetools import TTLCache

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS    = 720   # 12 min — 3 min buffer before 15 min STS expiry
_CACHE_MAXSIZE        = 128
_FAILURE_TTL_SECONDS  = 5     # short back-off after an STS failure, then retry

# A tenant id must start with a letter or digit and contain only letters,
# digits, '_' and '-'. ':' and '/' are rejected because they are the separators
# in actorId ("{tenantId}:...") and namespaces ("/{actorId}/..."): a tenant
# "acme" must never be able to match "acme:corp:*". 57 characters keeps
# RoleSessionName ("tenant-" + id) within the STS 64-character limit.
TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,56}$")

# Reserved tag for the session manager's start-up placeholder session. It is a
# valid AgentCore actor and session id prefix, and validate_tenant_id() refuses
# it, so no real tenant can ever own it.
BOOTSTRAP_TENANT_ID = "agentcore-bootstrap"


class IsolationError(Exception):
    """Raised on any STS or configuration failure. No ambient-credential fallback."""


def validate_tenant_id(tenant_context: Dict) -> str:
    """Return the validated tenantId from tenant_context, or raise IsolationError."""
    tenant_id = tenant_context.get("tenantId") if tenant_context else None
    if not tenant_id:
        raise IsolationError("tenant_context must contain a non-empty 'tenantId'.")
    if not isinstance(tenant_id, str) or not TENANT_ID_PATTERN.match(tenant_id):
        raise IsolationError(
            "tenantId must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,56}$ "
            "(no ':' or '/', at most 57 characters)."
        )
    if tenant_id == BOOTSTRAP_TENANT_ID:
        raise IsolationError(f"tenantId '{BOOTSTRAP_TENANT_ID}' is reserved.")
    return tenant_id


class TokenVendingMachine:
    """Obtains and caches tenant-scoped boto3 sessions via STS AssumeRole."""

    def __init__(self, role_arn: str, region_name: str = None) -> None:
        if not role_arn:
            raise IsolationError("role_arn must be a non-empty string.")
        self.role_arn    = role_arn
        self.region_name = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._sts        = boto3.client("sts", region_name=self.region_name)
        self._cache: TTLCache = TTLCache(maxsize=_CACHE_MAXSIZE, ttl=_CACHE_TTL_SECONDS)
        # Tenants whose last AssumeRole failed; entries expire after the back-off.
        self._recent_failures: TTLCache = TTLCache(maxsize=_CACHE_MAXSIZE, ttl=_FAILURE_TTL_SECONDS)
        # Logs the role ARN and region only; no credential values.
        logger.debug(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            "[tvm] TokenVendingMachine init: role_arn=%s region=%s cache_ttl=%ds",
            role_arn, self.region_name, _CACHE_TTL_SECONDS,
        )

    def get_session(self, tenant_context: Dict) -> boto3.Session:
        """
        Return a boto3.Session scoped to the tenant via STS AssumeRole.

        The TenantID session tag activates IAM ABAC conditions on the TVM role.
        Credentials are cached per tenantId for 12 minutes. After an STS failure
        the tenant is not retried for 5 seconds, so a burst of requests does not
        hammer STS, and a single transient error does not lock the tenant out.

        Raises IsolationError if tenantId is missing or invalid, or STS fails.
        """
        return self._assume(validate_tenant_id(tenant_context))

    def _assume(self, tenant_id: str) -> boto3.Session:
        session = self._cache.get(tenant_id)
        if session is not None:
            logger.debug("[tvm] get_session: cache hit tenant=%s", tenant_id)
            return session

        if tenant_id in self._recent_failures:
            raise IsolationError(
                f"STS AssumeRole recently failed for tenant '{tenant_id}' — retrying shortly."
            )

        logger.debug("[tvm] get_session: cache miss tenant=%s calling STS AssumeRole", tenant_id)
        try:
            response = self._sts.assume_role(
                RoleArn=self.role_arn,
                RoleSessionName=f"tenant-{tenant_id}",
                Tags=[{"Key": "TenantID", "Value": tenant_id}],
                DurationSeconds=900,
            )
        except Exception as exc:
            self._recent_failures[tenant_id] = True
            logger.error(
                "[tvm] get_session: STS AssumeRole failed tenant=%s error=%s",
                tenant_id, exc,
            )
            raise IsolationError(
                f"STS AssumeRole failed for tenant '{tenant_id}': {exc}"
            ) from exc

        creds = response["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        self._cache[tenant_id] = session
        self._recent_failures.pop(tenant_id, None)
        logger.debug(
            "[tvm] get_session: STS AssumeRole succeeded tenant=%s session cached ttl=%ds",
            tenant_id, _CACHE_TTL_SECONDS,
        )
        return session

    def get_client(self, service_name: str, tenant_context: Dict) -> Any:
        """Return boto3.client(service_name) from the tenant-scoped session."""
        session = self.get_session(tenant_context)
        logger.debug("[tvm] get_client: service=%s tenant=%s", service_name, tenant_context.get("tenantId"))
        return session.client(service_name, region_name=self.region_name)
