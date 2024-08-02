--Step 1 : Enable the pgvector extension

CREATE EXTENSION IF NOT EXISTS vector;

--Step 2:  Verify the version of the pgvector extension

SELECT extversion FROM pg_extension WHERE extname='vector';

--Step 3 : Create a schema and grant permissions 

CREATE SCHEMA aws_managed;
CREATE ROLE bedrock_user WITH PASSWORD '<update with secure password>' LOGIN;
GRANT ALL ON SCHEMA aws_managed to bedrock_user;

--Step 4 : Create the Vector table

CREATE TABLE aws_managed.kb (id uuid PRIMARY KEY, embedding vector(1536), chunks text, metadata json, tenantid varchar(10));
GRANT ALL ON TABLE aws_managed.kb to bedrock_user;

--Step 5 : Create the Index 

CREATE INDEX on aws_managed.kb USING hnsw (embedding vector_cosine_ops);

