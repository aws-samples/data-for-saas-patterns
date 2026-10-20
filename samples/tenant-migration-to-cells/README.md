# Tenant migration into a cell

Moves one tenant at a time from a pooled platform (V1) into a cell-based platform (V2) on Amazon Aurora PostgreSQL and Amazon S3, and proves the move is correct before cutover.

The target schema is deliberately not like-for-like: message content moves out of PostgreSQL into a per-tenant S3 bucket, and each row keeps a content-addressable reference (`content_ref`). Permissions move to a new model in a separate management database.

It implements **Clone → Flip → Redirect → Forget** from [Cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html) in the AWS Well-Architected guidance. See the [reference architecture](../../reference-architectures/tenant-migration-to-cells.md) for the pattern, and [Scale your relational database for SaaS, Part 2](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-2-sharding-and-routing/) for why tenant migration belongs in a sharded or cellular architecture.

![Architecture](docs/tenant-migration-pattern.png)

**The [guide](docs/GUIDE.md)** covers how each step works, a production cutover runbook, scale, cost, security and troubleshooting. All data here is synthetic.

## What it does

| Step | Source (V1) | Route | Target (V2) |
|---|---|---|---|
| 1–3 | Messaging rows with inline `content` | Content job: hash the content, write it to S3 only if the key is absent, `COPY` the row with `content_ref` | Cell messaging database + per-tenant bucket |
| 4 | New V1 rows after the bulk pass | The same job, re-run from a per-tenant watermark. The final pass at cutover adds an anti-join, so late commits aren't lost | Same |
| 5 | Auth (users, roles, permissions) and platform config | SQL over [`postgres_fdw`](https://docs.aws.amazon.com/prescriptive-guidance/latest/aurora-postgresql-integration/remote-postgresql-databases.html) as a read-only role: wildcards expanded against the application catalogue, denies subtracted, changes and deletions converged, credentials replaced by an [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) reference | Management database |
| 6 | — | Permissions diff harness: V1 and V2 answer the same questions independently, and every disagreement is listed | — |
| 7 | Shared bucket, objects under the tenant prefix | [S3 Batch Operations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-examples-copy.html) Copy, one job per tenant (direct copy locally) | Per-tenant bucket, `originals/` |
| 8 | — | Reconciliation: row counts, per-range checksums, every `content_ref` resolves, sample round-trip, every shared object copied | — |
| Flip + Redirect | — | `cutover`: freezes the tenant's writes, drains router caches, runs a final delta, re-verifies, then routes it to the cell. Rolls back to V1 on any failure or exception | `tenant_placement` |
| Forget | The tenant's V1 rows and objects | `forget`: dry run, then `--confirm`. Refuses unless the tenant is cut over | — |

Worth knowing about the design:

* **[S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) (`If-None-Match`) do the deduplication.** Identical content is stored once, and a re-run never stores a second copy — though a rejected PUT is still a billed request, so re-runs are cheap, not free.
* **Objects are written before rows, and rows commit with the watermark.** A row never points at missing content, and a crashed pass resumes from its last committed batch. An advisory lock allows one pass per tenant.
* **The permissions harness doesn't reuse the transform.** It evaluates the V1 model in its own code over the same catalogue, so a bug in the transform can't hide itself. `--inject-bug` shows it catching one.
* **Credentials can't travel the migration path.** The fdw sees only declared columns, without `credentials`, as a role that couldn't select them anyway.
* **Reconciliation checks more than counts.** Counts and checksums can pass while objects are missing; checking that every reference resolves catches that.

## Run it

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/); locally also PostgreSQL 15 and 18 and MinIO (`brew install postgresql@15 postgresql@18 minio`) or Docker; on AWS, Node.js 22+ and credentials for a sandbox account.

```bash
uv venv && uv pip install -e .
./scripts/local-up.sh && source .local/env      # sets TENANTMIG_SANDBOX=1, creates the reader role
# or: docker compose up -d && source scripts/docker.env && tenantmig init

tenantmig demo                 # end to end for one tenant (resets the V2 targets)
tenantmig demo --inject-bug    # seeds a permissions bug the harness must catch
```

Individual steps: `orgs`, `migrate-content <org> [--repair]`, `migrate-config <org>`, `migrate-objects <org>`, `diff-permissions <org>`, `reconcile <org>`, `cutover <org> [--drain-seconds N]`, `where <org>`, `forget <org> [--confirm]`, `reset`, `purge-buckets`. The [guide](docs/GUIDE.md#run-it) walks through them.

Tests: `python -m pytest tests` for the 7 unit tests, which need no database; add `TENANTMIG_INTEGRATION=1` with the local stack up for the 24 integration tests.

## Deploy to AWS

![Sandbox deployment](docs/tenant-migration-sandbox.png)

```bash
cd infra && npm install && npx cdk bootstrap
npx cdk deploy                                   # optional: -c notifyEmail=you@example.com
cd .. && ./scripts/aws-run.sh init && ./scripts/aws-run.sh demo
```

A VPC with one NAT gateway and isolated database subnets; two Aurora PostgreSQL 15 source clusters and two PostgreSQL 18 target clusters on Serverless v2, [scaling to 0 ACU when idle](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html); a cell KMS key; an S3 Batch Operations role; an on-demand [Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html) task that runs `tenantmig`; and optionally a monthly AWS Budget with email alerts.

`scripts/aws-run.sh` uses your current credentials, defaulting to `us-east-2`. Engine versions are in `infra/cdk.json` — check they're available in your Region.

## Clean up

```bash
./scripts/local-down.sh --wipe        # local

./scripts/aws-run.sh purge-buckets    # AWS: every bucket is created at run time, so purge first
cd infra && npx cdk destroy           # AWS: clusters, keys, secrets, network
```

## Security

This is sample code for a sandbox account with synthetic data: deletion protection is off and backup retention is one day. `reset`, `purge-buckets`, `init`, `generate` and `demo` delete or overwrite data, so they refuse unless `TENANTMIG_SANDBOX=1`. Read the guide's [security section](docs/GUIDE.md#security) and harden it before using it with real data.
