# Tenant migration into a cell

This sample moves one tenant (organisation) at a time from a pooled platform (V1) into a cell-based platform (V2) on Amazon Aurora PostgreSQL and Amazon S3, and proves the move is correct before cutover.

The target schema is not like-for-like: message content moves out of PostgreSQL into a per-tenant S3 bucket, and each row keeps a content-addressable reference (`content_ref`). Permissions move to a new model in a separate management database.

It implements the **Clone → Flip → Redirect → Forget** sequence from [Cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html) in the AWS Well-Architected guidance on cell-based architecture. See the [reference architecture](../../reference-architectures/tenant-migration-to-cells.md) for the pattern, and [Scale your relational database for SaaS, Part 2](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-2-sharding-and-routing/) for why tenant migration belongs in a sharded or cellular SaaS architecture.

![Architecture](docs/tenant-migration-pattern.png)

**New here? Read the [complete guide](docs/GUIDE.md).** It covers concepts, a walkthrough, how each step works, a production cutover runbook, scale and cost, security, and troubleshooting.

All data is synthetic.

## What the sample does

| Step | Source (V1) | Route | Target (V2) |
|---|---|---|---|
| 1–3 | Messaging rows with inline `content` | Content job: hash the content, write it to S3 only if the key is absent, `COPY` the row with `content_ref` | Cell messaging database + per-tenant bucket |
| 4 | New V1 rows after the bulk pass | The same job, re-run from a per-tenant watermark (rows older than a safety margin). The final pass at cutover adds an anti-join, so late commits aren't lost | Same |
| 5 | Auth (users, roles, permissions) + platform configuration | SQL over [`postgres_fdw`](https://docs.aws.amazon.com/prescriptive-guidance/latest/aurora-postgresql-integration/remote-postgresql-databases.html), connecting as a read-only role: wildcards expanded against the application catalogue, denies subtracted, changes and deletions converged, credentials replaced by an [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) reference | Management database |
| 6 | — | Permissions diff harness: V1 and V2 answer the same questions independently, over the application catalogue, and every disagreement is listed | — |
| 7 | Shared bucket, objects under the tenant prefix | [S3 Batch Operations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-examples-copy.html) Copy, one job per tenant (a direct copy when run locally) | Per-tenant bucket, `originals/` |
| 8 | — | Reconciliation: row counts, checksums, every `content_ref` resolves, sample round-trip, every shared object copied | — |
| Flip + Redirect | — | `cutover`: refuses unless the tenant is on V1, freezes its writes, drains router caches, runs a final delta, re-verifies, then routes it to the cell. Rolls back to V1 on any failure or exception | `tenant_placement` |
| Forget | Tenant's V1 rows and objects | `forget`: dry run, then `--confirm`. Refuses unless the tenant is cut over | — |

The demo verifies the tenant (`READY TO CUT OVER`, or `NOT READY TO CUT OVER` with the reasons), then cuts it over and shows where the router now sends it.

Points to note in the design:

* **[S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) (`If-None-Match`) do the deduplication.** Identical content is stored once, and re-running a pass never stores a second copy. (A rejected conditional PUT is still a billed request, so a re-run is cheap, not free.)
* **Objects are written before rows, and rows commit together with the watermark.** A row never points at missing content, and a pass that crashes resumes from its last committed batch. An advisory lock allows one pass per tenant at a time.
* **The permissions harness doesn't reuse the transform.** It evaluates the V1 model in its own code, over the same application catalogue, so a bug in the transform can't hide itself. Run the demo with `--inject-bug` to see it catch one.
* **Credentials can't travel the migration path.** The fdw sees only declared columns, without `credentials`, and connects as a read-only role that can't select them.
* **Reconciliation checks more than counts.** Counts and checksums can pass while objects are missing. Checking that every reference resolves catches that.

## Prerequisites

* Python 3.11+ and [uv](https://docs.astral.sh/uv/)
* To run locally: PostgreSQL 15 and 18, and MinIO (for example `brew install postgresql@15 postgresql@18 minio`), or Docker
* To run on AWS: Node.js 22+, the AWS CDK v2, and credentials for a sandbox account

## Run locally

```bash
uv venv && uv pip install -e .
./scripts/local-up.sh && source .local/env      # sets TENANTMIG_SANDBOX=1 and creates the reader role
# or with Docker:
#   docker compose up -d && source scripts/docker.env && tenantmig init

tenantmig demo                 # end to end for one tenant (resets the V2 targets: needs TENANTMIG_SANDBOX=1)
tenantmig demo --inject-bug    # seeds a permissions bug that the harness must catch
```

Tests: `uv pip install pytest && python -m pytest tests` runs the 7 unit tests, which need no database. Add `TENANTMIG_INTEGRATION=1` with the local stack running for the 24 integration tests. They cover crash and re-run, late commits held back by the safety margin, repair of deleted and edited rows, the application catalogue, config convergence, credential isolation and rotation, concurrency, the sandbox guards, cutover, rollback on failure and on interruption, refusals, and forget.

Individual steps: `tenantmig orgs`, `migrate-content <org>`, `migrate-config <org>`, `migrate-objects <org>`, `diff-permissions <org>`, `reconcile <org>`, `migrate-content <org> --repair`, `cutover <org> [--drain-seconds N]`, `where <org>`, `forget <org> [--confirm]`, `reset`, `purge-buckets`.

The [guide](docs/GUIDE.md#4-walkthrough-run-it-locally) walks through each command.

## Deploy to AWS

![Sandbox deployment](docs/tenant-migration-sandbox.png)

The CDK app creates a sandbox:

* a VPC with one NAT gateway and isolated database subnets;
* two Aurora PostgreSQL 15 source clusters and two Aurora PostgreSQL 18 target clusters (Serverless v2, [scaling to 0 ACU and pausing when idle](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html));
* a cell KMS key and an S3 Batch Operations role;
* an on-demand [AWS Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html) task that runs `tenantmig`;
* optionally, a monthly AWS Budget with email alerts.

```bash
cd infra && npm install
npx cdk bootstrap
npx cdk deploy                                   # optional: -c notifyEmail=you@example.com
cd .. && ./scripts/aws-run.sh init && ./scripts/aws-run.sh demo
```

`scripts/aws-run.sh` uses your current AWS credentials, and the Region defaults to `us-east-2`. The engine versions are set in `infra/cdk.json`. Check that they're available in your Region.

### Results from a test run

These come from a run in `us-east-2` with 2,000 synthetic messages for one tenant.

* **Bulk pass:** 2,000 rows in 2 to 5 seconds with 32 concurrent PUTs, against about 74 seconds with sequential PUTs. 706 duplicate objects were skipped.
* **Delta pass:** 50 new rows in under 1 second.
* **Permissions:** 600 decisions checked (25 users × 6 resources × 4 actions) with 0 disagreements. With `--inject-bug`, the harness reported 13 decisions where a user would gain write access that V1 denies.
* **S3 Batch Operations:** 200 of 200 objects copied.
* **Reconciliation:** passed.
* **Cutover:** after a 5-second drain and a 10-row final delta, the tenant was redirected to `cell-1`.

## Adapting it

These are placeholders. Each is a single file to change.

| Placeholder | Where |
|---|---|
| `content_ref` = SHA-256 of UTF-8 bytes, key `<ref[0:2]>/<ref>`. Use your application's own hashing code | `tenantmig/hashing.py` |
| Source and target schemas | `sql/v1/*.sql`, `sql/v2/*.sql` |
| Permission model (roles, wildcards, deny wins → flat allow grants) and the application catalogue | `tenantmig/config_job.py`, `tenantmig/perm_diff.py`, `sql/v1/auth.sql` |
| Messages are immutable. `id` is the watermark, with `DELTA_SAFETY_SECONDS` covering late commits. Router cache TTL (`PLACEMENT_CACHE_TTL_SECONDS`) is the cutover drain. If rows can change or be deleted, use change data capture (for example [AWS DMS with a PostgreSQL source](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Source.PostgreSQL.html)) for deltas | `tenantmig/content_job.py` |
| Bulk source. In production, read the [snapshot export to S3](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-export-snapshot.html) (Parquet) rather than the live database | `tenantmig/content_job.py` |

At production scale:

* Build manifests from [S3 Inventory](https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-inventory.html) rather than by listing.
* A copy lands in the destination bucket's default storage class unless the job sets one. If you set [S3 Intelligent-Tiering](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intelligent-tiering-overview.html), monitoring restarts at Frequent Access and objects under 128 KB never auto-tier.
* [Restore](https://docs.aws.amazon.com/AmazonS3/latest/userguide/restoring-objects.html) archived objects first: S3 Glacier Flexible Retrieval and Deep Archive, and the Intelligent-Tiering archive tiers.
* S3 Batch Operations copies objects up to 5 GB ([larger objects](https://repost.aws/knowledge-center/s3-batch-operations-copy)).
* Throttle loads into cells that are already serving traffic.

## Clean up

```bash
./scripts/local-down.sh --wipe        # local

./scripts/aws-run.sh purge-buckets    # AWS: the per-tenant, V1 shared and migration buckets the job created
cd infra && npx cdk destroy           # AWS: clusters, keys, secrets and network
```

The job creates every bucket at run time, so `cdk destroy` doesn't remove any of them. Run `purge-buckets` first. Like `reset` and `demo`, it only runs where `TENANTMIG_SANDBOX=1`.

## Security

This is sample code for a sandbox account with synthetic data:

* The databases have deletion protection off and a one-day backup retention.
* Clusters, keys and secrets are removed by `cdk destroy`. Buckets are removed by `purge-buckets`.
* `reset`, `purge-buckets` and `demo` delete data, so they refuse unless `TENANTMIG_SANDBOX=1`.

Review and harden it before using it with real data.
