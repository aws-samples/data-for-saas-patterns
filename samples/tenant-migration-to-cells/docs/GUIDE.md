# Tenant migration into a cell: the complete guide

This guide explains the pattern behind the sample, walks through a run step by step, shows how each step works, and gives a runbook for running the same approach in production. For a quick start, see the [README](../README.md).

**Contents**

1. [When to use this](#1-when-to-use-this)
2. [Architecture](#2-architecture)
3. [Concepts: Clone, Flip, Redirect, Forget](#3-concepts-clone-flip-redirect-forget)
4. [Walkthrough: run it locally](#4-walkthrough-run-it-locally)
5. [How each step works](#5-how-each-step-works)
6. [Run it on AWS](#6-run-it-on-aws)
7. [Production cutover runbook](#7-production-cutover-runbook)
8. [Scale and cost](#8-scale-and-cost)
9. [Security](#9-security)
10. [Adapting it to your platform](#10-adapting-it-to-your-platform)
11. [Testing](#11-testing)
12. [Troubleshooting](#12-troubleshooting)
13. [Further reading](#13-further-reading)

---

## 1. When to use this

Use this approach when you're moving **existing tenants** from one storage layout to another, one tenant at a time, and you need to prove each move is correct before it goes live. Typical cases:

* Moving from a pooled database into a [cell-based architecture](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html).
* Moving a large tenant into its own silo, or rebalancing tenants across shards. [Scale your relational database for SaaS, Part 2](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-2-sharding-and-routing/) calls this "required as part of the core architecture".
* Re-platforming where the **target schema differs** from the source. In this sample, message content moves out of PostgreSQL into Amazon S3, and each row keeps only a content-addressable reference.

If the schema doesn't change and you only need to copy tables, [PostgreSQL logical replication with row filters](https://aws.amazon.com/blogs/database/postgresql-logical-replication-how-to-replicate-only-the-data-that-you-need/) or [AWS DMS](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Source.PostgreSQL.html) may be all you need. The verification and cutover parts of this guide still apply.

## 2. Architecture

### The pattern

![Pattern](tenant-migration-to-cells.png)

| Area | V1 (source) | V2 (target) |
|---|---|---|
| Messages | Aurora PostgreSQL messaging database, `content` inline | Cell messaging database (one Aurora PostgreSQL global cluster per cell), `content_ref` only |
| Content | Inside the message rows | Per-tenant S3 bucket, key = content hash |
| Configuration and permissions | Auth database, plus platform configuration in the messaging database | Management database (separate cluster) |
| Existing objects | One shared S3 bucket, tenant ID prefix | Per-tenant S3 bucket, `originals/` |
| Routing | The application assumes V1 | `tenant_placement` decides V1 or cell |

### What the sample deploys

![Sandbox](tenant-migration-sandbox.png)

The CDK app deploys a small, cost-conscious version of the pattern:

* **Networking:** a VPC with isolated subnets for the databases and one NAT gateway for egress. This also works in accounts with [VPC Block Public Access](https://docs.aws.amazon.com/vpc/latest/userguide/security-vpc-bpa.html) in `block-ingress` mode.
* **Databases:** four Aurora PostgreSQL clusters on Serverless v2 that [scale to 0 ACU and pause when idle](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html).
* **Job runner:** an on-demand [AWS Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html) task that runs one `tenantmig` command and exits.
* **Supporting resources:** a cell KMS key, a Batch Operations role, credentials in [Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html), and job logs in CloudWatch Logs.

In production the same container would typically run as a Kubernetes Job on Amazon EKS with [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html). The sandbox uses Fargate to avoid paying for an EKS control plane.

## 3. Concepts: Clone, Flip, Redirect, Forget

The Well-Architected guidance on [cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html) describes four phases. This sample implements each one and adds an explicit verification step:

![Cutover lifecycle](tenant-migration-cutover.png)

| Phase | What happens | Command | `tenant_placement.status` |
|---|---|---|---|
| Clone | Bulk pass, deltas, configuration and permissions, shared objects. Repeatable | `migrate-content`, `migrate-config`, `migrate-objects` | `v1` |
| Verify | Permissions diff harness and reconciliation | `diff-permissions`, `reconcile` | `v1` |
| Flip | Freeze the tenant's writes, final delta, re-verify | `cutover` | `frozen` |
| Redirect | Route the tenant to its cell | `cutover` (on success) | `v2` |
| Forget | After the retention window, delete the V1 copy | `forget --confirm` | `v2` |

**One property makes this safe: V1 stays authoritative until Redirect.** Every failure before that point is fixed by setting the status back to `v1`. Nothing has to be undone in V1, and `cutover` does this automatically when re-verification fails. Every transition is recorded in `tenant_placement_log`.

## 4. Walkthrough: run it locally

**Prerequisites:**
* Python 3.11+ and [uv](https://docs.astral.sh/uv/).
* PostgreSQL 15 and 18, and MinIO. For example, `brew install postgresql@15 postgresql@18 minio`. Or use `docker compose up -d` instead.

```bash
uv venv && uv pip install -e .
./scripts/local-up.sh && source .local/env
tenantmig demo
```

`demo` generates synthetic data for three tenants and migrates the first one end to end. It resets the V2 targets first, so it only runs where `TENANTMIG_SANDBOX=1`. `local-up.sh` sets this, and so does the CDK task definition. The output below is shortened.

```text
== Step 1-3: bulk pass (content -> S3, rows -> cell messaging DB)
  "rows": 2000, "objects_written": 1294, "objects_deduped": 706

== V1 kept receiving messages for this org while the bulk pass ran...
== Step 4: delta pass (same job, picks up only new rows older than the safety margin)
  "rows": 50

== Step 5: config + permissions -> management DB
  "connectors": 3, "principals": 25, "grants": 336

== Step 6: permissions diff harness
  "decisions_checked": 600, "disagreement_count": 0      # 25 users x 6 resources x 4 actions

== Step 8: existing shared-bucket objects -> org bucket
  "source_objects": 200, "copied": 200

== Step 9: reconciliation
  "passed": true, "dedupe_ratio": 1.55 ...

READY TO CUT OVER: reconciliation passed and permissions agree.

== Flip + Redirect: freeze writes, drain 2s, final delta, re-verify, route the tenant to its cell
  "cut_over": true, "status": "v2", "cell": "cell-1", "drained_seconds": 2,
  "final_delta_rows": 10, "backfilled_rows": 0

CUT OVER: tenant ... is now served by cell-1.
```

Then try the commands one at a time:

```bash
ORG=$(tenantmig orgs | python3 -c "import json,sys; print(json.load(sys.stdin)[1])")   # a second tenant
tenantmig migrate-content $ORG
tenantmig migrate-content $ORG        # re-run: 0 rows, 0 objects
tenantmig migrate-config $ORG --inject-bug
tenantmig diff-permissions $ORG       # lists users who would gain write access V1 denies
tenantmig migrate-config $ORG         # fixed: 0 disagreements
tenantmig migrate-objects $ORG
tenantmig reconcile $ORG
tenantmig migrate-content $ORG --repair   # only copies rows the cell is missing (normally 0)
tenantmig cutover $ORG
tenantmig cutover $ORG                # refused: the tenant is already on v2
tenantmig where $ORG                  # routing decision and history
tenantmig forget $ORG                 # dry run
tenantmig forget $ORG --confirm
```

Stop the local stack with `./scripts/local-down.sh --wipe`.

## 5. How each step works

### 5.1 Content job (steps 1 to 4)

![Content job](tenant-migration-content-job.png)

For each batch of the tenant's rows after the watermark, the job does four things:

1. Computes `content_ref = hash(content)`.
2. Writes the content to `s3://<tenant-bucket>/<ref[0:2]>/<ref>` with [`If-None-Match: *`](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html). A `412` response means the object already exists, either because the content is a duplicate or because this is a re-run.
3. Uses `COPY` to load the rows into the cell database, without `content` and with `content_ref` set.
4. Advances the watermark **in the same transaction**.

Four design rules follow from this:

* **Objects before rows.** A committed row always points at an object that exists.
* **The watermark commits with the rows.** A pass that crashes resumes from the last committed batch, with no duplicate rows. The integration tests simulate this.
* **One pass per tenant.** A PostgreSQL advisory lock stops two passes running for the same tenant at once.
* **Late commits aren't lost.** Sequence IDs can commit out of order, so a row with a lower ID can commit after the watermark has passed it. Delta passes therefore only copy rows older than `DELTA_SAFETY_SECONDS` (60 by default), which keeps the watermark behind any in-flight transaction. The final pass at cutover, with writes frozen, drops the margin and then **anti-joins** V1 against the cell, one ID range at a time, to copy anything still missing. `migrate-content --repair` runs just the anti-join, which is the fix when reconciliation reports `mismatched_id_ranges`.

PUTs within a batch run concurrently (32 by default, set by `S3_WORKERS`). With small objects, per-request latency limits throughput, not bandwidth. In a test run in `us-east-2`, a bulk pass of 2,000 rows took about 74 seconds with sequential PUTs and 2 to 5 seconds with concurrent ones.

The bulk pass and every delta pass use the same code. In production, read the bulk pass from an [Aurora snapshot export to S3](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-export-snapshot.html), so the V1 writer carries no load, and read deltas from a read replica.

### 5.2 Configuration and permissions (step 5)

The management database reaches both V1 databases through [`postgres_fdw`](https://docs.aws.amazon.com/prescriptive-guidance/latest/aurora-postgresql-integration/remote-postgresql-databases.html), so the transform is a set of `INSERT ... SELECT` statements:

* **What the fdw can see:**
  * The foreign tables are declared column by column, and `connector_configs` is declared **without** its `credentials` column.
  * The fdw connects as `tenantmig_reader`, a read-only role that `tenantmig init` creates on both V1 databases, with no `SELECT` on `credentials`.
  * The V1 master credentials are never stored in the management database.
* **Connectors:** `connector_configs` becomes `connectors`. Credentials are **not** copied. Each connector gets a `secret_ref` pointing to Secrets Manager, and credentials move through a separate, audited path.
* **Permissions:** users, roles and role permissions in V1 (with `*` wildcards, and deny winning over allow) become flat allow-only `grants`.
  * Wildcards are expanded against the **application catalogue** (`catalogue_resources`, `catalogue_actions`): the full list of resource types and actions the product defines.
  * The catalogue is not derived from the rules. A resource that no rule names explicitly is still granted to a `*` role. In the synthetic data, nothing names `connector` or `approve`, and admins still get both.
  * Denies are subtracted.

Every run **converges** V2 on V1 for the tenant. Changed connectors, users and the organisation are updated, rows deleted in V1 are deleted in V2, and grants are rebuilt from scratch. A late configuration change made before the freeze is therefore picked up by the configuration pass at cutover.

### 5.3 Permissions diff harness (step 6)

For every (user, resource, action) combination in the application catalogue, the harness evaluates the **V1 model in its own code**, straight from the auth database, and compares the result with the V2 grants. It deliberately doesn't reuse the transform's SQL, so a bug in the transform can't hide itself. Any V2 grant outside the catalogue is also a disagreement. Every disagreement is listed with both decisions.

Both sides share the catalogue, and neither derives it. That closes the blind spot where the transform and the harness both build their lists of resources from the same rules, and so agree on a mistake. An integration test reintroduces that mistake and checks that the harness catches it.

`--inject-bug` makes the transform ignore wildcard denies, a realistic mistake. The harness then reports users who would gain `write` access that V1 denies.

### 5.4 Shared-bucket objects (step 8)

On AWS the job writes a CSV manifest of the tenant's prefix to a separate **migration bucket** and creates an [S3 Batch Operations Copy](https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-examples-copy.html) job into the tenant bucket under `originals/`. Reports of failed tasks go to the same migration bucket.

The job waits up to `BATCH_TIMEOUT_SECONDS` (default 3,600). The step **fails** if the job doesn't complete or any task fails. That makes `migrate-objects` exit non-zero, and makes `cutover` roll back.

Locally, MinIO has no Batch Operations, so the objects are copied directly.

### 5.5 Reconciliation (step 9)

Reconciliation checks five things. It passes only if **all** of them hold:

| Check | Why |
|---|---|
| Row counts match | Nothing lost or duplicated |
| Checksums match for every ID range: an ordered hash over (id, channel, sender, sent_at, content hash) per 50,000 IDs, with the V1 side's content hash computed **in SQL** | Values and content are identical, and the job's hashing matches the database's. Per-range checksums stay well under PostgreSQL's 1 GB text limit at any tenant size, and `mismatched_id_ranges` shows where to look |
| Every distinct `content_ref` resolves to an object | Counts and checksums can pass while objects are missing. In testing, this is the check that caught real failures |
| A sample of objects round-trips (S3 body equals V1 content) | End-to-end content check |
| Every shared-bucket object was copied | Step 8 completed |

### 5.6 Cutover (Flip and Redirect)

`cutover` runs these steps:

1. **Checks** that the tenant is on `v1`, or `frozen` from a crashed run, and takes a per-tenant advisory lock. A tenant already on `v2` is refused: cutting it over again would route a live tenant back to V1 and strand the writes made in V2.
2. Sets the tenant to `frozen`.
3. **Drains:** waits `--drain-seconds`, which defaults to `PLACEMENT_CACHE_TTL_SECONDS` (30, or 2 on the local stack). See below.
4. Runs the final content pass (no safety margin, plus the anti-join) and a configuration pass.
5. Re-copies objects if any are missing.
6. Re-runs the diff harness and reconciliation.
7. On success, sets the tenant to `v2` with its cell. If verification fails **or any step raises an exception** (an S3 error, a database timeout, a Batch Operations failure), sets it back to `v1` and records why. The tenant is never left `frozen`.

**Why the drain.** If the router caches `tenant_placement`, it can keep accepting V1 writes for a tenant until its cache refreshes. Those writes have to land before the final pass, or they never reach V2. This sample's choice is to **wait at least the router's cache TTL** after freezing. Set `PLACEMENT_CACHE_TTL_SECONDS` (or `--drain-seconds`) to your router's TTL plus a margin.

The stronger alternative is to **block the tenant's writes on the V1 side**, for example by revoking its write role or adding a check on insert, and then use `--drain-seconds 0`. Use that if your router can't bound its cache age.

`where <org>` shows the routing decision and the full history.

The router itself is part of your application. It should read `tenant_placement` (or a cached copy):
* `v1`: serve from V1.
* `frozen`: serve reads from V1 and reject or queue writes.
* `v2`: serve from the cell.

A DynamoDB mapping table, as in the [sharding reference architecture](../../../reference-architectures/relational-database-sharding.md), works equally well.

### 5.7 Forget

`forget <org>` is a dry run that lists what would be deleted. `forget <org> --confirm` deletes the tenant's V1 rows (messages in committed chunks of 10,000, not one huge transaction) and shared-bucket objects. It fails if S3 reports any object it couldn't delete, and it's safe to re-run. It refuses unless the tenant is `v2`. Run it only after your retention and rollback window.

## 6. Run it on AWS

```bash
cd infra && npm install
npx cdk bootstrap                                  # once per account and Region
npx cdk deploy                                     # optional: -c notifyEmail=you@example.com
cd .. && ./scripts/aws-run.sh init                 # create schemas, catalogue and the read-only fdw role
./scripts/aws-run.sh demo --messages 2000
./scripts/aws-run.sh demo --inject-bug             # exits non-zero: NOT READY TO CUT OVER
```

* `aws-run.sh` runs any `tenantmig` command as a one-off Fargate task, waits for it, and prints its logs.
* It uses your current AWS credentials, and the Region defaults to `us-east-2`.
* The first command after an idle period waits up to about a minute while Aurora resumes from pause.
* The task definition sets `TENANTMIG_SANDBOX=1` and `PLACEMENT_CACHE_TTL_SECONDS=5`.

**Clean up:**

```bash
./scripts/aws-run.sh purge-buckets                 # the buckets the job created: per-tenant, V1 shared, migration
cd infra && npx cdk destroy                        # clusters, keys, secrets, network
```

The job creates the buckets at run time, so `cdk destroy` doesn't remove them. That's why `purge-buckets` runs first.

Results from a test run in `us-east-2`:

| Step | Result |
|---|---|
| Bulk pass, 2,000 rows | 2 to 5 s with 32 concurrent PUTs (higher just after Aurora resumes) |
| Delta pass, 50 rows | under 1 s |
| Permissions | 600 decisions, 0 disagreements. 13 with `--inject-bug` (the run exits non-zero) |
| S3 Batch Operations | 200 of 200 objects copied, 0 failed |
| Reconciliation and cutover | Passed, with no mismatched ID ranges. After a 5-second drain and a 10-row final delta, the tenant was redirected to `cell-1` |

## 7. Production cutover runbook

Run this per tenant. Large tenants can take several Clone cycles before the freeze.

**Before the freeze**
1. Clone: run content, configuration and object passes until deltas are small.
2. Verify: the diff harness has 0 disagreements and reconciliation passes. Fix and repeat until both are clean.
3. Confirm monitoring is in place for the cell: error rates, latency, and [`AuroraGlobalDBReplicationLag`](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.AuroraMonitoring.Metrics.html).
4. Confirm the cell's IAM and KMS access for the tenant bucket is ready to switch on.
5. Agree the freeze window with the tenant. It only needs to cover the final delta and re-verification.

**Cutover**

6. Run `cutover` with `--drain-seconds` set to your router's cache TTL plus a margin, or 0 if you've blocked the tenant's writes in V1. It freezes the tenant, drains, runs the final pass, re-verifies, and redirects. On any failure it rolls back to `v1` and reports why.
7. Switch the cell's IAM and KMS access on. Remove V1's write access to the tenant's data.
8. Smoke-test as the tenant: read recent messages, check permissions on a known admin and a known read-only user.

**After cutover**

9. Watch the cell for a set period. To roll back after Redirect, set the tenant back to `v1`. V1 still holds the data, but writes made in V2 since cutover would need replaying, so decide this window in advance.
10. Forget: after the retention window, run `forget` (dry run), review the output, then `forget --confirm`.

## 8. Scale and cost

**Throughput**
* S3 supports at least [3,500 PUT requests per second per partitioned prefix](https://repost.aws/knowledge-center/s3-object-key-naming-pattern). Hash-prefixed keys (`ab/abcd…`) spread writes across prefixes.
* Run several tenants in parallel. Each tenant has its own bucket, watermark and lock.
* Split very large tenants into several Batch Operations jobs. [Jobs can run concurrently](https://aws.amazon.com/s3/features/batch-operations/).
* Size the cell's writer for the load window, and throttle if the cell is already serving other tenants.

**Cost drivers** (see [S3 pricing](https://aws.amazon.com/s3/pricing/) and [Aurora pricing](https://aws.amazon.com/rds/aurora/pricing/))
* **PUT and COPY requests,** roughly one per unique content object and one per shared object. Deduplication lowers the first.
* **The S3 Intelligent-Tiering reset.** [Copied objects start again in the Frequent Access tier](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intelligent-tiering-overview.html) and only move down after 30 and 90 days without access. For archives that are mostly in colder tiers today, this temporary increase can be the largest one-off cost.
* **Archive restores.** [Restore](https://docs.aws.amazon.com/AmazonS3/latest/userguide/restoring-objects.html) S3 Glacier Deep Archive objects before copying them.
* **S3 Batch Operations** charges per job and per object.
* **Aurora capacity** during the load window, and cross-Region replication for global clusters.

**Sandbox cost.** Idle, the main cost is the NAT gateway (see [VPC pricing](https://aws.amazon.com/vpc/pricing/)); Aurora pauses at 0 ACU. Destroy the stack when you're finished.

## 9. Security

* **Credentials never travel with the data.** Connector credentials are replaced by Secrets Manager references.
  * The fdw path can't read them: the foreign table has no `credentials` column, and the read-only role has no `SELECT` on it.
  * Database credentials, including the read-only role's generated password, are injected into the task from Secrets Manager.
* **Destructive commands only run in a sandbox.** `reset`, `purge-buckets` and `demo` refuse unless `TENANTMIG_SANDBOX=1`.
* **Encryption.** Per-tenant buckets use the cell's KMS key (with S3 Bucket Keys). Aurora storage is encrypted, and connections use TLS (`sslmode=require`), including `postgres_fdw`.
* **No public access.** Databases sit in isolated subnets with no route to the internet. Buckets have Block Public Access enabled. The task has no inbound rules.
* **Least privilege.** The task role can only use buckets with the sample's prefix and the cell key. `iam:PassRole` is limited to the Batch Operations role.
* **Moving a tenant between cells** means changing which cell's IAM role and KMS key can reach the tenant's bucket. No data moves for that step.
* **In production:**
  * Point the content job's reads at a V1 read replica, with a read-only user like `tenantmig_reader`.
  * Rotate the reader's password.
  * Turn deletion protection on.
  * Keep a longer backup retention than the sandbox's one day.

## 10. Adapting it to your platform

| Change | Where |
|---|---|
| The hash and key layout. **Call your application's own hashing code**, or references won't resolve | `tenantmig/hashing.py` |
| Source and target schemas | `sql/v1/*.sql`, `sql/v2/*.sql`, plus the column lists in `content_job.py` and `reconcile.py` |
| The permission model and its old-model evaluator. Populate the application catalogue from your product's real list of resources and actions | `tenantmig/config_job.py`, `tenantmig/perm_diff.py`, `catalogue_*` in `sql/v1/auth.sql` |
| Deltas. Tune `DELTA_SAFETY_SECONDS` to your longest write transaction. If rows can change or be deleted, use change data capture (for example AWS DMS) instead of an `id` watermark | `tenantmig/content_job.py`, `tenantmig/settings.py` |
| The router's cache TTL, used as the cutover drain | `PLACEMENT_CACHE_TTL_SECONDS` |
| The bulk source. Read the snapshot export (Parquet) instead of the live database | `tenantmig/content_job.py` |
| The router and placement store | `tenantmig/cutover.py` (`tenant_placement`) |
| Manifests at scale. Use [S3 Inventory](https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-inventory.html) filtered by tenant prefix instead of listing | `tenantmig/object_job.py` |

## 11. Testing

```bash
python -m pytest tests                                         # unit tests, no database

./scripts/local-up.sh && source .local/env                     # integration tests
TENANTMIG_INTEGRATION=1 python -m pytest tests
```

The 14 integration tests cover:

* **Content job:**
  * a crash mid-pass followed by a clean re-run;
  * re-runs being no-ops;
  * a lower ID that commits late, caught by reconciliation and fixed by `--repair`.
* **Configuration and permissions:**
  * the injected permissions bug being caught;
  * an admin keeping access to a resource and an action no rule names;
  * the harness catching a transform that drops them;
  * late configuration changes and deletions carrying through to V2;
  * connector credentials not being readable through the fdw.
* **Cutover and forget:**
  * a cutover, followed by forget;
  * an automatic rollback when an object is missing;
  * an automatic rollback when a step raises an exception;
  * a second cutover being refused;
  * forget refusing before cutover.
* **Sandbox guard:** `reset` and `purge-buckets` refusing outside a sandbox.

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Fargate task can't reach Secrets Manager or CloudWatch Logs | No internet egress. The sample uses a NAT gateway, which also works under VPC Block Public Access in `block-ingress` mode, where public IPs get no internet access |
| `InvalidManifestContent: Unsupported encryption type SSE_KMS used for manifest` | A [manually created manifest can't be SSE-KMS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-create-job.html). The job writes it with SSE-S3 |
| Batch Operations tasks fail with `Server Side Encryption with AWS KMS managed key requires HTTP header x-amz-server-side-encryption` | A KMS key was set on the Copy operation. Leave it out, and copies inherit the destination bucket's default encryption |
| `iam:PassRole` denied on `s3:CreateJob` | `s3:CreateJob` doesn't set `iam:PassedToService`. Scope `PassRole` by role ARN instead |
| First command times out connecting | Aurora is resuming from auto-pause, which usually takes about 15 seconds. The CLI retries every few seconds for several minutes before giving up |
| `another content pass is already running` | Two passes for the same tenant. Wait for the first one to finish |
| `Refusing to cut over …: it is already v2` | The tenant is live in its cell. Cutover won't route it back to V1 |
| Reconciliation lists `mismatched_id_ranges` with matching object checks | Rows are missing or different in those ranges, for example from a late commit. Run `migrate-content <org> --repair`, then reconcile again |
| `Refusing to reset …: set TENANTMIG_SANDBOX=1` | `reset`, `purge-buckets` and `demo` delete data. Set the flag only in a sandbox account or on the local stack |
| `Batch Operations copy failed …` | Failed tasks are listed in the migration bucket under `_reports/<org>/`. Fix the cause and re-run `migrate-objects` |
| Reconciliation fails only on `all_shared_objects_copied` | Step 8 hasn't run for this tenant yet |

## 13. Further reading

* [Reducing the scope of impact with cell-based architecture](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html), including [cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html)
* [Scale your relational database for SaaS, Part 2: Sharding and routing](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-2-sharding-and-routing/)
* [SaaS storage strategies: data migration](https://docs.aws.amazon.com/whitepapers/latest/multi-tenant-saas-storage-strategies/data-migration.html)
* [Building multi-writer applications on Amazon S3 using native controls](https://aws.amazon.com/blogs/storage/building-multi-writer-applications-on-amazon-s3-using-native-controls/)
* [PostgreSQL logical replication: replicate only the data that you need](https://aws.amazon.com/blogs/database/postgresql-logical-replication-how-to-replicate-only-the-data-that-you-need/)
* [Copy objects between S3 storage classes using S3 Batch Operations](https://aws.amazon.com/blogs/storage/copy-objects-between-any-amazon-s3-storage-classes-using-s3-batch-operations/)
