"""Per-org reconciliation: is V2 a faithful copy of V1 for this organisation?

1. Row counts match.
2. Checksums over (id, channel, sender, sent_at, content hash) match for every id range,
   with the V1 side's hash computed in SQL, independently of the migration's Python.
   Per-range checksums keep each aggregate small (PostgreSQL text values cap at 1 GB) and
   say where a mismatch is.
3. Every content_ref resolves to an object in the org's bucket.
4. A sample of objects round-trips: S3 body == V1 content.
5. Every object in the tenant's V1 shared prefix was copied, and the copy is the same object
   (size and ETag), not just a key with the same name.

Reads the V1 **writer** (V1_MESSAGING_DSN), never a replica: replica lag would make a missing
row look like a matching count.
"""

import psycopg

from tenantmig import identities, settings
from tenantmig.hashing import SQL_CONTENT_REF, Sha256Hasher
from tenantmig.store import ContentStore, org_bucket

RANGE_SIZE = 50_000      # ids per checksum range; about 5 MB of text per aggregate at 100 bytes a row

# to_char with an explicit pattern, not extract(epoch)::text: the text form of a double
# changed in PostgreSQL 12, so a legacy V1 and a modern cell would disagree on identical data.
# Each free-text field is length-prefixed, so no two different rows can serialise to the same
# string: without that, a '|' or ',' inside channel or sender can move a field boundary and collide.
CHECKSUM = """
SELECT id / %(rs)s AS range_no, count(*), md5(string_agg(
    id || '|' || length(channel) || ':' || channel
       || '|' || length(sender) || ':' || sender
       || '|' || to_char(sent_at   AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')
       || '|' || to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')
       || '|' || {ref},
    ',' ORDER BY id))
FROM messages WHERE org_id = %(org)s GROUP BY 1
"""


def _ranges(conn, ref_expr: str, org_id: str) -> dict:
    return {r[0]: (r[1], r[2]) for r in conn.execute(CHECKSUM.format(ref=ref_expr), {"rs": RANGE_SIZE, "org": org_id})}


def reconcile_org(org_id: str, sample: int = 25, store: ContentStore | None = None) -> dict:
    store = store or ContentStore()
    hasher = Sha256Hasher()
    bucket = org_bucket(org_id)

    with psycopg.connect(identities.tenant_dsn(settings.V1_MESSAGING_DSN, org_id)) as v1, \
            psycopg.connect(identities.tenant_dsn(settings.V2_CELL_MESSAGING_DSN, org_id)) as v2:
        r1, r2 = _ranges(v1, SQL_CONTENT_REF, org_id), _ranges(v2, "content_ref", org_id)
        v1_count, v2_count = sum(c for c, _ in r1.values()), sum(c for c, _ in r2.values())
        bad_ranges = sorted(k for k in set(r1) | set(r2) if r1.get(k) != r2.get(k))
        refs = {r[0] for r in v2.execute("SELECT DISTINCT content_ref FROM messages WHERE org_id = %s", (org_id,))}
        sample_ids = [r[0] for r in v2.execute(
            "SELECT id FROM messages WHERE org_id = %s ORDER BY md5(id::text) LIMIT %s", (org_id, sample))]
        samples = v1.execute("SELECT id, content FROM messages WHERE id = ANY(%s)", (sample_ids,)).fetchall()
        v2_refs = dict(v2.execute("SELECT id, content_ref FROM messages WHERE id = ANY(%s)", (sample_ids,)).fetchall())

    keys = {k for k in store.list_keys(bucket) if not k.startswith("originals/")}
    missing = [r for r in refs if hasher.object_key(r) not in keys]
    source = store.list_objects(settings.V1_SHARED_BUCKET, prefix=f"{org_id}/")
    copies = store.list_objects(bucket, prefix="originals/")
    missing_originals = sorted(k for k, meta in source.items()
                               if f"originals/{k}" not in copies or not store.same_content(
                                   settings.V1_SHARED_BUCKET, k, meta, bucket, f"originals/{k}", copies[f"originals/{k}"]))
    roundtrip_failures = [mid for mid, content in samples
                          if store.get_text(bucket, hasher.object_key(v2_refs[mid])) != content]

    checks = {
        "row_counts_match": v1_count == v2_count,
        "checksums_match": not bad_ranges,
        "all_refs_resolve": not missing,
        "sample_roundtrip_ok": not roundtrip_failures,
        "all_shared_objects_copied": not missing_originals,
    }
    return {
        "org_id": org_id, "passed": all(checks.values()), **checks,
        "v1_rows": v1_count, "v2_rows": v2_count,
        "unique_content_objects": len(refs), "objects_in_bucket": len(keys),
        "dedupe_ratio": round(v2_count / len(refs), 2) if refs else None,
        "shared_bucket_objects": len(source), "originals_copied": len(copies),
        "mismatched_id_ranges": [[k * RANGE_SIZE, (k + 1) * RANGE_SIZE - 1] for k in bad_ranges[:10]],
        "missing_refs": missing[:10], "missing_originals": missing_originals[:10], "roundtrip_failures": roundtrip_failures[:10],
    }
