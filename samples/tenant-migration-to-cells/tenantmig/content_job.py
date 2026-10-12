"""Per-org messaging migration: content leaves Postgres for S3, rows land with content_ref.

The same code path runs the bulk pass and every delta pass. A per-org watermark on
messages.id means re-runs pick up only new rows, and conditional PUTs mean re-runs never
rewrite objects. Run a final pass during the org's write freeze at cutover.

Source note: locally this reads V1 directly. In AWS the bulk pass should read the RDS
snapshot export (Parquet in S3) so V1 production carries no load; deltas read a V1 read
replica with a read-only migration user.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import psycopg

from tenantmig import settings
from tenantmig.hashing import Hasher, Sha256Hasher
from tenantmig.store import ContentStore, org_bucket


@dataclass
class PassResult:
    org_id: str
    rows: int = 0
    objects_written: int = 0
    objects_deduped: int = 0
    bytes_written: int = 0
    seconds: float = 0.0
    watermark: int = 0


def migrate_org(org_id: str, batch_size: int = 1_000, hasher: Hasher | None = None,
                store: ContentStore | None = None) -> PassResult:
    hasher = hasher or Sha256Hasher()
    store = store or ContentStore()
    bucket = org_bucket(org_id)
    store.ensure_bucket(bucket)
    result = PassResult(org_id)
    started = time.monotonic()

    with psycopg.connect(settings.V1_MESSAGING_DSN) as src, psycopg.connect(settings.V2_CELL_MESSAGING_DSN) as dst:
        org = src.execute("SELECT id, name, created_at FROM organisations WHERE id = %s", (org_id,)).fetchone()
        if org is None:
            raise ValueError(f"org {org_id} not found in V1")
        dst.execute("INSERT INTO organisations VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING", org)
        dst.execute("INSERT INTO migration.org_state (org_id) VALUES (%s) ON CONFLICT DO NOTHING", (org_id,))
        # One pass per tenant at a time. A session-level advisory lock survives the per-batch commits.
        if not dst.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (org_id,)).fetchone()[0]:
            raise RuntimeError(f"another content pass is already running for org {org_id}")
        watermark = dst.execute(
            "SELECT last_message_id FROM migration.org_state WHERE org_id = %s", (org_id,)
        ).fetchone()[0]
        dst.commit()

        with src.cursor(name="msgs") as cur:          # server-side cursor: constant memory
            cur.itersize = batch_size
            cur.execute(
                "SELECT id, org_id, channel, sender, sent_at, created_at, content "
                "FROM messages WHERE org_id = %s AND id > %s ORDER BY id",
                (org_id, watermark),
            )
            batch = []
            for row in cur:
                batch.append(row)
                if len(batch) >= batch_size:
                    watermark = _flush(org_id, batch, bucket, hasher, store, dst, result)
                    batch = []
            if batch:
                watermark = _flush(org_id, batch, bucket, hasher, store, dst, result)

        dst.execute("UPDATE migration.org_state SET passes = passes + 1, updated_at = now() WHERE org_id = %s",
                    (org_id,))
    result.watermark = watermark
    result.seconds = round(time.monotonic() - started, 2)
    return result


def _flush(org_id, batch, bucket, hasher: Hasher, store: ContentStore, dst, result: PassResult) -> int:
    rows, unique = [], {}
    for (mid, org, channel, sender, sent_at, created_at, content) in batch:
        ref = hasher.content_ref(content)
        if ref in unique:                       # duplicate within the batch: no second PUT
            result.objects_deduped += 1
        else:
            unique[ref] = content.encode("utf-8")
        rows.append((mid, org, channel, sender, sent_at, created_at, ref))

    # Objects first, rows second: a row never points at content that isn't there.
    def put(item):
        ref, body = item
        return store.put_if_absent(bucket, hasher.object_key(ref), body), len(body)

    with ThreadPoolExecutor(max_workers=settings.S3_WORKERS) as pool:
        for written, size in pool.map(put, unique.items()):
            if written:
                result.objects_written += 1
                result.bytes_written += size
            else:
                result.objects_deduped += 1

    with dst.cursor().copy(
        "COPY messages (id, org_id, channel, sender, sent_at, created_at, content_ref) FROM STDIN"
    ) as copy:
        for r in rows:
            copy.write_row(r)
    # Rows and watermark commit together, so a pass that crashes mid-way re-runs from the last
    # committed batch: no duplicate rows, and objects already written are skipped by the PUT.
    dst.execute("UPDATE migration.org_state SET last_message_id = %s, updated_at = now() WHERE org_id = %s",
                (batch[-1][0], org_id))
    dst.commit()
    result.rows += len(rows)
    return batch[-1][0]
