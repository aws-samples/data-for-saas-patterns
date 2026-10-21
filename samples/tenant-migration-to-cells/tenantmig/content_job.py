"""Per-org messaging migration: content leaves Postgres for S3, rows land with content_ref.

The same code path runs the bulk pass, every delta pass and the final pass at cutover. A
per-org watermark on messages.id means re-runs pick up only new rows, and conditional PUTs
mean re-runs never store a second copy of the same content.

Source note: locally this reads V1 directly. In AWS the bulk and delta passes can read a V1
read replica (set V1_MESSAGING_READ_DSN), or the bulk pass can read an RDS snapshot export,
so the V1 writer carries no load. The final pass and reconciliation always read the writer
(V1_MESSAGING_DSN): replica lag at cutover would silently drop rows.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import psycopg

from tenantmig import identities, placement, settings
from tenantmig.hashing import Hasher, Sha256Hasher
from tenantmig.store import ContentStore, org_bucket

COLUMNS = "id, org_id, channel, sender, sent_at, created_at"


@dataclass
class PassResult:
    org_id: str
    rows: int = 0
    objects_written: int = 0
    objects_deduped: int = 0
    bytes_written: int = 0
    seconds: float = 0.0
    watermark: int = 0
    backfilled: int = 0          # rows the cell was missing (final or repair pass)
    recopied: int = 0            # rows re-copied because they differ from V1 (repair)
    deleted_extras: int = 0      # cell rows whose V1 row is gone (repair)
    held_back: int = 0           # rows inside the safety margin, left for the next pass


@placement.guarded("run a content pass")
def migrate_org(org_id: str, batch_size: int = 1_000, hasher: Hasher | None = None,
                store: ContentStore | None = None, final: bool = False, repair: bool = False) -> PassResult:
    """One pass for one tenant.

    Normal (bulk and delta) passes copy rows after the watermark, holding back anything
    created within DELTA_SAFETY_SECONDS and never advancing the watermark past a held-back
    row, so a lower id that commits late is still picked up by the next pass.

    `final` (used by cutover, with writes frozen) drops the margin, reads the V1 writer, and
    then anti-joins V1 against the cell to copy anything still missing.

    `repair` fixes what reconciliation reports: it backfills missing rows, re-copies rows that
    differ from V1, and deletes cell rows whose V1 row is gone.
    """
    hasher = hasher or Sha256Hasher()
    store = store or ContentStore()
    bucket = org_bucket(org_id)
    store.ensure_bucket(bucket)
    result = PassResult(org_id)
    started = time.monotonic()
    # The final pass and repair must see every committed row, so they read the writer even
    # when the delta passes are pointed at a replica.
    src_dsn = settings.V1_MESSAGING_DSN if (final or repair) else settings.V1_MESSAGING_READ_DSN

    with psycopg.connect(identities.tenant_dsn(src_dsn, org_id)) as src, \
            psycopg.connect(identities.tenant_dsn(settings.V2_CELL_MESSAGING_DSN, org_id)) as dst:
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

        if not repair:
            # Hold back rows still inside the safety margin, and stop the watermark below the
            # lowest of them: ids are not committed in order, so advancing past a held-back row
            # would skip it for good. The final pass has writes frozen and uses no margin.
            margin = 0 if final else settings.DELTA_SAFETY_SECONDS
            ceiling = None
            if margin:
                row = src.execute(
                    "SELECT min(id), count(*) FROM messages "
                    "WHERE org_id = %s AND created_at >= now() - make_interval(secs => %s)",
                    (org_id, margin)).fetchone()
                if row[0] is not None:
                    ceiling, result.held_back = row[0] - 1, row[1]

            with src.cursor(name="msgs") as cur:          # server-side cursor: constant memory
                cur.itersize = batch_size
                cur.execute(
                    f"SELECT {COLUMNS}, content FROM messages WHERE org_id = %s AND id > %s "
                    "AND (%s::bigint IS NULL OR id <= %s) ORDER BY id",
                    (org_id, watermark, ceiling, ceiling),
                )
                batch = []
                for row in cur:
                    batch.append(row)
                    if len(batch) >= batch_size:
                        watermark = _flush(org_id, batch, bucket, hasher, store, dst, result)
                        batch = []
                if batch:
                    watermark = _flush(org_id, batch, bucket, hasher, store, dst, result)

        if final or repair:
            watermark = max(watermark, _repair(org_id, src, dst, bucket, hasher, store, result, batch_size,
                                               full=repair))

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

    # Batched INSERT, not COPY: PostgreSQL refuses COPY into a table with row-level security for
    # a role that isn't exempt from it, and the app role isn't.
    dst.cursor().executemany(f"INSERT INTO messages ({COLUMNS}, content_ref) "
                             "VALUES (%s, %s, %s, %s, %s, %s, %s)", rows)
    # Rows and watermark commit together, so a pass that crashes mid-way re-runs from the last
    # committed batch: no duplicate rows, and objects already written are skipped by the PUT.
    dst.execute("UPDATE migration.org_state SET last_message_id = GREATEST(last_message_id, %s), "
                "updated_at = now() WHERE org_id = %s", (batch[-1][0], org_id))
    dst.commit()
    result.rows += len(rows)
    return batch[-1][0]


def _repair(org_id, src, dst, bucket, hasher: Hasher, store: ContentStore, result: PassResult,
            batch_size: int, full: bool, range_size: int = 100_000) -> int:
    """Converge the cell on V1 for this tenant, one id range at a time.

    Always: copy V1 rows the cell is missing (the anti-join the final pass needs).
    With full=True (`--repair`): also re-copy rows whose V1 values differ from the cell's, and
    delete cell rows whose V1 row is gone. Those are the cases a watermark can never fix,
    because V1 changed under rows that were already copied.
    """
    lo, hi = src.execute("SELECT coalesce(min(id), 0), coalesce(max(id), -1) FROM messages WHERE org_id = %s",
                         (org_id,)).fetchone()
    if full:
        # Extras can sit outside V1's id span entirely (a deleted lowest or highest row), so
        # the scan has to cover the union of both sides or it would never see them.
        clo, chi = dst.execute("SELECT coalesce(min(id), %s), coalesce(max(id), -1) FROM messages "
                               "WHERE org_id = %s", (lo, org_id)).fetchone()
        lo, hi = min(lo, clo), max(hi, chi)
    top = 0
    for start in range(lo, hi + 1, range_size):
        end = start + range_size
        # A full repair compares content too, as its hash: a message whose content changed in V1
        # but whose metadata didn't would otherwise never be recopied, and reconcile would fail forever.
        v1 = {r[0]: r[1:] for r in src.execute(
            f"SELECT {COLUMNS}{', content' if full else ''} FROM messages WHERE org_id = %s AND id >= %s AND id < %s",
            (org_id, start, end))}
        if full:
            v1 = {mid: row[:-1] + (hasher.content_ref(row[-1]),) for mid, row in v1.items()}
        cell = {r[0]: r[1:] for r in dst.execute(
            f"SELECT {COLUMNS}{', content_ref' if full else ''} FROM messages WHERE org_id = %s AND id >= %s AND id < %s",
            (org_id, start, end))}

        ids = sorted(set(v1) - set(cell))
        changed = sorted(i for i in set(v1) & set(cell) if full and v1[i] != cell[i]) if full else []
        extras = sorted(set(cell) - set(v1)) if full else []

        if extras:
            dst.execute("DELETE FROM messages WHERE org_id = %s AND id = ANY(%s)", (org_id, extras))
            dst.commit()
            result.deleted_extras += len(extras)
        if changed:
            dst.execute("DELETE FROM messages WHERE org_id = %s AND id = ANY(%s)", (org_id, changed))
            dst.commit()
            result.recopied += len(changed)

        todo = sorted(ids + changed)
        for i in range(0, len(todo), batch_size):
            chunk = todo[i:i + batch_size]
            batch = src.execute(f"SELECT {COLUMNS}, content FROM messages WHERE org_id = %s AND id = ANY(%s) "
                                "ORDER BY id", (org_id, chunk)).fetchall()
            if batch:
                top = max(top, _flush(org_id, batch, bucket, hasher, store, dst, result))
        result.backfilled += len(ids)
    return top
