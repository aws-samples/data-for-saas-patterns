# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
identity.py — Read the caller's tenant identity from an AgentCore Runtime request.

AgentCore Runtime's JWT authorizer verifies the token's signature before the
request reaches the container, so this module only decodes the claims. Run
outside AgentCore Runtime, nothing verifies the token.
"""

import base64
import json
from typing import Optional


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT's payload without verifying it (AgentCore Runtime already did)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def extract_identity(request_headers: Optional[dict]) -> Optional[dict]:
    """
    Return {"tenant_context": {...}, "user_id": str} from the Authorization
    header, or None if the token or its custom:tenant_id or sub claim is missing.

    user_id is always the JWT `sub` claim — never anything from the request
    body. The tenant id format is validated later by the session manager.
    """
    headers     = request_headers or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    claims      = decode_jwt_payload(auth_header.removeprefix("Bearer "))
    tenant_id   = claims.get("custom:tenant_id", "")
    user_id     = claims.get("sub", "")
    if not tenant_id or not user_id:
        return None
    return {
        "tenant_context": {
            "tenantId":   tenant_id,
            "tenantName": claims.get("custom:tenant_name", ""),
            "tier":       claims.get("custom:tier", "standard"),
        },
        "user_id": user_id,
    }
