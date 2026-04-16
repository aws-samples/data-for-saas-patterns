"""
test_s3_vector_memory.py — Unit tests for S3VectorMemory and MultiTenantS3VectorMemory.

Covers:
  - TestS3VectorMemoryIndexNaming   (Requirements 2.1, 2.2, 2.3, 2.4)
  - TestS3VectorMemoryStoreMemory   (Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6)
  - TestS3VectorMemoryRetrieveMemories (Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6)
  - TestS3VectorMemoryEmbed         (Requirements 5.1, 5.2, 5.3)
  - TestMultiTenantS3VectorMemory   (Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7)

All boto3 calls are mocked — no real AWS calls are made.
"""

import json
import re
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_s3vectors_mock():
    """Return a MagicMock that behaves like a boto3 s3vectors client."""
    client = MagicMock()
    client.put_vectors.return_value = {}
    client.query_vectors.return_value = {"vectors": []}
    return client


def _make_bedrock_mock(embedding=None):
    """Return a MagicMock bedrock-runtime client returning a 1024-dim embedding."""
    if embedding is None:
        embedding = [0.1] * 1024
    client = MagicMock()
    body_mock = MagicMock()
    body_mock.read.return_value = json.dumps(
        {"embeddings": [{"embedding": embedding}]}
    ).encode()
    client.invoke_model.return_value = {"body": body_mock}
    return client


# ---------------------------------------------------------------------------
# Task 2.1 — TestS3VectorMemoryIndexNaming
# Requirements: 2.1, 2.2, 2.3, 2.4
# ---------------------------------------------------------------------------

class TestS3VectorMemoryIndexNaming:
    """Single-tenant S3VectorMemory always uses the fixed 'memory' index."""

    @pytest.fixture(autouse=True)
    def patch_boto3(self):
        """Patch boto3.client so __init__ does not make real AWS calls."""
        with patch("boto3.client", return_value=_make_s3vectors_mock()):
            yield

    def _make_store(self):
        from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
        return S3VectorMemory(bucket_name="test-bucket", region_name="us-east-1")

    def test_build_index_name_no_args(self):
        """_build_index_name() with no arguments returns 'memory'."""
        store = self._make_store()
        assert store._build_index_name() == "memory"

    def test_build_index_name_none(self):
        """_build_index_name(None) returns 'memory'."""
        store = self._make_store()
        assert store._build_index_name(None) == "memory"

    def test_build_index_name_with_tenant_context_ignored(self):
        """_build_index_name({'tenantId': 'any-value'}) returns 'memory' — tenant context ignored."""
        store = self._make_store()
        assert store._build_index_name({"tenantId": "any-value"}) == "memory"

    def test_get_s3vectors_client_returns_ambient_client(self):
        """_get_s3vectors_client() returns the same client instance created at construction."""
        mock_client = _make_s3vectors_mock()
        with patch("boto3.client", return_value=mock_client):
            from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
            store = S3VectorMemory(bucket_name="test-bucket", region_name="us-east-1")

        assert store._get_s3vectors_client() is mock_client
        assert store._get_s3vectors_client(None) is mock_client
        assert store._get_s3vectors_client({"tenantId": "x"}) is mock_client


# ---------------------------------------------------------------------------
# Task 2.3 — TestS3VectorMemoryStoreMemory
# Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
# ---------------------------------------------------------------------------

class TestS3VectorMemoryStoreMemory:
    """store_memory embeds content and writes a correctly structured vector to S3."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a store with a mocked s3vectors client and a mocked _embed."""
        mock_s3v = _make_s3vectors_mock()
        with patch("boto3.client", return_value=mock_s3v):
            from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
            self.store = S3VectorMemory(bucket_name="test-bucket", region_name="us-east-1")

        self.mock_s3v = mock_s3v
        # Patch _embed directly on the instance so we control the embedding
        self.fake_embedding = [0.5] * 1024
        self.store._embed = MagicMock(return_value=self.fake_embedding)

    def test_embed_called_once_with_content_and_generic_index_purpose(self):
        """_embed is called exactly once with the content and purpose='GENERIC_INDEX'."""
        self.store.store_memory("user1", "hello world")
        self.store._embed.assert_called_once_with("hello world", purpose="GENERIC_INDEX")

    def test_put_vectors_called_with_correct_bucket_and_index(self):
        """put_vectors is called with vectorBucketName='test-bucket' and indexName='memory'."""
        self.store.store_memory("user1", "hello world")
        call_kwargs = self.mock_s3v.put_vectors.call_args[1]
        assert call_kwargs["vectorBucketName"] == "test-bucket"
        assert call_kwargs["indexName"] == "memory"

    def test_put_vectors_called_with_single_vector_entry(self):
        """put_vectors receives exactly one vector in the vectors list."""
        self.store.store_memory("user1", "hello world")
        call_kwargs = self.mock_s3v.put_vectors.call_args[1]
        assert len(call_kwargs["vectors"]) == 1

    def test_metadata_contains_required_fields(self):
        """Vector metadata contains user_id, content, and stored_at fields."""
        self.store.store_memory("user1", "hello world")
        call_kwargs = self.mock_s3v.put_vectors.call_args[1]
        metadata = call_kwargs["vectors"][0]["metadata"]
        assert metadata["user_id"] == "user1"
        assert metadata["content"] == "hello world"
        assert "stored_at" in metadata

    def test_return_dict_has_success_status_and_memory_index(self):
        """Return dict contains status='success', index_name='memory', and non-empty key."""
        result = self.store.store_memory("user1", "hello world")
        assert result["status"] == "success"
        assert result["index_name"] == "memory"
        assert result["key"]  # non-empty

    def test_content_longer_than_4096_chars_truncated_in_metadata(self):
        """Content > 4096 chars is truncated to 4096 in the stored metadata."""
        long_content = "x" * 5000
        self.store.store_memory("user1", long_content)
        call_kwargs = self.mock_s3v.put_vectors.call_args[1]
        stored_content = call_kwargs["vectors"][0]["metadata"]["content"]
        assert len(stored_content) == 4096

    def test_key_format_matches_expected_pattern(self):
        """Key matches pattern {user_id}_{YYYYMMDD}_{HHMMSS}_{8-char-hex}."""
        result = self.store.store_memory("myuser", "some content")
        key = result["key"]
        pattern = r"^.+_\d{8}_\d{6}_[0-9a-f]{8}$"
        assert re.match(pattern, key), f"Key '{key}' does not match pattern '{pattern}'"

    def test_key_starts_with_user_id(self):
        """Key starts with the provided user_id."""
        result = self.store.store_memory("alice", "some content")
        assert result["key"].startswith("alice_")

    def test_store_memory_metadata_includes_agent_name(self):
        """store_memory includes agent_name in metadata when provided."""
        self.store.store_memory("user1", "content", agent_name="orchestrator")
        metadata = self.mock_s3v.put_vectors.call_args[1]["vectors"][0]["metadata"]
        assert metadata["agent_name"] == "orchestrator"

    def test_store_memory_metadata_agent_name_defaults_to_default(self):
        """store_memory uses 'default' when agent_name is not provided."""
        self.store.store_memory("user1", "content")
        metadata = self.mock_s3v.put_vectors.call_args[1]["vectors"][0]["metadata"]
        assert metadata["agent_name"] == "default"


# ---------------------------------------------------------------------------
# Task 2.4 — TestS3VectorMemoryRetrieveMemories
# Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
# ---------------------------------------------------------------------------

class TestS3VectorMemoryRetrieveMemories:
    """retrieve_memories queries S3 and transforms the response correctly."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a store with mocked s3vectors client and mocked _embed."""
        self.mock_s3v = _make_s3vectors_mock()
        with patch("boto3.client", return_value=self.mock_s3v):
            from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
            self.store = S3VectorMemory(bucket_name="test-bucket", region_name="us-east-1")

        self.fake_embedding = [0.1] * 1024
        self.store._embed = MagicMock(return_value=self.fake_embedding)

    def _make_vector(self, key, distance, content="some content", stored_at="20250101_120000"):
        return {
            "key": key,
            "distance": distance,
            "metadata": {
                "user_id": "user1",
                "content": content,
                "stored_at": stored_at,
            },
        }

    def test_embed_called_once_with_query_and_generic_retrieval_purpose(self):
        """_embed is called exactly once with the query and purpose='GENERIC_RETRIEVAL'."""
        self.store.retrieve_memories("user1", "my query")
        self.store._embed.assert_called_once_with("my query", purpose="GENERIC_RETRIEVAL")

    def test_query_vectors_called_with_correct_filter_and_flags(self):
        """query_vectors is called with filter={'user_id': user_id}, returnDistance=True, returnMetadata=True."""
        self.store.retrieve_memories("user1", "my query")
        call_kwargs = self.mock_s3v.query_vectors.call_args[1]
        assert call_kwargs["filter"] == {"user_id": "user1"}
        assert call_kwargs["returnDistance"] is True
        assert call_kwargs["returnMetadata"] is True

    def test_similarity_formula_applied_to_each_result(self):
        """similarity = round(1.0 - (distance / 2.0), 3) for each result."""
        self.mock_s3v.query_vectors.return_value = {
            "vectors": [
                self._make_vector("k1", 0.4),
                self._make_vector("k2", 1.0),
                self._make_vector("k3", 0.0),
            ]
        }
        results = self.store.retrieve_memories("user1", "query")
        similarities = {r["key"]: r["similarity"] for r in results}
        assert similarities["k1"] == round(1.0 - (0.4 / 2.0), 3)
        assert similarities["k2"] == round(1.0 - (1.0 / 2.0), 3)
        assert similarities["k3"] == round(1.0 - (0.0 / 2.0), 3)

    def test_results_sorted_by_similarity_descending(self):
        """Results are returned sorted by similarity in descending order."""
        self.mock_s3v.query_vectors.return_value = {
            "vectors": [
                self._make_vector("k_low", 1.6),    # similarity 0.2
                self._make_vector("k_high", 0.2),   # similarity 0.9
                self._make_vector("k_mid", 1.0),    # similarity 0.5
            ]
        }
        results = self.store.retrieve_memories("user1", "query")
        sims = [r["similarity"] for r in results]
        assert sims == sorted(sims, reverse=True)

    def test_empty_vectors_list_returns_empty_list(self):
        """When query_vectors returns empty vectors, retrieve_memories returns []."""
        self.mock_s3v.query_vectors.return_value = {"vectors": []}
        results = self.store.retrieve_memories("user1", "query")
        assert results == []

    def test_top_k_passed_to_query_vectors(self):
        """top_k=3 is passed as topK=3 to query_vectors."""
        self.store.retrieve_memories("user1", "query", top_k=3)
        call_kwargs = self.mock_s3v.query_vectors.call_args[1]
        assert call_kwargs["topK"] == 3

    def test_result_shape_contains_expected_fields(self):
        """Each result dict contains key, content, similarity, and stored_at."""
        self.mock_s3v.query_vectors.return_value = {
            "vectors": [self._make_vector("k1", 0.5, "test content", "20250101_120000")]
        }
        results = self.store.retrieve_memories("user1", "query")
        assert len(results) == 1
        r = results[0]
        assert r["key"] == "k1"
        assert r["content"] == "test content"
        assert r["stored_at"] == "20250101_120000"
        assert "similarity" in r

    def test_retrieve_memories_filter_includes_agent_name_when_provided(self):
        """query_vectors filter uses $and when agent_name is provided."""
        self.store.retrieve_memories("user1", "query", agent_name="researcher")
        call_kwargs = self.mock_s3v.query_vectors.call_args[1]
        assert call_kwargs["filter"] == {"$and": [{"user_id": "user1"}, {"agent_name": "researcher"}]}

    def test_retrieve_memories_filter_excludes_agent_name_when_none(self):
        """query_vectors filter does not include agent_name when None (cross-agent access)."""
        self.store.retrieve_memories("user1", "query", agent_name=None)
        call_kwargs = self.mock_s3v.query_vectors.call_args[1]
        assert call_kwargs["filter"] == {"user_id": "user1"}


# ---------------------------------------------------------------------------
# Task 2.6 — TestS3VectorMemoryEmbed
# Requirements: 5.1, 5.2, 5.3
# ---------------------------------------------------------------------------

class TestS3VectorMemoryEmbed:
    """_embed creates a bedrock-runtime client per call and returns the embedding."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Create a store with a mocked s3vectors client at init time."""
        with patch("boto3.client", return_value=_make_s3vectors_mock()):
            from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
            self.store = S3VectorMemory(
                bucket_name="test-bucket",
                region_name="us-east-1",
                embedding_model="amazon.nova-2-multimodal-embeddings-v1:0",
            )

    def test_bedrock_client_created_per_embed_call(self):
        """boto3.client('bedrock-runtime') is called once per _embed invocation."""
        mock_bedrock = _make_bedrock_mock()
        with patch("boto3.client", return_value=mock_bedrock) as mock_boto3:
            self.store._embed("hello", purpose="GENERIC_INDEX")
            mock_boto3.assert_called_once_with("bedrock-runtime", region_name="us-east-1")

    def test_invoke_model_called_with_correct_model_id(self):
        """invoke_model is called with the configured embedding_model as modelId."""
        mock_bedrock = _make_bedrock_mock()
        with patch("boto3.client", return_value=mock_bedrock):
            self.store._embed("hello", purpose="GENERIC_INDEX")
        call_kwargs = mock_bedrock.invoke_model.call_args[1]
        assert call_kwargs["modelId"] == "amazon.nova-2-multimodal-embeddings-v1:0"

    def test_invoke_model_body_has_correct_task_type(self):
        """invoke_model body contains taskType='SINGLE_EMBEDDING'."""
        mock_bedrock = _make_bedrock_mock()
        with patch("boto3.client", return_value=mock_bedrock):
            self.store._embed("hello", purpose="GENERIC_INDEX")
        call_kwargs = mock_bedrock.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["taskType"] == "SINGLE_EMBEDDING"

    def test_invoke_model_body_has_correct_embedding_purpose(self):
        """invoke_model body contains the provided embeddingPurpose."""
        mock_bedrock = _make_bedrock_mock()
        with patch("boto3.client", return_value=mock_bedrock):
            self.store._embed("hello", purpose="GENERIC_RETRIEVAL")
        call_kwargs = mock_bedrock.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        purpose = body["singleEmbeddingParams"]["embeddingPurpose"]
        assert purpose == "GENERIC_RETRIEVAL"

    def test_invoke_model_body_has_embedding_dimension_1024(self):
        """invoke_model body contains embeddingDimension=1024."""
        mock_bedrock = _make_bedrock_mock()
        with patch("boto3.client", return_value=mock_bedrock):
            self.store._embed("hello", purpose="GENERIC_INDEX")
        call_kwargs = mock_bedrock.invoke_model.call_args[1]
        body = json.loads(call_kwargs["body"])
        dim = body["singleEmbeddingParams"]["embeddingDimension"]
        assert dim == 1024

    def test_returns_embeddings_0_embedding(self):
        """_embed returns embeddings[0]['embedding'] from the invoke_model response."""
        expected = [float(i) / 1024 for i in range(1024)]
        mock_bedrock = _make_bedrock_mock(embedding=expected)
        with patch("boto3.client", return_value=mock_bedrock):
            result = self.store._embed("hello", purpose="GENERIC_INDEX")
        assert result == expected

    def test_two_embed_calls_create_two_bedrock_clients(self):
        """Each _embed call creates a fresh bedrock-runtime client (thread-safety)."""
        mock_bedrock = _make_bedrock_mock()
        with patch("boto3.client", return_value=mock_bedrock) as mock_boto3:
            self.store._embed("first", purpose="GENERIC_INDEX")
            self.store._embed("second", purpose="GENERIC_RETRIEVAL")
        assert mock_boto3.call_count == 2


# ---------------------------------------------------------------------------
# Task 2.7 — TestMultiTenantS3VectorMemory
# Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7
# ---------------------------------------------------------------------------

class TestMultiTenantS3VectorMemory:
    """MultiTenantS3VectorMemory enforces per-tenant index naming and client isolation."""

    def _make_store(self, tvm_role_arn=None, mock_tvm_cls=None):
        """Construct a MultiTenantS3VectorMemory with all external deps mocked.

        TokenVendingMachine is imported inside __init__ via a local import, so we
        patch it at the token_vending_machine module level using create=True to
        intercept the 'from token_vending_machine import TokenVendingMachine' call.
        """
        mock_s3v = _make_s3vectors_mock()
        tvm_patch = mock_tvm_cls or MagicMock()
        with patch("boto3.client", return_value=mock_s3v), \
             patch("strands_s3_vectors_memory.token_vending_machine.TokenVendingMachine", tvm_patch, create=True):
            from strands_s3_vectors_memory.s3_vector_memory import MultiTenantS3VectorMemory
            store = MultiTenantS3VectorMemory(
                bucket_name="test-bucket",
                region_name="us-east-1",
                tvm_role_arn=tvm_role_arn,
            )
        store._s3vectors = mock_s3v
        return store

    def test_build_index_name_with_tenant_id(self):
        """_build_index_name({'tenantId': 'acme'}) returns 'memory-acme'."""
        store = self._make_store()
        assert store._build_index_name({"tenantId": "acme"}) == "memory-acme"

    def test_build_index_name_empty_dict_raises_value_error(self):
        """_build_index_name({}) raises ValueError with message containing 'tenantId'."""
        store = self._make_store()
        with pytest.raises(ValueError, match="tenantId"):
            store._build_index_name({})

    def test_build_index_name_none_raises_value_error(self):
        """_build_index_name(None) raises ValueError."""
        store = self._make_store()
        with pytest.raises(ValueError):
            store._build_index_name(None)

    def test_two_distinct_tenant_ids_produce_distinct_index_names(self):
        """Two different tenant IDs produce two different index names."""
        store = self._make_store()
        name_a = store._build_index_name({"tenantId": "tenant-a"})
        name_b = store._build_index_name({"tenantId": "tenant-b"})
        assert name_a != name_b

    def test_tvm_role_arn_provided_sets_isolation_to_tvm_instance(self):
        """When tvm_role_arn is provided, _isolation is a TokenVendingMachine instance."""
        mock_tvm_cls = MagicMock()
        mock_tvm_instance = MagicMock()
        mock_tvm_cls.return_value = mock_tvm_instance

        store = self._make_store(tvm_role_arn="arn:aws:iam::123:role/R", mock_tvm_cls=mock_tvm_cls)

        mock_tvm_cls.assert_called_once_with(
            role_arn="arn:aws:iam::123:role/R", region_name="us-east-1"
        )
        assert store._isolation is mock_tvm_instance

    def test_tvm_role_arn_none_sets_isolation_to_none(self):
        """When tvm_role_arn is None, _isolation is None."""
        store = self._make_store(tvm_role_arn=None)
        assert store._isolation is None

    def test_get_s3vectors_client_raises_when_no_isolation_no_tenant_context(self):
        """When _isolation is None and no tenant_context is provided, raises ValueError.
        MultiTenantS3VectorMemory has no legitimate ambient-credential path."""
        store = self._make_store(tvm_role_arn=None)
        with pytest.raises(ValueError, match="tvm_role_arn"):
            store._get_s3vectors_client(None)

    def test_get_s3vectors_client_raises_when_no_isolation_empty_tenant_context(self):
        """When _isolation is None and tenant_context has no tenantId, raises ValueError."""
        store = self._make_store(tvm_role_arn=None)
        with pytest.raises(ValueError, match="tvm_role_arn"):
            store._get_s3vectors_client({})

    def test_get_s3vectors_client_raises_when_no_isolation_but_tenant_context_provided(self):
        """When _isolation is None but tenant_context is provided, raises ValueError (issue #5)."""
        import pytest
        store = self._make_store(tvm_role_arn=None)
        with pytest.raises(ValueError, match="tvm_role_arn"):
            store._get_s3vectors_client({"tenantId": "acme"})

    def test_get_s3vectors_client_delegates_to_isolation_when_set(self):
        """When _isolation is set, _get_s3vectors_client delegates to _isolation.get_client."""
        mock_tvm_cls = MagicMock()
        mock_tvm_instance = MagicMock()
        mock_tvm_cls.return_value = mock_tvm_instance
        expected_client = MagicMock()
        mock_tvm_instance.get_client.return_value = expected_client

        store = self._make_store(tvm_role_arn="arn:aws:iam::123:role/R", mock_tvm_cls=mock_tvm_cls)
        tenant_context = {"tenantId": "acme"}
        result = store._get_s3vectors_client(tenant_context)

        mock_tvm_instance.get_client.assert_called_once_with("s3vectors", tenant_context)
        assert result is expected_client


# ---------------------------------------------------------------------------
# Issue #2 — content truncation is byte-based, not char-based
# ---------------------------------------------------------------------------

class TestStoreMemoryByteTruncation:
    """Issue #2: store_memory must truncate content to 4096 UTF-8 bytes, not chars."""

    @pytest.fixture(autouse=True)
    def setup(self):
        mock_s3v = _make_s3vectors_mock()
        with patch("boto3.client", return_value=mock_s3v):
            from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
            self.store = S3VectorMemory(bucket_name="b", region_name="us-east-1")
        self.mock_s3v = mock_s3v
        self.store._embed = MagicMock(return_value=[0.1] * 1024)

    def test_multibyte_content_stored_within_4096_bytes(self):
        """4096 emoji chars (4 bytes each = 16384 bytes) must be truncated to <= 4096 bytes."""
        self.store.store_memory("u1", "😀" * 4096)
        stored = self.mock_s3v.put_vectors.call_args[1]["vectors"][0]["metadata"]["content"]
        assert len(stored.encode("utf-8")) <= 4096


# ---------------------------------------------------------------------------
# Issue #3 — empty query guard in retrieve_memories
# ---------------------------------------------------------------------------

class TestRetrieveMemoriesEmptyQuery:
    """Issue #3: retrieve_memories with empty query must return [] without calling _embed."""

    @pytest.fixture(autouse=True)
    def setup(self):
        with patch("boto3.client", return_value=_make_s3vectors_mock()):
            from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
            self.store = S3VectorMemory(bucket_name="b", region_name="us-east-1")
        self.store._embed = MagicMock(return_value=[0.1] * 1024)

    def test_empty_string_returns_empty_list_without_embed(self):
        try:
            result = self.store.retrieve_memories("u1", "")
            assert result == []
            self.store._embed.assert_not_called()
        except ValueError:
            pass  # explicit rejection is also acceptable

    def test_whitespace_only_returns_empty_list_without_embed(self):
        try:
            result = self.store.retrieve_memories("u1", "   ")
            assert result == []
            self.store._embed.assert_not_called()
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Issue #5 — silent ambient fallback warning in multi-tenant mode
# ---------------------------------------------------------------------------

class TestMultiTenantAmbientFallbackWarning:
    """Issue #5: _get_s3vectors_client must raise ValueError whenever no TVM role is set,
    regardless of whether tenant_context is provided."""

    def _make_mt_store(self):
        with patch("boto3.client", return_value=_make_s3vectors_mock()), \
             patch("strands_s3_vectors_memory.token_vending_machine.TokenVendingMachine", MagicMock(), create=True):
            from strands_s3_vectors_memory.s3_vector_memory import MultiTenantS3VectorMemory
            return MultiTenantS3VectorMemory(bucket_name="b", region_name="us-east-1", tvm_role_arn=None)

    def test_no_tvm_role_with_tenant_context_raises_value_error(self):
        """Raises ValueError when tenant_context with tenantId is provided but no TVM role."""
        store = self._make_mt_store()
        with pytest.raises(ValueError, match="tvm_role_arn"):
            store._get_s3vectors_client({"tenantId": "tenant-x"})

    def test_no_tvm_role_without_tenant_context_raises_value_error(self):
        """Raises ValueError even when no tenant_context is provided — no ambient fallback."""
        store = self._make_mt_store()
        with pytest.raises(ValueError, match="tvm_role_arn"):
            store._get_s3vectors_client(None)

    def test_no_tvm_role_empty_tenant_context_raises_value_error(self):
        """Raises ValueError even when tenant_context is an empty dict."""
        store = self._make_mt_store()
        with pytest.raises(ValueError, match="tvm_role_arn"):
            store._get_s3vectors_client({})


# ---------------------------------------------------------------------------
# Issue #6 — vector key length cap
# ---------------------------------------------------------------------------

class TestStoreMemoryKeyLengthCap:
    """Issue #6: vector key must be capped to 512 chars regardless of user_id length."""

    @pytest.fixture(autouse=True)
    def setup(self):
        with patch("boto3.client", return_value=_make_s3vectors_mock()):
            from strands_s3_vectors_memory.s3_vector_memory import S3VectorMemory
            self.store = S3VectorMemory(bucket_name="b", region_name="us-east-1")
        self.store._embed = MagicMock(return_value=[0.1] * 1024)

    def test_600_char_user_id_produces_key_within_512_chars(self):
        result = self.store.store_memory("u" * 600, "content")
        assert len(result["key"]) <= 512
