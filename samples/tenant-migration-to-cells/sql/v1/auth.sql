-- Example V1 auth schema (replace with your own).
-- Role-based model with wildcards and explicit denies (deny wins).

CREATE TABLE users (
    id      bigint PRIMARY KEY,
    org_id  uuid NOT NULL,
    email   text NOT NULL
);

CREATE TABLE roles (
    id      bigint PRIMARY KEY,
    org_id  uuid NOT NULL,
    name    text NOT NULL
);

CREATE TABLE user_roles (
    user_id bigint NOT NULL REFERENCES users(id),
    role_id bigint NOT NULL REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);

-- resource and action may be '*'. effect is 'allow' or 'deny'.
CREATE TABLE role_permissions (
    role_id   bigint NOT NULL REFERENCES roles(id),
    resource  text NOT NULL,
    action    text NOT NULL,
    effect    text NOT NULL CHECK (effect IN ('allow', 'deny')),
    PRIMARY KEY (role_id, resource, action, effect)
);
