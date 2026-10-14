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

![Pattern](tenant-migration-pattern.png)

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

The Well-Architected guidance on [cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html) describes four phases: Clone, Flip, Redirect, Forget. This sample implements each one and adds an explicit verification step.

One wording difference worth knowing if you read both: in the guidance, **Flip** is the moment the new copy becomes authoritative. Here the authority transfer happens at **Redirect** (when `tenant_placement` changes), and the Flip phase is the write freeze plus the final catch-up that makes the copy complete enough to be flipped. The order of operations is the same; only the label moves.

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

== Step 7: existing shared-bucket objects -> org bucket
  "source_objects": 200, "copied": 200

== Step 8: reconciliation
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
* **Late commits aren't lost.** Sequence IDs are handed out before commit, so a row with a lower ID can commit *after* a higher one. A watermark alone would step over it for good. Two things prevent that:
  * Delta passes ignore rows created within `DELTA_SAFETY_SECONDS` (60 by default), **and never advance the watermark past the lowest held-back row.** Holding the rows back is not enough on its own: if the watermark moved to the highest copied ID, a lower ID committing later would still be skipped. `held_back` in the pass result shows how many rows were left for the next pass.
  * `created_at` is set when the row is written, not when it commits, so the margin is a heuristic. Set it above your longest write transaction. Clock skew between application servers, and backdated rows from an import, weaken it further; the next point is the guarantee.
  * The final pass at cutover, with writes frozen, drops the margin, reads the V1 **writer**, and then **anti-joins** V1 against the cell one ID range at a time. That is what actually guarantees completeness, so reconciliation is the gate rather than the margin.

PUTs within a batch run concurrently (32 by default, set by `S3_WORKERS`). With small objects, per-request latency limits throughput, not bandwidth. In a test run in `us-east-2`, a bulk pass of 2,000 rows took about 74 seconds with sequential PUTs and 2 to 5 seconds with concurrent ones.

The bulk pass and every delta pass use the same code. In production, read the bulk pass from an [Aurora snapshot export to S3](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-export-snapshot.html), so the V1 writer carries no load, and point the delta passes at a read replica with `V1_MESSAGING_READ_DSN`. The final pass and `--repair` always read the writer, whatever that is set to.

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

### 5.4 Shared-bucket objects (step 7)

On AWS the job writes a CSV manifest of the tenant's prefix to a separate **migration bucket** and creates an [S3 Batch Operations Copy](https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-examples-copy.html) job into the tenant bucket under `originals/`. Reports of failed tasks go to the same migration bucket.

The job waits up to `BATCH_TIMEOUT_SECONDS` (default 3,600). The step **fails** if the job doesn't complete or any task fails. That makes `migrate-objects` exit non-zero, and makes `cutover` roll back.

Locally, MinIO has no Batch Operations, so the objects are copied directly.

### 5.5 Reconciliation (step 8)

Reconciliation checks five things. It passes only if **all** of them hold:

| Check | Why |
|---|---|
| Row counts match | Nothing lost or duplicated |
| Checksums match for every ID range: an ordered hash over (id, channel, sender, sent_at, content hash) per 50,000 IDs, with the V1 side's content hash computed **in SQL** | Values and content are identical, and the job's hashing matches the database's. Per-range checksums stay well under PostgreSQL's 1 GB text limit at any tenant size, and `mismatched_id_ranges` shows where to look |
| Every distinct `content_ref` resolves to an object | Counts and checksums can pass while objects are missing. In testing, this is the check that caught real failures |
| A sample of objects round-trips (S3 body equals V1 content) | End-to-end content check |
| Every shared-bucket object was copied | Step 7 completed |

### 5.6 Cutover (Flip and Redirect)

`cutover` runs these steps:

1. **Checks** that the tenant is on `v1`, or `frozen` from a crashed run, and takes a per-tenant advisory lock. A tenant already on `v2` is refused: cutting it over again would route a live tenant back to V1 and strand the writes made in V2.
2. Sets the tenant to `frozen`.
3. **Drains:** waits `--drain-seconds`, which defaults to `PLACEMENT_CACHE_TTL_SECONDS` (30, or 2 on the local stack). See below.
4. Runs the final content pass (no safety margin, plus the anti-join) and a configuration pass.
5. Re-copies objects if any are missing.
6. Re-runs the diff harness and reconciliation.
7. On success, sets the tenant to `v2` with its cell. If verification fails, **or any step raises, or the process is interrupted** (`SIGTERM` from a stopped task, `Ctrl-C`, an S3 error, a database timeout, a Batch Operations failure), sets it back to `v1` and records why.

**What is not guaranteed.** A hard kill (`SIGKILL`, the host dying, or the management database becoming unreachable during the rollback itself) can still leave the tenant `frozen`, with its writes paused. Re-running `cutover` from `frozen` is safe and is the fix. In production, **alarm on it**: `where <org>` reports `frozen_for_seconds`, and a tenant frozen for longer than a cutover should take needs a human.

**Why the drain.** If the router caches `tenant_placement`, it can keep accepting V1 writes for a tenant until its cache refreshes. Those writes have to land before the final pass, or they never reach V2. This sample's default is to **wait at least the router's cache TTL** after freezing: set `PLACEMENT_CACHE_TTL_SECONDS`, or `--drain-seconds`, to your router's TTL plus a margin.

**Be clear about what waiting does and does not do.** It bounds when the router *decides* where a write goes. It does not bound when that write *commits*, and it does not stop writers that never consult the router:

| Writer | Does the drain stop it? |
|---|---|
| A request the router already dispatched to V1 | **No.** A slow request, or a client retry, can commit after the wait |
| Connector ingestion, queue consumers, scheduled batch jobs | **No.** They write directly and never read `tenant_placement`. The sample's own `smtp_journal`, `chat_api` and `voice_capture` connectors are exactly this kind of writer |
| A new request arriving after every router cache has refreshed | Yes |

So treat the drain as a convenience for a router-only write path, and **for anything else block writes in V1 itself**, then pass `--drain-seconds 0`. Ways to do that, strongest first:

* A **trigger or row-level security policy on the V1 tables** that rejects writes for a tenant whose status is not `v1`. This is the only option that also stops connectors and batch jobs.
* Pausing the tenant's ingestion (its connectors and queue consumers) at the source.
* Revoking the tenant's write grants, **if** your tenants have their own database roles. In a pooled database they share a role, so there is nothing tenant-specific to revoke.

None of these stop a transaction that has already started. After blocking new writes, wait for the in-flight ones to finish, for example until no session in `pg_stat_activity` has been running a write against those tables since before the freeze.

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
* The first command after an idle period waits while Aurora resumes from pause: usually about 15 seconds, longer after a long idle period. The CLI retries for a few minutes.
* Auto-pause is a sandbox saving, not a production pattern. A cluster that is part of an Aurora global database does not auto-pause, so the per-cell global clusters in the target architecture would not either.
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
5. Agree the freeze window with the tenant, and **size it from the tenant, not from the delta**. The delta is small, but re-verification during the freeze is proportional to the tenant's whole dataset: reconciliation re-hashes every V1 content row, lists the tenant's bucket, and walks every ID range between its lowest and highest ID. The object step is incremental (it copies only what the destination is missing), but the checksum pass is not. Measure it on a copy of the tenant before you agree a window, and for a very large tenant consider verifying the unchanged ID ranges before the freeze and only re-verifying the ranges that changed.

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
* **Storage class on the copy.** A copy lands in the destination bucket's **default** storage class unless the job asks for another one, and this sample doesn't ask, so copies land in S3 Standard. If you do set [S3 Intelligent-Tiering](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intelligent-tiering-overview.html) on the copy, monitoring starts again from the Frequent Access tier and objects only move down after 30 and 90 days without access, so an archive that is mostly in cold tiers today is billed at Frequent Access rates for a quarter. Objects under 128 KB never auto-tier at all. Either way, decide the destination class deliberately: for a large archive this is often the biggest one-off cost of the migration.
* **Archive restores.** [Restore](https://docs.aws.amazon.com/AmazonS3/latest/userguide/restoring-objects.html) S3 Glacier Deep Archive objects before copying them.
* **S3 Batch Operations** charges per job and per object.
* **Aurora capacity** during the load window, and cross-Region replication for global clusters.

**Sandbox cost.** Idle, the main cost is the NAT gateway (see [VPC pricing](https://aws.amazon.com/vpc/pricing/)); Aurora pauses at 0 ACU. Destroy the stack when you're finished.

## 9. Security

* **Credentials never travel with the data.** Connector credentials are replaced by Secrets Manager references.
  * The fdw path can't read them: the foreign table has no `credentials` column, and the read-only role has no `SELECT` on it.
  * Database credentials, including the read-only role's generated password, are injected into the task from Secrets Manager.
* **Destructive commands only run in a sandbox.** `generate` (which truncates V1), `init` (which creates roles and grants on V1), `reset`, `purge-buckets` and `demo` all refuse unless `TENANTMIG_SANDBOX=1`.
* **Migration steps refuse to touch a live tenant.** `migrate-content`, `migrate-config`, `migrate-objects` and `cutover` all check `tenant_placement` first: once a tenant is `v2` they stop, so a stray command can't overwrite live data with stale V1 data (or, after `forget`, empty it).
* **Bucket names are guessable** (`<prefix><tenant-uuid>`) and S3 bucket names are global. Every call sends `ExpectedBucketOwner`, and both IAM roles are conditioned on `aws:ResourceAccount`, so a bucket pre-created under one of those names in another account fails the call instead of receiving a tenant's content. The Batch Operations role's trust policy is conditioned on `aws:SourceAccount` and `aws:SourceArn`.
* **Encryption in transit and at rest.** Per-tenant buckets use the cell's KMS key (with S3 Bucket Keys), and Aurora storage is encrypted.
  * The job's own database connections use `sslmode=require`, which encrypts the channel but does **not** verify the server's certificate, so it does not prevent an impersonated endpoint. Use `verify-full` with the [Amazon RDS CA bundle](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/UsingWithRDS.SSL.html) in production: set `V1_MESSAGING_SSLMODE=verify-full` and friends, and mount the bundle.
  * **The `postgres_fdw` hop cannot be verified on Aurora, only encrypted.** It stays on `sslmode=require`. Verification needs a CA that the *client* trusts, and the fdw client is the managed management-database instance rather than this container: tested on Aurora PostgreSQL 18, `verify-full` fails with `CERTIFICATE_VERIFY_FAILED` both with a file path (a managed host has no readable path to put a bundle) and with `sslrootcert=system` (the Amazon RDS CA is not a public root). Mitigate it the other way: the hop never leaves isolated subnets, the security group admits only the two V1 clusters, and the role it carries is read-only and narrowly granted. If you need the hop verified, do the join in your own job (which can mount a CA) instead of in the database.
* **No public access.** Databases sit in isolated subnets with no route to the internet. Buckets have Block Public Access enabled. The task has no inbound rules.
* **Least privilege.** The task role can only use buckets with the sample's prefix, in this account, plus the cell key. `iam:PassRole` is limited to the single Batch Operations role ARN.
* **Moving a tenant between cells** is a smaller job than moving it off V1, but it is not free. The tenant's rows live in the old cell's messaging database and have to be copied to the new one, and its bucket is encrypted with the old cell's KMS key, so either the new cell is granted use of that key or the objects are re-encrypted by copying them. Only the routing change itself (`tenant_placement`) is instant.
* **In production:**
  * Point the bulk and delta passes at a V1 read replica with `V1_MESSAGING_READ_DSN`. The final pass, `--repair` and reconciliation deliberately ignore it and read the writer (`V1_MESSAGING_DSN`): replica lag at cutover would make a missing row look like a matching count.
  * Give that replica user `SELECT` on `messages` and `organisations`. `tenantmig_reader` is **not** that user: it exists for `postgres_fdw` and is deliberately limited to the auth and configuration tables.
  * Rotate the reader's password. `migrate-config` reapplies the user mapping on every run, so a rotation takes effect on the next pass with no manual drop.
  * Know that the reader's password is stored **in clear text** in the management database's `pg_user_mapping`, readable by that database's superuser. That is how `postgres_fdw` works. Keep the role read-only and narrowly granted, as it is here, and treat the management database's superuser as equivalent to it.
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
| **An ID strategy for the cell.** This sample carries V1's `messages.id`, `principals.id` and `connectors.id` across unchanged, which is the simplest thing that can be verified row by row. It only works while V1 is the only source of those IDs. Before V2 starts issuing its own, give each cell a non-overlapping range, or switch the key to a UUID or a (tenant, id) pair. Otherwise a row written in V2 after cutover can collide with a V1 row from a tenant you migrate later | `sql/v2/*.sql`, `content_job.py`, `config_job.py` |
| **Connector secrets.** `migrate-config` writes a `secret_ref` but does not create the secret. Set `CHECK_CONNECTOR_SECRETS=1` so cutover refuses while any `secret_ref` doesn't resolve: after `forget` the V1 `credentials` column is gone for good | `cutover.py`, `config_job.py` |
| The bulk source. Read the snapshot export (Parquet) instead of the live database | `tenantmig/content_job.py` |
| The router and placement store | `tenantmig/cutover.py` (`tenant_placement`) |
| Manifests at scale. Use [S3 Inventory](https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-inventory.html) filtered by tenant prefix instead of listing | `tenantmig/object_job.py` |

## 11. Testing

```bash
python -m pytest tests                                         # unit tests, no database

./scripts/local-up.sh && source .local/env                     # integration tests
TENANTMIG_INTEGRATION=1 python -m pytest tests
```

The 24 integration tests cover:

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
* **Sandbox guard:** `generate`, `init`, `reset` and `purge-buckets` all refusing outside a sandbox.
* **Guards against clobbering a live tenant:** `migrate-content`, `migrate-config` and `migrate-objects` refusing once a tenant is `v2`.
* **The safety margin:** a held-back row stopping the watermark, so a lower ID committing late is still copied.
* **Repair:** deleted and edited V1 rows converging, not just missing ones.
* **S3 semantics:** a 409 being retried, and a 412 treated as "already there".
* **The catalogue:** a V1 rule naming something outside the catalogue being reported.
* **Concurrency:** four tenants running `migrate-config` at once on an empty management database.
* **Credentials:** a rotated reader password taking effect, and an upgrade dropping a foreign table that exposed `credentials`.
* **Incremental objects:** the second object pass copying nothing.

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Fargate task can't reach Secrets Manager or CloudWatch Logs | No internet egress. The sample uses a NAT gateway, which also works under VPC Block Public Access in `block-ingress` mode, where public IPs get no internet access |
| `InvalidManifestContent: Unsupported encryption type SSE_KMS used for manifest` | A [manually created manifest can't be SSE-KMS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-create-job.html). The job writes it with SSE-S3 |
| Batch Operations tasks fail with `Server Side Encryption with AWS KMS managed key requires HTTP header x-amz-server-side-encryption` | A KMS key was set on the Copy operation. Leave it out, and copies inherit the destination bucket's default encryption |
| `iam:PassRole` denied on `s3:CreateJob` | Observed in testing: a `PassRole` statement conditioned on `iam:PassedToService = batchoperations.s3.amazonaws.com` denied the call, so the condition appears not to be set for `s3:CreateJob`. The AWS documentation doesn't state this either way. Scope `PassRole` to the single role ARN instead, as this sample does |
| First command times out connecting | Aurora is resuming from auto-pause, which usually takes about 15 seconds. The CLI retries every few seconds for several minutes before giving up |
| `another content pass is already running` | Two passes for the same tenant. Wait for the first one to finish |
| `Refusing to cut over …: it is already v2` | The tenant is live in its cell. Cutover won't route it back to V1 |
| Reconciliation lists `mismatched_id_ranges` | Rows in those ranges are missing, changed or extra. `migrate-content <org> --repair` converges all three: it backfills missing rows, re-copies rows whose V1 values differ, and deletes cell rows whose V1 row is gone. Then reconcile again. If V1 rows change routinely, use change data capture instead of a watermark |
| `Refusing to reset …: set TENANTMIG_SANDBOX=1` | `reset`, `purge-buckets` and `demo` delete data. Set the flag only in a sandbox account or on the local stack |
| `Batch Operations copy failed …` | Failed tasks are listed in the migration bucket under `_reports/<org>/`. Fix the cause and re-run `migrate-objects` |
| Reconciliation fails only on `all_shared_objects_copied` | Step 7 hasn't run for this tenant yet |

## 13. Further reading

* [Reducing the scope of impact with cell-based architecture](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html), including [cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html)
* [Scale your relational database for SaaS, Part 2: Sharding and routing](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-2-sharding-and-routing/)
* [SaaS storage strategies: data migration](https://docs.aws.amazon.com/whitepapers/latest/multi-tenant-saas-storage-strategies/data-migration.html)
* [Building multi-writer applications on Amazon S3 using native controls](https://aws.amazon.com/blogs/storage/building-multi-writer-applications-on-amazon-s3-using-native-controls/)
* [PostgreSQL logical replication: replicate only the data that you need](https://aws.amazon.com/blogs/database/postgresql-logical-replication-how-to-replicate-only-the-data-that-you-need/)
* [Copy objects between S3 storage classes using S3 Batch Operations](https://aws.amazon.com/blogs/storage/copy-objects-between-any-amazon-s3-storage-classes-using-s3-batch-operations/)
