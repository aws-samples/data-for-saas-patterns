"""Unit tests that need no database or S3: run with `python -m pytest tests`."""
import pytest
import hashlib
import importlib

from tenantmig.hashing import Sha256Hasher
from tenantmig.perm_diff import _old_decision


def test_content_ref_is_sha256_of_utf8_and_stable():
    h = Sha256Hasher()
    text = "Réunion à 10h — 📈"
    assert h.content_ref(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert h.content_ref(text) == h.content_ref(str(text))


def test_identical_content_shares_one_object_key():
    h = Sha256Hasher()
    a, b = h.content_ref("has joined the chat"), h.content_ref("has joined the chat")
    assert h.object_key(a) == h.object_key(b) == f"{a[:2]}/{a}"


def test_old_model_wildcard_allow():
    assert _old_decision([("*", "*", "allow")], "record", "read")


def test_old_model_deny_wins_over_allow():
    perms = [("*", "read", "allow"), ("activity_log", "read", "deny")]
    assert _old_decision(perms, "record", "read")
    assert not _old_decision(perms, "activity_log", "read")


def test_old_model_wildcard_deny_beats_specific_allow():
    perms = [("export", "write", "allow"), ("*", "write", "deny")]
    assert not _old_decision(perms, "export", "write")


def test_no_matching_rule_is_deny():
    assert not _old_decision([("policy", "read", "allow")], "policy", "write")


def test_dsn_from_parts_escapes_password(monkeypatch):
    for var in ("V1_MESSAGING_DSN", "V1_MESSAGING_PORT", "FDW_V1_MESSAGING_HOST", "FDW_V1_MESSAGING_PORT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("V1_MESSAGING_HOST", "db.example.internal")
    monkeypatch.setenv("V1_MESSAGING_PASSWORD", "p@ss/w:rd")
    from tenantmig import settings
    try:
        importlib.reload(settings)
        assert settings.V1_MESSAGING_DSN.startswith("postgresql://postgres:p%40ss%2Fw%3Ard@db.example.internal:5432/")
        assert settings.V1_MESSAGING_DSN.endswith("sslmode=require")
        assert settings.FDW_V1_MESSAGING["port"] == "5432"
        # The fdw server settings never carry a login: each tenant's mapping does that.
        assert "p@ss/w:rd" not in str(settings.FDW_V1_MESSAGING)
        assert not any(k for k in settings.FDW_V1_MESSAGING if k in ("user", "password", "reader_user"))
    finally:
        monkeypatch.undo()
        importlib.reload(settings)                      # don't leak the patched settings into other tests


class _FakeS3:
    """Just enough S3 for same_content: bodies by key, and no stored checksums."""

    def __init__(self, bodies):
        self.bodies = bodies

    def head_object(self, Bucket, Key, **_):
        kms = Bucket == "kms"                       # a KMS-encrypted object has no MD5 ETag
        return {"ServerSideEncryption": "aws:kms" if kms else "AES256"}

    def get_object(self, Bucket, Key, **_):
        import io

        class Body(io.BytesIO):
            def iter_chunks(self, size):
                return iter(lambda: self.read(size), b"")

        return {"Body": Body(self.bodies[(Bucket, Key)])}


def test_same_content_uses_etag_for_single_part_and_bytes_for_multipart():
    from tenantmig.store import ContentStore
    bodies = {("src", "a"): b"hello", ("dst", "a"): b"hello",
              ("src", "m"): b"x" * 10, ("dst", "m"): b"x" * 10, ("dst", "changed"): b"y" * 10,
              ("src", "m2"): b"x" * 10}
    store = ContentStore(client=_FakeS3(bodies))
    md5 = "5d41402abc4b2a76b9719d911017c592"
    assert store.same_content("src", "a", (5, md5), "dst", "a", (5, md5))
    assert not store.same_content("src", "a", (5, md5), "dst", "a", (5, "0" * 32))
    # Multipart ETags differ even when the bytes are identical (a copy of a multipart object is
    # a different object): the bytes decide, not the ETag.
    assert store.same_content("src", "m", (10, "abc-3"), "dst", "m", (10, "def-1"))
    # Same size, different bytes: the old size-only check passed this. It must not.
    assert not store.same_content("src", "m", (10, "abc-3"), "dst", "changed", (10, "xyz-3"))
    # A KMS copy has a different ETag from an identical source, so the bytes decide (regression:
    # on AWS every object in a KMS bucket was reported missing).
    bodies[("kms", "a")] = b"hello"
    assert store.same_content("src", "a", (5, md5), "kms", "a", (5, "66330f68f0aa96c8c3b940187141a7d8"))


def test_normalise_org_id_is_canonical_lowercase_uuid():
    from tenantmig.placement import normalise_org_id
    assert normalise_org_id("6513270E-269E-0D37-F2A7-4DE452E6B438") == "6513270e-269e-0d37-f2a7-4de452e6b438"
    assert normalise_org_id("not-a-uuid") == "not-a-uuid"


def test_guarded_step_refuses_a_tenant_that_is_live_in_its_cell(monkeypatch):
    from tenantmig import placement

    class Conn:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=()):
            class R:
                def __init__(self, row):
                    self.row = row

                def fetchone(self):
                    return self.row
            if "pg_try_advisory_lock" in sql:
                return R((True,))
            return R((self.status, "cell-1" if self.status == "v2" else None))

        def commit(self):
            pass

    calls = []

    @placement.guarded("copy things")
    def step(org_id):
        calls.append(org_id)
        return "done"

    monkeypatch.setattr(placement.identities, "control_dsn", lambda base, org: "control")
    monkeypatch.setattr(placement.psycopg, "connect", lambda dsn: Conn("v2"))
    with pytest.raises(SystemExit, match="live in its cell"):
        step("org-1")
    assert calls == []

    monkeypatch.setattr(placement.psycopg, "connect", lambda dsn: Conn("v1"))
    assert step("org-1") == "done" and calls == ["org-1"]


def test_guarded_step_refuses_while_the_cutover_lock_is_held(monkeypatch):
    from tenantmig import placement

    class Locked:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=()):
            class R:
                def fetchone(self):
                    return (False,)
            return R()

    monkeypatch.setattr(placement.identities, "control_dsn", lambda base, org: "control")
    monkeypatch.setattr(placement.psycopg, "connect", lambda dsn: Locked())

    @placement.guarded("copy things")
    def step(org_id):
        raise AssertionError("must not run while the lock is held")

    with pytest.raises(SystemExit, match="already running"):
        step("org-1")


def test_tenant_task_role_grants_object_reads_without_a_listing_condition(monkeypatch):
    """s3:prefix only applies to ListBucket. A GetObject statement that carries it is refused, so
    reads of the tenant's V1 objects (including HeadObject) need their own statement."""
    from tenantmig import settings, task_roles
    monkeypatch.setattr(settings, "ACCOUNT_ID", "111122223333")
    monkeypatch.setattr(settings, "S3_REGION", "us-east-2")
    monkeypatch.setattr(settings, "BATCH_OPS_ROLE_ARN", "arn:aws:iam::111122223333:role/batch")
    monkeypatch.setattr(settings, "CELL_KMS_KEY_ARN", "arn:aws:kms:us-east-2:111122223333:key/k")
    monkeypatch.setenv("CODE_BUCKET", "code")
    org = "6513270e-269e-0d37-f2a7-4de452e6b438"
    statements = {st["Sid"]: st for st in task_roles.policy(org)["Statement"]}
    for st in statements.values():
        if "s3:prefix" in st.get("Condition", {}).get("StringLike", {}):
            assert st["Action"] == "s3:ListBucket", st["Sid"]
    assert statements["OwnV1Objects"]["Action"] == "s3:GetObject"
    assert "s3:prefix" not in statements["OwnV1Objects"].get("Condition", {})
