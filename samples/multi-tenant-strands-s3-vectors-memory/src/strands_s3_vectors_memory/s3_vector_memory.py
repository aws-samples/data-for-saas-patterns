"""
s3_vector_memory.py — S3 Vectors memory store (single-tenant and multi-tenant)

Single-tenant  : S3VectorMemory
  - One shared index named "memory"
  - Ambient boto3 credentials

Multi-tenant   : MultiTenantS3VectorMemory(S3VectorMemory)
  - One index per tenant: "memory-{tenantId}"
  - TVM credentials scoped per tenant via STS AssumeRole + TenantID session tag
  - IAM Resource ARN: arn:aws:s3vectors:*:*:bucket/*/index/memory-${aws:PrincipalTag/TenantID}

Env vars: S3_VECTOR_BUCKET_NAME, S3_VECTOR_TVM_ROLE_ARN, AWS_REGION, EMBEDDING_MODEL

Debug logging:
  import logging
  logging.getLogger("strands_s3_vectors_memory.s3_vector_memory").setLevel(logging.DEBUG)
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)

_MAX_KEY_LENGTH = 512  # S3 Vectors key length limit

_DEFAULT_BUCKET   = os.environ.get("S3_VECTOR_BUCKET_NAME", "my-vector-memory")
_DEFAULT_REGION   = os.environ.get("AWS_REGION", "us-east-1")
_DEFAULT_MODEL    = os.environ.get("EMBEDDING_MODEL", "amazon.nova-2-multimodal-embeddings-v1:0")
_VECTOR_DIMENSION = 1024


class S3VectorMemory:
    """
    Single-tenant S3 Vectors memory store.

    Uses a single index named 'memory' and ambient boto3 credentials.
    Suitable for single-tenant applications or development/testing.
    """

    def __init__(self, bucket_name: str = _DEFAULT_BUCKET, region_name: str = _DEFAULT_REGION,
                 embedding_model: str = _DEFAULT_MODEL) -> None:
        self.bucket_name     = bucket_name
        self.region_name     = region_name
        self.embedding_model = embedding_model
        self._s3vectors      = boto3.client("s3vectors", region_name=region_name)
        logger.debug(
            "[s3-vector-memory] S3VectorMemory init: bucket=%s region=%s model=%s",
            bucket_name, region_name, embedding_model,
        )

    def _build_index_name(self, tenant_context: Optional[Dict] = None) -> str:
        """Single-tenant: always returns 'memory'."""
        return "memory"

    def _get_s3vectors_client(self, tenant_context: Optional[Dict] = None):
        """Single-tenant: always returns the ambient client."""
        return self._s3vectors

    def store_memory(self, user_id: str, content: str,
                     tenant_context: Optional[Dict] = None) -> Dict:
        """Embed content and put_vectors to the index."""
        index_name = self._build_index_name(tenant_context)
        logger.debug(
            "[s3-vector-memory] store_memory: user=%s index=%s content_len=%d",
            user_id, index_name, len(content),
        )

        embedding  = self._embed(content, purpose="GENERIC_INDEX")
        timestamp  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        raw_key    = f"{user_id}_{timestamp}_{uuid.uuid4().hex[:8]}"
        key        = raw_key[:_MAX_KEY_LENGTH]  # cap to S3 Vectors key length limit (#6)
        client     = self._get_s3vectors_client(tenant_context)

        # Truncate to 4096 UTF-8 bytes, not 4096 characters (#2)
        content_bytes = content.encode("utf-8")[:4096]
        safe_content  = content_bytes.decode("utf-8", errors="ignore")

        client.put_vectors(
            vectorBucketName=self.bucket_name,
            indexName=index_name,
            vectors=[{
                "key":  key,
                "data": {"float32": [float(x) for x in embedding]},
                "metadata": {
                    "user_id":   user_id,
                    "content":   safe_content,
                    "stored_at": timestamp,
                },
            }],
        )
        logger.debug(
            "[s3-vector-memory] store_memory: stored key=%s bytes=%d",
            key, len(content_bytes),
        )
        return {"status": "success", "index_name": index_name, "key": key}

    def retrieve_memories(self, user_id: str, query: str, top_k: int = 5,
                          tenant_context: Optional[Dict] = None) -> List[Dict]:
        """query_vectors from the index filtered by user_id."""
        if not query or not query.strip():  # guard for empty query (#3)
            logger.debug("[s3-vector-memory] retrieve_memories: empty query, returning []")
            return []

        index_name = self._build_index_name(tenant_context)
        logger.debug(
            "[s3-vector-memory] retrieve_memories: user=%s index=%s query_len=%d top_k=%d",
            user_id, index_name, len(query), top_k,
        )

        query_embedding = self._embed(query, purpose="GENERIC_RETRIEVAL")
        client          = self._get_s3vectors_client(tenant_context)

        response = client.query_vectors(
            vectorBucketName=self.bucket_name,
            indexName=index_name,
            queryVector={"float32": query_embedding},
            topK=top_k,
            filter={"user_id": user_id},
            returnDistance=True,
            returnMetadata=True,
        )

        results = []
        for v in response.get("vectors", []):
            distance = v.get("distance", 2.0)
            results.append({
                "key":        v["key"],
                "content":    v["metadata"].get("content", ""),
                "similarity": round(1.0 - (distance / 2.0), 3),
                "stored_at":  v["metadata"].get("stored_at", ""),
            })
        results.sort(key=lambda x: x["similarity"], reverse=True)

        logger.debug(
            "[s3-vector-memory] retrieve_memories: returned %d results top_similarity=%s",
            len(results),
            results[0]["similarity"] if results else "n/a",
        )
        return results

    def _embed(self, text: str, purpose: str = "GENERIC_INDEX") -> List[float]:
        """Call Amazon Nova Multimodal Embeddings and return the embedding vector.
        Creates a new boto3 client per call — boto3 clients are not thread-safe."""
        logger.debug(
            "[s3-vector-memory] _embed: purpose=%s text_len=%d",
            purpose, len(text),
        )
        body = {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": purpose,
                "embeddingDimension": _VECTOR_DIMENSION,
                "text": {"truncationMode": "END", "value": text},
            },
        }
        client = boto3.client("bedrock-runtime", region_name=self.region_name)
        response = client.invoke_model(
            modelId=self.embedding_model, body=json.dumps(body), contentType="application/json"
        )
        embedding = json.loads(response["body"].read())["embeddings"][0]["embedding"]
        logger.debug("[s3-vector-memory] _embed: received vector dim=%d", len(embedding))
        return embedding


class MultiTenantS3VectorMemory(S3VectorMemory):
    """
    Multi-tenant S3 Vectors memory store.

    Extends S3VectorMemory with per-tenant index isolation and TVM credentials.
    Each tenant gets a dedicated index named 'memory-{tenantId}'. Credentials
    are scoped per tenant via STS AssumeRole + TenantID session tag so IAM
    enforces the boundary at the resource ARN level.

    TVM role Resource: arn:aws:s3vectors:*:*:bucket/*/index/memory-${aws:PrincipalTag/TenantID}
    """

    def __init__(self, bucket_name: str = _DEFAULT_BUCKET, region_name: str = _DEFAULT_REGION,
                 embedding_model: str = _DEFAULT_MODEL, tvm_role_arn: Optional[str] = None) -> None:
        super().__init__(bucket_name=bucket_name, region_name=region_name,
                         embedding_model=embedding_model)
        from .token_vending_machine import TokenVendingMachine
        self._isolation = TokenVendingMachine(role_arn=tvm_role_arn, region_name=region_name) \
            if tvm_role_arn else None
        logger.debug(
            "[s3-vector-memory] MultiTenantS3VectorMemory init: mode=%s",
            "tvm" if self._isolation else "ambient",
        )

    def _build_index_name(self, tenant_context: Optional[Dict] = None) -> str:
        """Multi-tenant: returns 'memory-{tenantId}' — matches TVM role Resource ARN."""
        if not tenant_context or not tenant_context.get("tenantId"):
            raise ValueError("tenant_context with a valid 'tenantId' is required.")
        return f"memory-{tenant_context['tenantId']}"

    def _get_s3vectors_client(self, tenant_context: Optional[Dict] = None):
        """Multi-tenant: returns a TVM-scoped client, or raises if no TVM role is set (#5).

        Ambient credential fallback is intentionally blocked in multi-tenant mode —
        it would silently bypass IAM ABAC tenant isolation.
        """
        if self._isolation:
            tenant_id = tenant_context.get("tenantId") if tenant_context else None
            logger.debug(
                "[s3-vector-memory] _get_s3vectors_client: delegating to TVM tenant=%s",
                tenant_id,
            )
            return self._isolation.get_client("s3vectors", tenant_context)
        # No TVM role configured — always raise, regardless of whether tenant_context
        # was provided. MultiTenantS3VectorMemory has no legitimate ambient-credential
        # path; silently falling back would bypass IAM ABAC isolation entirely.
        tenant_id = tenant_context.get("tenantId") if tenant_context else None
        raise ValueError(
            "MultiTenantS3VectorMemory requires a TVM role (tvm_role_arn) to enforce "
            "tenant isolation. No TVM role is configured"
            + (f" but tenant_context with tenantId='{tenant_id}' was provided." if tenant_id
               else " and no tenant_context was provided.")
            + " Set tvm_role_arn when constructing MultiTenantS3VectorMemory."
        )
