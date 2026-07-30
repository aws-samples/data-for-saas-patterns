# SPDX-License-Identifier: MIT-0
"""
Gateway Interceptor — Dual-mode: Header injection + WIF token exchange.

Lambda targets: Injects x-tenant-id header (Lambda does its own WIF internally).
MCP Server targets: Performs WIF exchange per-tenant, injects Authorization: Bearer <databricks-token>.

DynamoDB-based: per-tenant client_id lookup enables dynamic tenant onboarding.
"""

import json
import logging
import os
import time

import jwt  # PyJWT

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_TABLE_NAME = os.environ.get("WIF_CONFIG_TABLE", "multi-tenant-databricks-wif-config")
_DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "")
_REGION = os.environ.get("AWS_REGION", "us-east-1")

_token_cache = {}  # {tenant_id: {"token": str, "expires_at": float}}
# This in-memory cache works within a single Lambda execution environment (warm starts).
# For production with many concurrent Lambda instances, consider ElastiCache (Redis)
# for cross-instance token sharing to reduce WIF exchange calls.

import boto3
import requests

_dynamodb = boto3.resource("dynamodb", region_name=_REGION)
_sts_client = boto3.client("sts", region_name=_REGION)
_table = _dynamodb.Table(_TABLE_NAME)


def _get_tenant_config(tenant_id: str) -> dict:
    """Look up per-tenant WIF config from DynamoDB."""
    response = _table.get_item(Key={"tenant_id": tenant_id})
    item = response.get("Item")
    if not item:
        raise ValueError(f"No WIF config for tenant: {tenant_id}")
    return item


def _get_databricks_token(tenant_id: str) -> str:
    """Perform WIF exchange → per-tenant Databricks access token. Cached."""
    cached = _token_cache.get(tenant_id)
    if cached and cached["expires_at"] > time.time():
        return cached["token"]

    config = _get_tenant_config(tenant_id)
    client_id = config["client_id"]

    # Step 1: Get AWS OIDC token (signed by AWS STS)
    oidc_response = _sts_client.get_web_identity_token(
        Audience=["databricks"],
        SigningAlgorithm="RS256",
        DurationSeconds=300,
    )
    aws_oidc_token = oidc_response["WebIdentityToken"]

    # Step 2: Exchange AWS OIDC token for Databricks OAuth token
    token_endpoint = f"{_DATABRICKS_HOST}/oidc/v1/token"
    exchange_response = requests.post(
        token_endpoint,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": aws_oidc_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "client_id": client_id,
            "scope": "all-apis",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )

    if exchange_response.status_code != 200:
        raise RuntimeError(
            f"Databricks token exchange failed for {tenant_id}: "
            f"{exchange_response.status_code} {exchange_response.text}"
        )

    token_data = exchange_response.json()
    access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 3600)

    # Cache with 5-minute buffer against the Databricks token lifetime
    _token_cache[tenant_id] = {
        "token": access_token,
        "expires_at": time.time() + expires_in - 300,
    }

    logger.info(json.dumps({
        "event": "wif_token_exchange",
        "tenant_id": tenant_id,
        "client_id": client_id[:8] + "...",
        "target": "interceptor",
    }))

    return access_token


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
    from datetime import datetime, timezone

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

    is_mcp_server_target = tool_name.startswith("databricks-mcp___")

    if is_mcp_server_target:
        databricks_token = _get_databricks_token(tenant_id)
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "body": request_body,
                    "headers": {
                        "Authorization": f"Bearer {databricks_token}",
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

        # For non-tool-call requests (initialize, tools/list), inject a WIF token
        # so the Databricks MCP Server can authenticate the request.
        # In DYNAMIC listing mode, tools/list goes through the interceptor.
        if mcp_method != "tools/call":
            try:
                headers = gateway_request.get("headers", {})
                auth_header = headers.get("Authorization", headers.get("authorization", ""))
                if not auth_header:
                    return _error_response(401, request_body.get("id", 1), "Authorization header required")
                tid = _extract_tenant_id(auth_header)
                databricks_token = _get_databricks_token(tid)
                return {
                    "interceptorOutputVersion": "1.0",
                    "mcp": {
                        "transformedGatewayRequest": {
                            "body": request_body,
                            "headers": {
                                "Authorization": f"Bearer {databricks_token}",
                            },
                        },
                    },
                }
            except Exception:
                # If token exchange fails, pass through (Lambda targets don't need it)
                return _pass_through(request_body)

        # For tools/call: extract tenant from JWT and route appropriately
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
