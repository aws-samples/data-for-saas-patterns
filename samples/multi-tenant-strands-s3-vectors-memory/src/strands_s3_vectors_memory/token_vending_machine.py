"""
token_vending_machine.py — TVM credential manager

Provides tenant-scoped boto3 sessions via STS AssumeRole + TenantID session tag.
Credentials are cached per tenant (TTL = 12 min) to avoid redundant STS calls.

Usage:
    mgr    = TokenVendingMachine(role_arn="arn:aws:iam::123456789012:role/MyTvmRole")
    client = mgr.get_client("s3vectors", tenant_context)

Debug logging:
  import logging
  logging.getLogger("strands_s3_vectors_memory.token_vending_machine").setLevel(logging.DEBUG)
"""

import logging
import os
import boto3
from typing import Any, Dict

from cachetools import TTLCache

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 720   # 12 min — 3 min buffer before 15 min STS expiry
_CACHE_MAXSIZE     = 128


class IsolationError(Exception):
    """Raised on any STS or configuration failure. No ambient-credential fallback."""


class TokenVendingMachine:
    """Obtains and caches tenant-scoped boto3 sessions via STS AssumeRole."""

    def __init__(self, role_arn: str, region_name: str = None) -> None:
        if not role_arn:
            raise IsolationError("role_arn must be a non-empty string.")
        self.role_arn    = role_arn
        self.region_name = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._sts        = boto3.client("sts", region_name=self.region_name)  # explicit region (#15)
        self._cache: TTLCache = TTLCache(maxsize=_CACHE_MAXSIZE, ttl=_CACHE_TTL_SECONDS)
        logger.debug(
            "[tvm] TokenVendingMachine init: role_arn=%s region=%s cache_ttl=%ds",
            role_arn, self.region_name, _CACHE_TTL_SECONDS,
        )

    def get_session(self, tenant_context: Dict) -> boto3.Session:
        """
        Return a boto3.Session scoped to the tenant via STS AssumeRole.

        The TenantID session tag activates IAM ABAC conditions on the TVM role.
        Credentials are cached per tenantId for 12 minutes.

        Raises IsolationError if tenantId is missing or STS fails.
        """
        tenant_id = tenant_context.get("tenantId")
        if not tenant_id:
            raise IsolationError("tenant_context must contain a non-empty 'tenantId'.")

        if tenant_id in self._cache:
            session = self._cache[tenant_id]
            if session is None:
                logger.warning(
                    "[tvm] get_session: cached STS failure for tenant=%s — "
                    "will retry after TTL expires",
                    tenant_id,
                )
                raise IsolationError(
                    f"STS AssumeRole previously failed for tenant '{tenant_id}' — "
                    "cached failure, will retry after TTL expires."
                )
            logger.debug("[tvm] get_session: cache hit tenant=%s", tenant_id)
            return session

        # Cache miss — call STS
        logger.debug("[tvm] get_session: cache miss tenant=%s calling STS AssumeRole", tenant_id)
        try:
            response = self._sts.assume_role(
                RoleArn=self.role_arn,
                RoleSessionName=f"tenant-{tenant_id}",
                Tags=[{"Key": "TenantID", "Value": tenant_id}],
                DurationSeconds=900,
            )
            creds = response["Credentials"]
            # Cache the Session object directly so it's reused on every call (#14)
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
            self._cache[tenant_id] = session
            logger.debug(
                "[tvm] get_session: STS AssumeRole succeeded tenant=%s session cached ttl=%ds",
                tenant_id, _CACHE_TTL_SECONDS,
            )
            return session
        except Exception as exc:
            # Cache a sentinel so rapid retries don't hammer STS (#16)
            self._cache[tenant_id] = None
            logger.error(
                "[tvm] get_session: STS AssumeRole failed tenant=%s error=%s",
                tenant_id, exc,
            )
            raise IsolationError(
                f"STS AssumeRole failed for tenant '{tenant_id}': {exc}"
            ) from exc

    def get_client(self, service_name: str, tenant_context: Dict) -> Any:
        """Return boto3.client(service_name) from the tenant-scoped session."""
        tenant_id = tenant_context.get("tenantId") if tenant_context else None
        logger.debug(
            "[tvm] get_client: service=%s tenant=%s", service_name, tenant_id,
        )
        return self.get_session(tenant_context).client(service_name, region_name=self.region_name)
