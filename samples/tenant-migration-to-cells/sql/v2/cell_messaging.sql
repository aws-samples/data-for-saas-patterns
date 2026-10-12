-- V2 cell messaging schema: `content` dropped, `content_ref` added
-- (content-addressable hash; also the S3 object key prefix).
-- Other columns match V1. Example schema: replace with your own.

CREATE TABLE organisations (
    id          uuid PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL
);

CREATE TABLE messages (
    id           bigint PRIMARY KEY,
    org_id       uuid NOT NULL REFERENCES organisations(id),
    channel      text NOT NULL,
    sender       text NOT NULL,
    sent_at      timestamptz NOT NULL,
    created_at   timestamptz NOT NULL,
    content_ref  text NOT NULL
);

-- Migration bookkeeping: per-org watermark for delta passes.
CREATE SCHEMA migration;
CREATE TABLE migration.org_state (
    org_id         uuid PRIMARY KEY,
    last_message_id bigint NOT NULL DEFAULT 0,
    passes         int NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
