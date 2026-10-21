-- Example V2 management schema (replace with your own).
-- Connector config moves here from messaging; permissions become explicit allow grants.

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

CREATE TABLE organisations (
    id          uuid PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL
);

CREATE TABLE connectors (
    id              bigint PRIMARY KEY,
    org_id          uuid NOT NULL REFERENCES organisations(id),
    connector_type  text NOT NULL,
    settings        jsonb NOT NULL,
    secret_ref      text NOT NULL    -- pointer to Secrets Manager; credentials never copied here
);

CREATE TABLE principals (
    id      bigint PRIMARY KEY,
    org_id  uuid NOT NULL REFERENCES organisations(id),
    email   text NOT NULL
);

-- Flattened, allow-only model: one row per (principal, resource, action).
CREATE TABLE grants (
    principal_id bigint NOT NULL REFERENCES principals(id),
    resource     text NOT NULL,
    action       text NOT NULL,
    PRIMARY KEY (principal_id, resource, action)
);

-- Old V1 ID -> new V2 ID, kept for audit and rollback.
CREATE TABLE id_map (
    org_id  uuid NOT NULL,
    entity  text NOT NULL,
    old_id  text NOT NULL,
    new_id  text NOT NULL,
    PRIMARY KEY (org_id, entity, old_id)
);

-- Routing source of truth: which platform (and cell) serves each tenant.
CREATE TABLE IF NOT EXISTS tenant_placement (
    org_id      uuid PRIMARY KEY,
    status      text NOT NULL CHECK (status IN ('v1', 'frozen', 'v2')),
    cell        text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tenant_placement_log (
    id      bigserial PRIMARY KEY,
    org_id  uuid NOT NULL,
    status  text NOT NULL,
    cell    text,
    note    text NOT NULL,
    at      timestamptz NOT NULL DEFAULT now()
);
