# Relational database sharding

The below diagram illustrates an architecture that enables a microservice to route queries to the correct sharded database instance based on the tenant context contained within a JSON Web Token (JWT). 

![arch diagram](./diagrams/relational-database-sharding.png)

## Components

1. **User**: Represents a user interacting with the SaaS application.
2. **Microservice**: The core application component responsible for processing user requests and interacting with the database.
3. **JWT manager**: A component responsible for inspecting and validating JSON Web Tokens (JWTs).
4. **Data access manager**: A component that resolves the connection detailsbetween the microservice and the appropriate database instance.
5. **DynamoDB Mapping Table**: An Amazon DynamoDB table that stores the mapping between tenant IDs and the corresponding database instance details (e.g., shard IDs).
6. The database service hosting the tenant-specific database instances. e.g. **Amazon Aurora**.

## Steps

1. The user initiates a request to the SaaS application, and the request includes a JWT containing the tenant context.
2. The microservice receives the request and passes the JWT to the data access manager.
3. The data access manager invokes the JWT manager to inspect and validate the JWT, extracting the tenant_id field.
4. Using the extracted tenant_id, the data access manager queries the DynamoDB Mapping Table to retrieve the corresponding database instance details (e.g., shard_id).
5. The data access manager returns the database instance details to the microservice.
6. With the database instance details, the microservice can establish a connection to the appropriate Amazon Aurora database instance and perform the necessary operations for the specific tenant.

This architecture promotes data isolation and secure access by ensuring that each tenant's data is stored in a dedicated database instance. The use of JWTs and the centralized mapping table (DynamoDB Mapping Table) enables the routing of requests to the correct database instance based on the tenant context. Additionally, the modular design with separate components for JWT management and data access management promotes code reusability and maintainability.

For more details see [Scale your relational database for SaaS](https://aws.amazon.com/blogs/database/scale-your-relational-database-for-saas-part-2-sharding-and-routing/)