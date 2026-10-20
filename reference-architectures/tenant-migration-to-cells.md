# Tenant migration into a cell

The below diagram illustrates an architecture for moving one tenant at a time from a pooled platform (V1) into a cell-based platform (V2), where the target schema differs from the source: message content leaves PostgreSQL for Amazon S3 and each row keeps only a content-addressable reference. Every step can be re-run safely, and the migration ends with a verdict on whether the tenant is ready to cut over.

Tenant migration is a core capability of sharded and cellular SaaS architectures. [Scale your relational database for SaaS, Part 2](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-2-sharding-and-routing/) calls it a required part of the architecture, the [SaaS storage strategies whitepaper](https://docs.aws.amazon.com/whitepapers/latest/multi-tenant-saas-storage-strategies/data-migration.html) covers it for the silo and bridge models, and this architecture implements the **Clone → Flip → Redirect → Forget** sequence from [Cell migration](https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/cell-migration.html).

![arch diagram](../samples/tenant-migration-to-cells/docs/tenant-migration-pattern.png)

## Components

1. **V1 platform**: Amazon Aurora PostgreSQL databases (messaging with inline content and platform configuration, plus auth), and a shared S3 bucket with objects under a tenant ID prefix.
2. **Content job**: a container (a Kubernetes Job on Amazon EKS with [EKS Pod Identity](https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html), or an Amazon ECS task on [AWS Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html)) that hashes each message's content, writes it to the tenant's bucket only if the key is absent, and loads the row with `content_ref` into the cell.
3. **Configuration transform**: SQL over [`postgres_fdw`](https://docs.aws.amazon.com/prescriptive-guidance/latest/aurora-postgresql-integration/remote-postgresql-databases.html) from the management database, as a read-only role that can't see credentials. It expands wildcard permissions against the application catalogue, subtracts denies, converges changes and deletions, and replaces connector credentials with an [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) reference.
4. **Permissions diff harness**: evaluates the old and new authorization models independently, on every (user, resource, action) in the application catalogue, and lists every disagreement.
5. **[S3 Batch Operations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-examples-copy.html)**: one Copy job per tenant, from a manifest built from [S3 Inventory](https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-inventory.html) filtered by the tenant prefix.
6. **Reconciliation**: compares row counts and checksums, checks that every `content_ref` resolves to an object, round-trips a sample of content, and confirms every shared object was copied.
7. **V2 cell**: a cell messaging database (one Aurora PostgreSQL global cluster per cell), a management database, and per-tenant S3 buckets encrypted with a cell AWS KMS key.

## Steps

1. **Clone (bulk)**: [export the tenant's tables from a snapshot](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-export-snapshot.html), so V1 production carries no load.
2. **Clone (content)**: compute `content_ref`, write the object if absent — [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) make identical content a single object and re-runs cheap — then `COPY` the row without its content.
3. **Clone (deltas)**: re-run the same job from a per-tenant watermark until cutover. If rows can change or be deleted, capture changes with [AWS DMS](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Source.PostgreSQL.html) instead; where the schema doesn't change, [logical replication with row filters](https://aws.amazon.com/blogs/database/postgresql-logical-replication-how-to-replicate-only-the-data-that-you-need/) is an alternative.
4. **Clone (configuration and permissions)**: transform into the management database.
5. **Verify**: run the diff harness and the reconciliation. Any disagreement or failed check blocks cutover.
6. **Flip and Redirect**: refuse unless the tenant is still on V1, freeze its writes, wait out the router's placement-cache TTL (or block writes on the V1 side), run a final delta pass that also anti-joins for late commits, re-verify, then update the tenant-to-cell mapping. Granting the cell's IAM and KMS access to the tenant's bucket is a separate runbook step, not something the migration code does. If re-verification fails or any step raises, set the tenant back to V1: V1 was authoritative throughout, so nothing has to be undone.
7. **Forget**: remove the tenant from V1 after the retention window.

![Cutover lifecycle](../samples/tenant-migration-to-cells/docs/tenant-migration-cutover.png)

For a runnable implementation, see the [tenant migration to cells sample](../samples/tenant-migration-to-cells/) and its [guide](../samples/tenant-migration-to-cells/docs/GUIDE.md), which covers scale, cost, security and a production cutover runbook.
