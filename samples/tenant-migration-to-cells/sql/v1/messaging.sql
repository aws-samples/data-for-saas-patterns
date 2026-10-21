-- Example V1 messaging schema (replace with your own).
-- Message content is stored inline; platform/connector config also lives here.

CREATE TABLE organisations (
    id          uuid PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id          bigint PRIMARY KEY,
    org_id      uuid NOT NULL REFERENCES organisations(id),
    channel     text NOT NULL,          -- email, chat, voice, ...
    sender      text NOT NULL,
    sent_at     timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    content     text NOT NULL
);
CREATE INDEX messages_org_id_id ON messages (org_id, id);

-- Platform configuration that moves to the V2 management DB.
CREATE TABLE connector_configs (
    id              bigint PRIMARY KEY,
    org_id          uuid NOT NULL REFERENCES organisations(id),
    connector_type  text NOT NULL,
    settings        jsonb NOT NULL,
    credentials     text NOT NULL        -- must NOT travel through the bulk path
);
