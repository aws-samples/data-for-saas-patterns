"""Unit tests that need no database or S3: run with `python -m pytest tests`."""
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
    assert _old_decision([("*", "*", "allow")], "mailbox", "read")


def test_old_model_deny_wins_over_allow():
    perms = [("*", "read", "allow"), ("audit_log", "read", "deny")]
    assert _old_decision(perms, "mailbox", "read")
    assert not _old_decision(perms, "audit_log", "read")


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
        assert settings.FDW_V1_MESSAGING["reader_user"] == "tenantmig_reader"    # never the master user
        assert "password" not in settings.FDW_V1_MESSAGING
    finally:
        monkeypatch.undo()
        importlib.reload(settings)                      # don't leak the patched settings into other tests
