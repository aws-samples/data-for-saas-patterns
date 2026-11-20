# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""test_identity.py — Unit tests for reading tenant identity from the JWT."""

import base64
import json

from agentcore_memory.identity import decode_jwt_payload, extract_identity


def _token(claims) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


CLAIMS = {"sub": "0a1b2c3d", "custom:tenant_id": "tenant-001", "custom:tenant_name": "Acme", "custom:tier": "premium"}


def test_identity_from_bearer_token():
    identity = extract_identity({"Authorization": f"Bearer {_token(CLAIMS)}"})
    assert identity == {
        "tenant_context": {"tenantId": "tenant-001", "tenantName": "Acme", "tier": "premium"},
        "user_id": "0a1b2c3d",
    }


def test_lowercase_header_is_accepted():
    assert extract_identity({"authorization": f"Bearer {_token(CLAIMS)}"})["user_id"] == "0a1b2c3d"


def test_missing_tenant_claim_returns_none():
    claims = {k: v for k, v in CLAIMS.items() if k != "custom:tenant_id"}
    assert extract_identity({"Authorization": f"Bearer {_token(claims)}"}) is None


def test_missing_sub_returns_none():
    claims = {k: v for k, v in CLAIMS.items() if k != "sub"}
    assert extract_identity({"Authorization": f"Bearer {_token(claims)}"}) is None


def test_no_header_returns_none():
    assert extract_identity(None) is None
    assert extract_identity({}) is None


def test_malformed_tokens_decode_to_empty():
    assert decode_jwt_payload("not-a-jwt") == {}
    assert decode_jwt_payload("a.!!!.c") == {}
    body = base64.urlsafe_b64encode(b"[1, 2]").decode().rstrip("=")
    assert decode_jwt_payload(f"a.{body}.c") == {}
