"""
Integration tests for multi-tenant MultiTenantS3VectorMemory.

Covers: TestMultiTenantStoreRetrieve (Reqs 3.1, 3.2, 3.3, 3.4)

Requires:
  - S3_VECTOR_BUCKET_NAME env var set
  - Valid AWS credentials with s3vectors + bedrock-runtime access
"""

import os

import pytest
from botocore.exceptions import ClientError

from strands_s3_vectors_memory.s3_vector_memory import MultiTenantS3VectorMemory
from strands_s3_vectors_memory.token_vending_machine import TokenVendingMachine
from tests.integration._constants import BUCKET_NAME, AWS_REGION, BEDROCK_MODEL_ID, RUN_ID, TVM_ROLE_ARN

# Embedding model is separate from the chat model
EMBEDDING_MODEL_ID: str = os.environ.get("EMBEDDING_MODEL", "amazon.nova-2-multimodal-embeddings-v1:0")


class TestMultiTenantStoreRetrieve:
    """End-to-end store/retrieve tests for multi-tenant MultiTenantS3VectorMemory (Reqs 3.1–3.4)."""

    @pytest.fixture(autouse=True)
    def _setup(self, tenant_a_index, tenant_b_index):
        """Ensure both tenant indexes exist before any test in this class runs."""
        self.mem = MultiTenantS3VectorMemory(
            bucket_name=BUCKET_NAME,
            region_name=AWS_REGION,
            embedding_model=EMBEDDING_MODEL_ID,
            tvm_role_arn=TVM_ROLE_ARN,
        )
        self.tenant_a_context = {"tenantId": f"tenant-a-{RUN_ID}"}
        self.tenant_b_context = {"tenantId": f"tenant-b-{RUN_ID}"}

    # ------------------------------------------------------------------
    # 8.1 — store writes to the tenant-specific index (Req 3.1)
    # ------------------------------------------------------------------

    def test_store_writes_to_tenant_index(self):
        """Req 3.1: store_memory under tenant A context writes to memory-{tenantId_A} index."""
        user_id = f"user_{RUN_ID}_mt_8_1"
        content = "I enjoy reading science fiction novels on weekends."

        result = self.mem.store_memory(user_id, content, tenant_context=self.tenant_a_context)

        assert result["status"] == "success"
        assert result["index_name"] == f"memory-tenant-a-{RUN_ID}"

        # Verify the vector is retrievable from tenant A's index
        results = self.mem.retrieve_memories(
            user_id, content, tenant_context=self.tenant_a_context, top_k=5
        )
        assert len(results) >= 1, (
            "Expected at least one result when retrieving from tenant A's index after storing"
        )

    # ------------------------------------------------------------------
    # 8.2 — cross-tenant retrieve returns empty list (Req 3.2)
    # ------------------------------------------------------------------

    def test_cross_tenant_retrieve_returns_empty(self):
        """Req 3.2: retrieving under tenant B context returns empty list for tenant A's content."""
        user_id = f"user_{RUN_ID}_mt_8_2"
        content = "My favourite sport is cycling through the countryside."

        # Store under tenant A
        self.mem.store_memory(user_id, content, tenant_context=self.tenant_a_context)

        # Retrieve under tenant B with the same user and matching query
        results = self.mem.retrieve_memories(
            user_id, content, tenant_context=self.tenant_b_context, top_k=5
        )

        assert results == [], (
            f"Expected empty list when retrieving tenant A's content under tenant B context, "
            f"got {results}"
        )

    # ------------------------------------------------------------------
    # 8.3 — _build_index_name returns "memory-{tenantId}" (Req 3.3)
    # ------------------------------------------------------------------

    def test_index_name_includes_tenant_id(self):
        """Req 3.3: _build_index_name(tenant_context) returns 'memory-{tenantId}'."""
        tenant_context = {"tenantId": "acme-corp"}

        index_name = self.mem._build_index_name(tenant_context)

        assert index_name == "memory-acme-corp", (
            f"Expected 'memory-acme-corp', got {index_name!r}"
        )

    # ------------------------------------------------------------------
    # 8.4 — missing tenantId raises ValueError (Req 3.4)
    # ------------------------------------------------------------------

    def test_missing_tenant_id_raises_value_error(self):
        """Req 3.4: _build_index_name({}) raises ValueError when tenantId is absent."""
        with pytest.raises(ValueError):
            self.mem._build_index_name({})


class TestTVMCredentialScoping:
    """TVM credential scoping tests (Reqs 3.5, 3.6).

    All tests in this class are skipped when S3_VECTOR_TVM_ROLE_ARN is not set.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tenant_a_index, tenant_b_index):
        """Ensure both tenant indexes exist and set up shared state."""
        self.tenant_a_context = {"tenantId": f"tenant-a-{RUN_ID}"}
        self.tenant_b_context = {"tenantId": f"tenant-b-{RUN_ID}"}

    # ------------------------------------------------------------------
    # 9.1 — TVM vends a tenant-scoped session (Req 3.5)
    # ------------------------------------------------------------------

    @pytest.mark.skipif(TVM_ROLE_ARN is None, reason="S3_VECTOR_TVM_ROLE_ARN not set")
    def test_tvm_vends_tenant_scoped_session(self):
        """Req 3.5: TVM returns a boto3 session with valid credentials scoped to the tenant."""
        tvm = TokenVendingMachine(role_arn=TVM_ROLE_ARN, region_name=AWS_REGION)

        session = tvm.get_session(self.tenant_a_context)

        # Session must have credentials
        credentials = session.get_credentials()
        assert credentials is not None, "Expected TVM session to have credentials"

        # Resolve the credentials to confirm they are real (not None)
        resolved = credentials.get_frozen_credentials()
        assert resolved.access_key is not None, "Expected non-None access_key"
        assert resolved.secret_key is not None, "Expected non-None secret_key"
        assert resolved.token is not None, "Expected non-None session token (STS assumed role)"

        # Verify the session is scoped to the correct role by checking the caller identity.
        # The assumed-role ARN should contain the role name from TVM_ROLE_ARN.
        sts_client = session.client("sts", region_name=AWS_REGION)
        identity = sts_client.get_caller_identity()
        assumed_role_arn = identity.get("Arn", "")

        # The assumed role session ARN has the form:
        #   arn:aws:sts::<account>:assumed-role/<role-name>/tenant-<tenantId>
        # Verify the session name contains the tenant ID (confirms TenantID tag was used).
        tenant_id = self.tenant_a_context["tenantId"]
        assert f"tenant-{tenant_id}" in assumed_role_arn, (
            f"Expected assumed-role ARN to contain 'tenant-{tenant_id}', got: {assumed_role_arn}"
        )

    # ------------------------------------------------------------------
    # 9.2 — Tenant B TVM session denied access to tenant A's index (Req 3.6)
    # ------------------------------------------------------------------

    @pytest.mark.skipif(TVM_ROLE_ARN is None, reason="S3_VECTOR_TVM_ROLE_ARN not set")
    def test_tvm_tenant_b_denied_access_to_tenant_a_index(self):
        """Req 3.6: TVM credentials for tenant B are denied access to tenant A's index (IAM ABAC)."""
        tvm = TokenVendingMachine(role_arn=TVM_ROLE_ARN, region_name=AWS_REGION)

        # Get a session scoped to tenant B
        tenant_b_session = tvm.get_session(self.tenant_b_context)
        s3vectors_client = tenant_b_session.client("s3vectors", region_name=AWS_REGION)

        # Attempt to query tenant A's index using tenant B's credentials.
        # IAM ABAC policy should deny this with AccessDeniedException.
        tenant_a_index_name = f"memory-tenant-a-{RUN_ID}"

        with pytest.raises(ClientError) as exc_info:
            s3vectors_client.query_vectors(
                vectorBucketName=BUCKET_NAME,
                indexName=tenant_a_index_name,
                queryVector={"float32": [0.0] * 1024},
                topK=1,
            )

        error_code = exc_info.value.response["Error"]["Code"]
        assert error_code == "AccessDeniedException", (
            f"Expected AccessDeniedException, got: {error_code}"
        )
