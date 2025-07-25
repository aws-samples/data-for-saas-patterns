# AWS Data for SaaS

This repository contains a collection of samples, best practices and reference architectures for implementing SaaS applications on AWS for databases and data services.

## Contents

* [Reference Architectures](./reference-architectures/)
* [Samples](./samples/)
* * [RDS Data API Row-level Security](README.md#rds-data-api-row-level-security)
* * [Multi-tenant vector databases](README.md#multi-tenant-vector-databases)
* * [Scheduled Autoscaling Aurora Serverless V2](README.md#scheduled-autoscaling-aurora-serverless-v2)
* * [Aurora Global Database Serverless V2](README.md#aurora-global-database-serverless-v2)
* * [Tenant isolation patterns](README.md#tenant-isolation-patterns)
* [Data for SaaS blogs](README.md#data-for-saas-blogs-books)
* [Videos](README.md#videos-movie_camera)

## RDS Data API Row-level Security

Row-level security is commonly employed in multi-tenant databases to provide isolation between tenant's data. Row level security policies are created in the database to enforce this isolation on tenant-owned tables.

This sample contains 2 examples for implementing row-level security using the [RDS data API for Amazon Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html). The examples provided are in Python, but they can be easily transferred to other languages using the same patterns. Additionally, these examples will work for Amazon Aurora PostgreSQL provisioned or serverless V2.

[Example 1 - RLS with PostgreSQL function](./samples/rds-data-api-rls/rds-data-api-rls-function.ipynb)

[Example 2 - RLS with database transactions](./samples/rds-data-api-rls/rds-data-api-rls-transaction.ipynb)

## Multi-tenant vector databases

### Amazon Aurora

Vector databases are commonly employed to store embeddings generated as part of generative-AI applications. A popular option is to use the pgvector extension for PostgreSQL. Amazon Aurora PostgreSQL supports the pgvector extension to store embeddings from machine learning (ML) models in your database and to perform efficient similarity searches.

You can use an existing Aurora PostgreSQL cluster or use the CDK code to provision an Aurora PostgreSQL cluster that is a prerequisite for running the sample.

[Deploy Aurora PostgreSQL using CDK](./samples/multi-tenant-vector-database/amazon-aurora/cdk/README.md)

This sample shows how to use pgvector in a multi-tenant database, enforcing tenant isolation. One example is using a self-managed Retrieval-augmented generation (RAG) implementation, the other is using [Amazon Bedrock knowledge bases](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html).

[Example 1 - Self-managed](./samples/multi-tenant-vector-database/amazon-aurora/self-managed/)

[Example 2 - AWS-managed](./samples/multi-tenant-vector-database/amazon-aurora/aws-managed/)

### Amazon OpenSearch

Amazon OpenSearch Service offers powerful vector search capabilities in both provisioned and serverless deployment options. These vector stores enable efficient similarity searches on high-dimensional data, making them ideal for applications like semantic search, recommendation systems, and image recognition. With provisioned OpenSearch, users have granular control over cluster configuration and scaling, while the serverless option provides on-demand, automatically scaled resources without the need to manage infrastructure.

You can use the CloudFormation template to provision the Amazon OpenSearch provisioned domain and the serverless collection, that are prerequisites to run this sample.

[Deploy Amazon OpenSearch using CloudFormation](./samples/multi-tenant-vector-database/amazon-opensearch/README.md)

This sample describes both the self-managed Retrieval-augmented generation (RAG) implementation and also the fully-managed RAG approach using [Amazon Bedrock knowledge bases](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html).

[Example 1 - Self-managed](./samples/multi-tenant-vector-database/amazon-opensearch/self-managed/opensearch_self_managed_notebook.ipynb)

[Example 2 - Fully-managed](./samples/multi-tenant-vector-database/amazon-opensearch/fully-managed/opensearch_fully_managed_notebook.ipynb)

## Scheduled Autoscaling Aurora Serverless V2

This sample provides a CDK application that creates a scheduled job to scale up and down an Aurora Serverless V2 instance minimum capacity. This is useful for SaaS applications with predictable usage patterns to reduce scaling times during busy periods.

[Scheduled Autoscaling Aurora Serverless V2](./samples/scheduled-aurora-serverless-scaling/)

## Aurora Global Database Serverless V2

This sample provides a CDK application that creates Amazon Aurora Global database custer across a primary and secondary region for SaaS applications that need global footprint and for disaster recovery strategies. The stack also includes a Fargate container application to test the primary and secondary regions with CRUD API operations. 

[Aurora Global Database Serverless V2](./samples/aurora-serverless-global-db-cdk/)

## Tenant isolation patterns

This sample provides tenant isolation patterns using [Attribute Based Access Control (ABAC)](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction_attribute-based-access-control.html), implemented with [AssumeRoleWithWebIdentity](https://aws.amazon.com/blogs/security/saas-tenant-isolation-with-abac-using-aws-sts-support-for-tags-in-jwt/) for a robust tenant isolation mechanism.

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

### [Partitioning and Isolating Multi-Tenant SaaS Data with Amazon S3](https://aws.amazon.com/blogs/apn/partitioning-and-isolating-multi-tenant-saas-data-with-amazon-s3/)

This blog post discusses various strategies for partitioning and isolating multi-tenant data using Amazon S3 in SaaS applications, presenting three main approaches: bucket-per-tenant model, object key prefix-per-tenant model, and database-mapped tenant objects. The bucket-per-tenant model assigns separate buckets for each tenant but has limitations due to AWS bucket quotas, while the prefix-per-tenant model allows better scalability by using key name prefixes to associate objects with tenants within shared buckets. For enhanced security, the article explains how tenant isolation can be achieved through IAM policies, access points, and encryption using AWS KMS, with options for server-side encryption and envelope encryption. The blog also covers tenant activity tracking and cost management through features like S3 Inventory, server access logging, and cost allocation tags. Finally, this covers lifecycle management options for different tenant tiers and recommends additional security and cost management configurations such as S3 Intelligent-Tiering, disabling ACLs, and implementing S3 Block Public Access.

Note: The blog mentions hard quota of 1,000 buckets per AWS account. However, this has been increased to 1 million starting [Nov 2024](https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-s3-up-1-million-buckets-per-aws-account/) 

### [Multi-tenant RAG with Amazon Bedrock Knowledge Bases](https://aws.amazon.com/blogs/machine-learning/multi-tenant-rag-with-amazon-bedrock-knowledge-bases/)

The blog provides a technical deep-dive into implementing multi-tenant RAG architecture using Amazon Bedrock Knowledge Bases, integrating with Amazon S3 for document storage, OpenSearch Service for vector database, and DynamoDB for tenant configuration management. The implementation includes detailed code examples for metadata filtering, tenant configuration storage, and API interactions using AWS SDK for Python (Boto3), specifically demonstrating the usage of RetrieveAndGenerate API with knowledge base configurations and vector search filtering. The solution addresses technical challenges around document chunking, vector embeddings, HNSW algorithm parameters, and considers performance limitations such as ingestion job sizes (max 100GB), document limits (5 million documents per data source), and embedding model throughput.

### [Amazon DynamoDB data modeling for Multi-Tenancy](https://aws.amazon.com/blogs/database/amazon-dynamodb-data-modeling-for-multi-tenancy-part-1/)

This blog series walks through the process of creating a DynamoDB data model using an example multi-tenant application, a customer issue tracking service. The goal of this series is to explore areas that are important for decision-making and provide insights into the influences to help you plan your data model for a multi-tenant application.

In [part1](https://aws.amazon.com/blogs/database/amazon-dynamodb-data-modeling-for-multi-tenancy-part-1/), we define the access patterns and decide on the table design. In [Part 2](https://aws.amazon.com/blogs/database/amazon-dynamodb-data-modeling-for-multi-tenancy-part-2/), we select a partition key design and create the data schema by iterating across the access patterns. Finally, in [Part 3](https://aws.amazon.com/blogs/database/amazon-dynamodb-data-modeling-for-multi-tenancy-part-3/), we validate the data model and explore how to extend the model as new access patterns emerge.

### Videos :movie_camera:

* [ Building SaaS on AWS - Building a modern data architecture for SaaS ](https://www.youtube.com/watch?v=KGR4SQMNsXo)
* [ Data for SaaS YouTube Playlist ](https://youtube.com/playlist?list=PLoqD0z_296PbKATwUcaGowmOJwlE_2ysP&si=kytUeU0uZsNrNF_R)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

