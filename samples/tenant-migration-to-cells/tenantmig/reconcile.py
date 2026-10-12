"""Per-org reconciliation: is V2 a faithful copy of V1 for this organisation?

1. Row counts match.
2. Order-independent checksum over (id, channel, sender, sent_at, content hash) matches,
   with the V1 side's hash computed in SQL, independently of the migration's Python.
3. Every content_ref resolves to an object in the org's bucket.
4. A sample of objects round-trips: S3 body == V1 content.
"""
import random

import psycopg

from tenantmig import settings
from tenantmig.hashing import SQL_CONTENT_REF, Sha256Hasher
from tenantmig.store import ContentStore, org_bucket

CHECKSUM = """
SELECT count(*), coalesce(md5(string_agg(
    id || '|' || channel || '|' || sender || '|' || extract(epoch FROM sent_at)::text || '|' || {ref},
    ',' ORDER BY id)), '')
FROM messages WHERE org_id = %s
"""


def reconcile_org(org_id: str, sample: int = 25, store: ContentStore | None = None) -> dict:
    store = store or ContentStore()
    hasher = Sha256Hasher()
    bucket = org_bucket(org_id)

    with psycopg.connect(settings.V1_MESSAGING_DSN) as v1, psycopg.connect(settings.V2_CELL_MESSAGING_DSN) as v2:
        v1_count, v1_sum = v1.execute(CHECKSUM.format(ref=SQL_CONTENT_REF), (org_id,)).fetchone()
        v2_count, v2_sum = v2.execute(CHECKSUM.format(ref="content_ref"), (org_id,)).fetchone()
        refs = {r[0] for r in v2.execute("SELECT DISTINCT content_ref FROM messages WHERE org_id = %s", (org_id,))}
        ids = [r[0] for r in v2.execute("SELECT id FROM messages WHERE org_id = %s", (org_id,))]
        sample_ids = random.Random(1).sample(ids, k=min(sample, len(ids)))
        samples = v1.execute("SELECT id, content FROM messages WHERE id = ANY(%s)", (sample_ids,)).fetchall()
        v2_refs = dict(v2.execute("SELECT id, content_ref FROM messages WHERE id = ANY(%s)", (sample_ids,)).fetchall())

    keys = store.list_keys(bucket)
    originals = {k for k in keys if k.startswith("originals/")}
    keys -= originals
    missing = [r for r in refs if hasher.object_key(r) not in keys]
    source_objects = set()
    for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=settings.V1_SHARED_BUCKET, Prefix=f"{org_id}/"):
        source_objects.update(o["Key"] for o in page.get("Contents", []) if not o["Key"].startswith("_"))
    missing_originals = sorted(k for k in source_objects if f"originals/{k}" not in originals)
    roundtrip_failures = [mid for mid, content in samples
                          if store.get_text(bucket, hasher.object_key(v2_refs[mid])) != content]

    checks = {
        "row_counts_match": v1_count == v2_count,
        "checksums_match": v1_sum == v2_sum,
        "all_refs_resolve": not missing,
        "sample_roundtrip_ok": not roundtrip_failures,
        "all_shared_objects_copied": not missing_originals,
    }
    return {
        "org_id": org_id, "passed": all(checks.values()), **checks,
        "v1_rows": v1_count, "v2_rows": v2_count,
        "unique_content_objects": len(refs), "objects_in_bucket": len(keys),
        "dedupe_ratio": round(v2_count / len(refs), 2) if refs else None,
        "shared_bucket_objects": len(source_objects), "originals_copied": len(originals),
        "missing_refs": missing[:10], "missing_originals": missing_originals[:10], "roundtrip_failures": roundtrip_failures[:10],
    }
