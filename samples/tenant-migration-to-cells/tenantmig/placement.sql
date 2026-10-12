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
