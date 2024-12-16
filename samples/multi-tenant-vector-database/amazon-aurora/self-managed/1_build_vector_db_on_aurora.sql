--Step 1 : Enable the pgvector extension (You will need rds_superuser privilege)

CREATE EXTENSION IF NOT EXISTS vector;

--Step 2:  Verify the version of the pgvector extension

SELECT extversion FROM pg_extension WHERE extname='vector';

--Step 3 : Create a schema (You will need database owner privilege)
CREATE SCHEMA self_managed;

--Step 4 : Create the Vector table

CREATE TABLE self_managed.kb (id uuid PRIMARY KEY, embedding vector(1536), chunks TEXT, metadata JSONB, tenantid BIGINT);

--Step 5 : Create the Index 

CREATE INDEX on self_managed.kb USING hnsw (embedding vector_cosine_ops);

-- Step 6 : Enable Row Level Security
CREATE POLICY tenant_policy ON self_managed.kb USING (tenantid = current_setting('self_managed.kb.tenantid')::BIGINT);

ALTER TABLE self_managed.kb enable row level security;

CREATE ROLE app_user WITH PASSWORD '<update with secure password>' LOGIN;

GRANT ALL ON SCHEMA self_managed to app_user;
GRANT SELECT ON TABLE self_managed.kb to app_user;

