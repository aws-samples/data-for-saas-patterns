# SPDX-License-Identifier: MIT-0
"""
Gateway Interceptor — Dual-mode: Header injection + WIF token exchange.

Lambda targets: Injects x-tenant-id header (Lambda does its own WIF internally).
MCP Server targets: Performs WIF exchange, injects Authorization: Bearer <gcp-token>.

Template-based: one WIF template serves unlimited tenants via {tenant_id} placeholder.
"""

import copy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import jwt  # PyJWT

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_config_cache = None
_token_cache = {}  # {tenant_id: {"token": str, "expires_at": float}}


def _get_wif_config() -> dict:
    """Load WIF template from static wif_config.json (cached)."""
    global _config_cache
    if _config_cache is None:
        config_path = Path(__file__).parent / "wif_config.json"
        with open(config_path) as f:
            _config_cache = json.load(f)
    return _config_cache


def _get_gcp_token(tenant_id: str) -> str:
    """Perform WIF exchange → per-tenant GCP access token. Cached for 55 min."""
    import time

    cached = _token_cache.get(tenant_id)
    if cached and cached["expires_at"] > time.time():
        return cached["token"]

    config = _get_wif_config()
    if not config:
        raise ValueError("WIF config not loaded (wif_config.json missing?)")

    # Resolve using template or legacy per-tenant lookup
    if "wif_template" in config:
        wif_config = copy.deepcopy(config["wif_template"])
        wif_config["service_account_impersonation_url"] = (
            wif_config["service_account_impersonation_url"].replace("{tenant_id}", tenant_id)
        )
    elif tenant_id in config.get("tenants", {}):
        wif_config = config["tenants"][tenant_id]["wif_config"]
    else:
        raise ValueError(f"No WIF config for tenant: {tenant_id}")

    from google.auth import aws as google_auth_aws
    from google.auth.transport.requests import Request

    credentials = google_auth_aws.Credentials.from_info(wif_config)
    credentials = credentials.with_scopes(
        ["https://www.googleapis.com/auth/bigquery"]
    )
    credentials.refresh(Request())

    _token_cache[tenant_id] = {
        "token": credentials.token,
        "expires_at": time.time() + 3300,
    }

    logger.info(json.dumps({
        "event": "wif_token_exchange",
        "tenant_id": tenant_id,
        "sa": wif_config["service_account_impersonation_url"].split("/")[-1].replace(":generateAccessToken", ""),
    }))

    return credentials.token


def _pass_through(request_body: dict) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": request_body}},
    }


def _error_response(status_code: int, request_id, message: str) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": status_code,
                "body": {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": message}},
            }
        },
    }


def _extract_tenant_id(auth_header: str) -> str:
    """Decode JWT → custom:tenant_id (signature already verified by Gateway)."""
    if not auth_header:
        raise ValueError("Authorization header missing")
    token = auth_header.replace("Bearer ", "").replace("bearer ", "")
    claims = jwt.decode(token, options={"verify_signature": False})
    tenant_id = claims.get("custom:tenant_id", "")
    if not tenant_id:
        raise ValueError("JWT missing custom:tenant_id claim")
    return tenant_id


def _handle_invoke_tool(event: dict, tenant_id: str) -> dict:
    """Route based on target type: Lambda (header) vs MCP Server (WIF + token)."""
    mcp_payload = event.get("mcp", {})
    gateway_request = mcp_payload.get("gatewayRequest", {})
    request_body = gateway_request.get("body", {})
    tool_name = request_body.get("params", {}).get("name", "unknown")

    logger.info(json.dumps({
        "event": "tool_invocation",
        "tenant_id": tenant_id,
        "tool": tool_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }))

    is_mcp_server_target = tool_name.startswith("bigquery-mcp___")

    if is_mcp_server_target:
        gcp_token = _get_gcp_token(tenant_id)
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "body": request_body,
                    "headers": {
                        "Authorization": f"Bearer {gcp_token}",
                        "x-tenant-id": tenant_id,
                    },
                },
            },
        }
    else:
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "body": request_body,
                    "headers": {"x-tenant-id": tenant_id},
                },
            },
        }


def lambda_handler(event: dict, context) -> dict:
    """Entry point — called by Gateway on every MCP request."""
    try:
        mcp_payload = event.get("mcp", {})
        gateway_request = mcp_payload.get("gatewayRequest", {})
        request_body = gateway_request.get("body", {})
        mcp_method = request_body.get("method", "") if isinstance(request_body, dict) else ""

        if mcp_method != "tools/call":
            return _pass_through(request_body)

        headers = gateway_request.get("headers", {})
        auth_header = headers.get("Authorization", headers.get("authorization", ""))
        tenant_id = _extract_tenant_id(auth_header)

        return _handle_invoke_tool(event, tenant_id)

    except ValueError as e:
        logger.error(f"Auth error: {e}")
        return _error_response(401, 1, str(e))
    except Exception as e:
        logger.error(f"Interceptor error: {e}")
        return _error_response(500, 1, f"Interceptor error: {e}")
