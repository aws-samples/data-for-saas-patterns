# SPDX-License-Identifier: MIT-0
"""
Databricks Query Lambda — Tenant-scoped queries via WIF.

Reads tenant_id from Gateway-propagated headers, looks up the tenant's
service principal client_id from DynamoDB, performs WIF exchange to
authenticate as that SP, then executes SQL via the Statement API.
Unity Catalog row filters enforce isolation based on the authenticated SP identity.

DynamoDB-based config: per-tenant client_id lookup enables dynamic onboarding.
"""

import json
import logging
import os
import time

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_TABLE_NAME = os.environ.get("WIF_CONFIG_TABLE", "multi-tenant-databricks-wif-config")
_DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "")
_DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
_REGION = os.environ.get("AWS_REGION", "us-east-1")

_dynamodb = boto3.resource("dynamodb", region_name=_REGION)
_sts_client = boto3.client("sts", region_name=_REGION)
_table = _dynamodb.Table(_TABLE_NAME)

# Token cache: {tenant_id: {"token": str, "expires_at": float}}
# This in-memory cache works within a single Lambda execution environment (warm starts).
# For production with many concurrent Lambda instances, consider ElastiCache (Redis)
# for cross-instance token sharing to reduce WIF exchange calls.
_token_cache = {}


def _get_tenant_config(tenant_id: str) -> dict:
    """Look up per-tenant WIF config from DynamoDB."""
    response = _table.get_item(Key={"tenant_id": tenant_id})
    item = response.get("Item")
    if not item:
        raise ValueError(f"No WIF config for tenant: {tenant_id}")
    return item


def _get_databricks_token(tenant_id: str) -> str:
    """Perform WIF exchange → per-tenant Databricks access token. Cached for 55 min."""
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
    }))

    return access_token


def _execute_sql(token: str, sql: str) -> list:
    """Execute SQL via Databricks SQL Statement API."""
    response = requests.post(
        f"{_DATABRICKS_HOST}/api/2.0/sql/statements/",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "warehouse_id": _DATABRICKS_WAREHOUSE_ID,
            "statement": sql,
            "wait_timeout": "30s",
        },
        timeout=35,
    )

    if response.status_code != 200:
        raise RuntimeError(f"SQL execution failed: {response.status_code} {response.text}")

    result = response.json()
    status = result.get("status", {}).get("state", "")

    if status != "SUCCEEDED":
        error = result.get("status", {}).get("error", {}).get("message", "Unknown error")
        raise RuntimeError(f"SQL failed: {status} — {error}")

    # Parse result into rows
    columns = [col["name"] for col in result.get("manifest", {}).get("schema", {}).get("columns", [])]
    data_array = result.get("result", {}).get("data_array", [])
    rows = [dict(zip(columns, row)) for row in data_array]

    return rows


def lambda_handler(event: dict, context) -> dict:
    """Handle Databricks query tool invocations from AgentCore Gateway."""

    # Read tenant_id from propagated headers (Gateway delivers via client_context)
    tenant_id = ""
    try:
        propagated_headers = context.client_context.custom.get(
            "bedrockAgentCorePropagatedHeaders", {}
        )
        if isinstance(propagated_headers, str):
            propagated_headers = json.loads(propagated_headers)
        tenant_id = propagated_headers.get("x-tenant-id", "")
    except (AttributeError, TypeError, json.JSONDecodeError):
        pass

    # Fallback: direct invocation (testing)
    if not tenant_id:
        headers = event.get("headers", {})
        tenant_id = headers.get("X-Tenant-ID", headers.get("x-tenant-id", ""))

    if not tenant_id:
        return {"statusCode": 400, "body": json.dumps({"error": "X-Tenant-ID missing"})}

    sql = event.get("sql", "")
    if not sql:
        return {"statusCode": 400, "body": json.dumps({"error": "sql field required"})}

    # Only allow SELECT queries (defense-in-depth alongside row filters)
    if not sql.strip().upper().startswith("SELECT"):
        return {"statusCode": 400, "body": json.dumps({"error": "Only SELECT queries are permitted"})}

    try:
        token = _get_databricks_token(tenant_id)
    except ValueError as e:
        return {"statusCode": 403, "body": json.dumps({"error": str(e)})}
    except RuntimeError as e:
        logger.error(f"WIF exchange failed for {tenant_id}: {e}")
        return {"statusCode": 502, "body": json.dumps({"error": f"WIF exchange failed: {e}"})}

    try:
        logger.info(json.dumps({
            "event": "databricks_query",
            "tenant_id": tenant_id,
            "sql_preview": sql[:100],
        }))
        rows = _execute_sql(token, sql)

        return {
            "statusCode": 200,
            "body": json.dumps(
                {"results": rows, "row_count": len(rows), "tenant_id": tenant_id},
                default=str,
            ),
        }
    except Exception as e:
        logger.error(f"Databricks query failed for {tenant_id}: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e), "tenant_id": tenant_id})}
