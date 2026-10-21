"""Synthetic V1 data: organisations (tenants), users, permissions, messages and objects.

Deliberately includes: duplicate content (repeated chat and system messages), tiny and large
messages, unicode, wildcard permissions and explicit denies.
"""
import random
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

from tenantmig import identities, settings

CHANNELS = ["email", "chat", "voice", "video", "sms", "social"]
RESOURCES = ["record", "collection", "policy", "export", "activity_log", "connector"]
ACTIONS = ["read", "write", "approve", "export"]
BOILERPLATE = [
    "has joined the channel", "has left the channel", "Out of office until Monday.",
    "Please see attached.", "Thanks!", "Automated notification: no action required.",
]


def _content(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.35:                      # repeated system/chat noise -> dedupes in S3
        return rng.choice(BOILERPLATE)
    if roll < 0.40:                      # large message
        return "Quarterly update. " + "Lorem ipsum dolor sit amet. " * rng.randint(2_000, 6_000)
    if roll < 0.45:                      # unicode and odd characters
        return f"Réunion à 10h — 東京オフィス 📈 ref {rng.randint(1, 10**9)}"
    return f"Message {uuid.UUID(int=rng.getrandbits(128))} about account {rng.randint(1000, 9999)}"


def _shared_bucket_objects(org_ids: list[str], per_org: int, rng: random.Random) -> None:
    """V1 shared bucket: one bucket, objects under an org-UUID prefix (attachments, originals)."""
    from tenantmig.store import ContentStore
    store = ContentStore()
    store.ensure_bucket(settings.V1_SHARED_BUCKET)
    for org in org_ids:
        # Start each tenant's prefix empty, so earlier runs don't leave extra objects behind.
        for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=settings.V1_SHARED_BUCKET,
                                                                       Prefix=f"{org}/"):
            old = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if old:
                store.s3.delete_objects(Bucket=settings.V1_SHARED_BUCKET, Delete={"Objects": old})
        for i in range(per_org):
            kind = rng.choice(["attachments", "originals"])
            store.s3.put_object(Bucket=settings.V1_SHARED_BUCKET, Key=f"{org}/{kind}/{i:05d}.eml",
                                Body=_content(rng).encode("utf-8"))


def generate(orgs: int = 3, messages_per_org: int = 2_000, seed: int = 7, objects_per_org: int = 200) -> list[str]:
    """Replace V1 with synthetic tenants. TRUNCATES the V1 tables, so it is sandbox-only."""
    from tenantmig.cli import _require_sandbox
    _require_sandbox("truncate V1 and generate synthetic data")
    rng = random.Random(seed)
    org_ids = [str(uuid.UUID(int=rng.getrandbits(128))) for _ in range(orgs)]
    now = datetime.now(timezone.utc)

    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg, psycopg.connect(settings.V1_AUTH_DSN) as auth:
        msg.execute("TRUNCATE messages, connector_configs, organisations CASCADE")
        auth.execute("TRUNCATE role_permissions, user_roles, roles, users, catalogue_resources, "
                     "catalogue_actions CASCADE")
        # The product's catalogue: includes resources and actions no role names explicitly.
        auth.cursor().executemany("INSERT INTO catalogue_resources VALUES (%s)", [(r,) for r in RESOURCES])
        auth.cursor().executemany("INSERT INTO catalogue_actions VALUES (%s)", [(a,) for a in ACTIONS])

        msg_id = user_id = role_id = conn_id = 0
        for n, org in enumerate(org_ids):
            msg.execute("INSERT INTO organisations (id, name, created_at) VALUES (%s, %s, %s)",
                        (org, f"Synthetic Org {n + 1}", now - timedelta(days=900)))

            with msg.cursor().copy(
                "COPY messages (id, org_id, channel, sender, sent_at, created_at, content) FROM STDIN"
            ) as copy:
                for _ in range(messages_per_org):
                    msg_id += 1
                    sent = now - timedelta(minutes=rng.randint(1, 60 * 24 * 700))
                    copy.write_row((msg_id, org, rng.choice(CHANNELS), f"user{rng.randint(1, 40)}@example.com",
                                    sent, sent, _content(rng)))

            for ctype in ("smtp_journal", "chat_api", "voice_capture"):
                conn_id += 1
                credentials = f"FAKE-SECRET-{rng.getrandbits(64):x}"
                msg.execute(
                    "INSERT INTO connector_configs VALUES (%s, %s, %s, %s, %s)",
                    (conn_id, org, ctype, psycopg.types.json.Jsonb({"region": "us-east-2", "poll_seconds": 60}),
                     credentials),
                )
                # The credential moves to Secrets Manager under the name the migration records. Synthetic
                # data does this step itself; a real migration needs its own audited path.
                identities.put_secret_value(identities.connector_secret_name(org, conn_id), credentials)

            # Roles: admin (wildcard), reviewer, exporter (with a wildcard deny), readonly.
            roles = {}
            for name in ("admin", "reviewer", "exporter", "readonly"):
                role_id += 1
                roles[name] = role_id
                auth.execute("INSERT INTO roles VALUES (%s, %s, %s)", (role_id, org, name))
            perms = [
                (roles["admin"], "*", "*", "allow"),
                (roles["reviewer"], "collection", "*", "allow"),
                (roles["reviewer"], "record", "read", "allow"),
                (roles["reviewer"], "policy", "read", "allow"),
                (roles["exporter"], "*", "read", "allow"),
                (roles["exporter"], "export", "export", "allow"),
                (roles["exporter"], "*", "write", "deny"),
                (roles["readonly"], "*", "read", "allow"),
                (roles["readonly"], "activity_log", "read", "deny"),
            ]
            auth.cursor().executemany("INSERT INTO role_permissions VALUES (%s, %s, %s, %s)", perms)

            for u in range(25):
                user_id += 1
                auth.execute("INSERT INTO users VALUES (%s, %s, %s)", (user_id, org, f"user{u + 1}@org{n + 1}.example.com"))
                for name in rng.sample(list(roles), k=rng.randint(1, 2)):
                    auth.execute("INSERT INTO user_roles VALUES (%s, %s)", (user_id, roles[name]))
    _shared_bucket_objects(org_ids, objects_per_org, rng)
    for org in org_ids:
        identities.provision_tenant(org)    # each tenant gets its own login role and password
    return org_ids


def add_new_messages(org_id: str, count: int = 50, seed: int = 99, age_seconds: int = 0) -> None:
    """Simulate V1 still receiving messages for an org mid-migration (created age_seconds ago)."""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    with psycopg.connect(settings.V1_MESSAGING_DSN) as msg:
        start = msg.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
        with msg.cursor().copy(
            "COPY messages (id, org_id, channel, sender, sent_at, created_at, content) FROM STDIN"
        ) as copy:
            for i in range(count):
                copy.write_row((start + i + 1, org_id, rng.choice(CHANNELS), "late@example.com", now, now, _content(rng)))
