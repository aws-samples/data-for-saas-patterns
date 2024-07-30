# AWS Data for SaaS

This repository contains a collection of samples, best practices and reference architectures for implementing SaaS applications on AWS for databases and data services.

## Contents

* [Reference Architectures](./reference-architectures/)
* [RDS Data API Row-level Security](README.md#rds-data-api-row-level-security)
* [Multi-tenant vector databases](README.md#multi-tenant-vector-databases)
* [Data for SaaS blogs](README.md#data-for-saas-blogs)
* [Videos](README.md#videos)

## RDS Data API Row-level Security

Row-level security is commonly employed in multi-tenant databases to provide isolation between tenant's data. Row level security policies are created in the database to enforce this isolation on tenant-owned tables.

This sample contains 2 examples for implementing row-level security using the [RDS data API for Amazon Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html). The examples provided are in Python, but they can be easily transferred to other languages using the same patterns. Additionally, these examples will work for Amazon Aurora PostgreSQL provisioned or serverless V2.

[Example 1 - RLS with PostgreSQL function](rds-data-api-rls/rds-data-api-rls-function.ipynb)

[Example 2 - RLS with database transactions](rds-data-api-rls/rds-data-api-rls-transaction.ipynb)

## Multi-tenant vector databases

### Amazon Aurora

Vector databases are commonly employed to store embeddings generated as part of generative-AI applications. A popular option is to use the pgvector extension for PostgreSQL. Amazon Aurora PostgreSQL supports the pgvector extension to store embeddings from machine learning (ML) models in your database and to perform efficient similarity searches.

You can use an existing Aurora PostgreSQL cluster or use the CDK code to provision an Aurora PostgreSQL cluster that is a prerequisite for running the sample.

[Deploy Aurora PostgreSQL using CDK](multi-tenant-vector-database/amazon-aurora/cdk/README.md)

This sample shows how to use pgvector in a multi-tenant database, enforcing tenant isolation. One example is using a self-managed Retrieval-augmented generation (RAG) implementation, the other is using [Amazon Bedrock knowledge bases](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html).

[Example 1 - Self-managed](multi-tenant-vector-database/amazon-aurora/self-managed/)

[Example 2 - AWS-managed](multi-tenant-vector-database/amazon-aurora/aws-managed/)

## Data for SaaS Blogs :books:

Below is a collection of published blog posts covering different aspects of building data architectures for SaaS applications on AWS:

### [Scale your relational database for SaaS](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-1-common-scaling-patterns/)

This blog post provides guidance for software as a service (SaaS) providers who are using relational databases, such as Amazon RDS and Amazon Aurora, and are looking to scale their databases effectively as their business grows. The post explores common scaling patterns for relational databases in a SaaS context, including scaling vertical and horizontal resources, optimizing operations through techniques like micro-batching and table partitioning, and bringing in purpose-built databases for specific use cases. The post discusses the importance of understanding the trade-offs and aligning the scaling approach with the SaaS partitioning model (silo, bridge, or pool) and usage patterns. The post aims to help SaaS providers make informed decisions about scaling their relational databases while considering factors like performance, operational complexity, and tenant isolation.

### [Managed database backup and recovery in a multi-tenant SaaS application](https://aws.amazon.com/blogs/database/managed-database-backup-and-recovery-in-a-multi-tenant-saas-application/)

This blog post discusses approaches for managing database backup and recovery in a multi-tenant SaaS application deployed on AWS. It explores how different database partitioning models (silo, bridge, pool) influence backup and recovery complexity. The post compares segregating tenant data during backup vs during recovery, providing examples using PostgreSQL on Amazon RDS and Aurora. It covers complete vs selective restore scenarios, minimizing costs during recovery, and maintaining a recovery inventory for large datasets. The key takeaway is that there is no one-size-fits-all approach, and the choice depends on factors like the partitioning model, data volumes, and requirements around backup frequency, costs, and recovery flexibility. Proper testing of the backup/recovery solution is emphasized as critical.

### [Choose the right PostgreSQL data access pattern for your SaaS application](https://aws.amazon.com/blogs/database/choose-the-right-postgresql-data-access-pattern-for-your-saas-application/)

The post explores different data access patterns for multi-tenant SaaS applications using Amazon RDS for PostgreSQL or Amazon Aurora PostgreSQL-Compatible Edition. It covers the silo, bridge, and pool database isolation models combined with different compute isolation approaches (siloed or pooled). The access patterns vary based on factors like using AWS IAM authentication vs AWS Secrets Manager, the ability to scope permissions using IAM session policies or ABAC, and enforcement mechanisms like PostgreSQL's row-level security. The tradeoffs between isolation strength, cost efficiency, operational complexity, and noisy neighbor impact are evaluated for each pattern. Code examples are provided for implementing the different access patterns.

### [Modernize legacy databases using event sourcing and CQRS with AWS DMS](https://aws.amazon.com/blogs/database/modernize-legacy-databases-using-event-sourcing-and-cqrs-with-aws-dms/)

This blog post discusses two approaches to implement event sourcing and Command Query Responsibility Segregation (CQRS) architecture using AWS Database Migration Service (AWS DMS). The first approach uses only AWS DMS to replicate data from a monolithic source database to an Amazon S3 event store, and then from the event store to downstream databases like DynamoDB. The second approach combines AWS DMS with Amazon Managed Streaming for Apache Kafka (Amazon MSK) to replicate data from the source database to a Kafka topic, which is then consumed by downstream systems like DynamoDB and an S3 event store. The post explains the benefits of each approach, provides instructions to deploy sample solutions using AWS Serverless Application Model (AWS SAM) templates, and discusses how these architectures future-proof applications by enabling data portability and the ability to replay events into any data store in the future.

### [Send webhooks to SaaS applications from Amazon Aurora via Amazon EventBridge](https://aws.amazon.com/blogs/database/send-webhooks-to-saas-applications-from-amazon-aurora-via-amazon-eventbridge/)

This blog post explains how to use Amazon Aurora PostgreSQL and Amazon EventBridge to send outgoing webhooks (HTTP callbacks) to external SaaS applications like Salesforce, Marketo, or ServiceNow. The solution involves configuring an Aurora PostgreSQL cluster to invoke an AWS Lambda function when certain events occur (e.g. creating a new database record). This serverless architecture reduces the need for custom webhook processing code and infrastructure management overhead. The post provides a step-by-step walkthrough using an AWS CDK sample project to deploy and test the solution.

### [Enforce row-level security with the RDS Data API](https://aws.amazon.com/blogs/database/enforce-row-level-security-with-the-rds-data-api/)

This blog post discusses how to enforce row-level security in Amazon Aurora PostgreSQL-Compatible Edition using the RDS Data API and PostgreSQL features. It provides an overview of row-level security policies in PostgreSQL and demonstrates how to create a shared tenant schema, define a row-level security policy, and test the tenant isolation using both traditional connection management and the RDS Data API. The post highlights the benefits of using the RDS Data API for securely querying filtered data without managing database connections or connection pools. It also covers cost considerations, metering strategies, and cleanup steps. Overall, the post aims to help readers build secure and scalable multi-tenant PostgreSQL architectures on AWS.

### Videos :movie_camera:

[ Building SaaS on AWS - Building a modern data architecture for SaaS ](https://www.youtube.com/watch?v=KGR4SQMNsXo)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

