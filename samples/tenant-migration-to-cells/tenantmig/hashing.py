"""content_ref generation.

Replace with your own: if the target application computes content_ref itself, the migration
must call exactly that code (same algorithm, encoding and normalisation) or refs will not
resolve. Swap `Sha256Hasher` for a thin wrapper around your implementation.
"""
import hashlib
from typing import Protocol


class Hasher(Protocol):
    def content_ref(self, content: str) -> str: ...
    def object_key(self, content_ref: str) -> str: ...


class Sha256Hasher:
    """Assumed default: SHA-256 of UTF-8 bytes, hex; key sharded by the first two hex chars."""

    def content_ref(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def object_key(self, content_ref: str) -> str:
        return f"{content_ref[:2]}/{content_ref}"


# SQL expression that must agree with Sha256Hasher.content_ref, used by reconciliation.
SQL_CONTENT_REF = "encode(sha256(convert_to(content, 'UTF8')), 'hex')"
