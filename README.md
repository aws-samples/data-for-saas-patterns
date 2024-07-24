# AWS Data for SaaS

This repository contains a collection of samples, best practices and reference architectures for implementing SaaS applications on AWS for databases and data services. 

## Contents

* [RDS Data API Row-level Security](README.md#rds-data-api-row-level-security)
* [Multi-tenant vector databases](README.md#multi-tenant-vector-databases)

## RDS Data API Row-level Security

Row-level security is commonly employed in multi-tenant databases to provide isolation between tenant's data. Row level security policies are created in the database to enforce this isolation on tenant-owned tables. 

This sample contains 2 examples for implementing row-level security using the [RDS data API for Amazon Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html). The examples provided are in Python, but they can be easily transferred to other languages using the same patterns. Additionally, these examples will work for Amazon Aurora PostgreSQL provisioned or serverless V2.

[Example 1 - RLS with PostgreSQL function](rds-data-api-rls/rds-data-api-rls-function.ipynb)

[Example 2 - RLS with database transactions](rds-data-api-rls/rds-data-api-rls-transaction.ipynb)

## Multi-tenant vector databases

### Amazon Aurora

Vector databases are commonly employed to store embeddings generated as part of generative-AI applications. A popular option is to use the pg_vector extension for PostgreSQL. 

This sample shows how to use pg_vector in a multi-tenant database, enforcing tenant isolation. One example is using a self-managed Retrieval-augmented generation (RAG) implementation, the other is using [Amazon Bedrock knowledge bases](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html). 

[Example 1 - Self-managed](multi-tenant-vector-database/amazon-aurora/self-managed/)

[Example 2 - AWS-managed](multi-tenant-vector-database/amazon-aurora/aws-managed/)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

