# SPDX-License-Identifier: MIT-0
"""
BigQuery Query Lambda — Tenant-scoped queries via WIF.

Reads tenant_id from Gateway-propagated headers, performs WIF exchange
to authenticate as the tenant's GCP service account, executes SQL.
BigQuery RLS filters rows based on the authenticated SA identity.

Template-based config: one WIF template serves unlimited tenants.
"""

import copy
import json
import logging
from pathlib import Path

from google.auth import aws as google_auth_aws
from google.auth.transport.requests import Request
from google.cloud import bigquery

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_config_cache = None


def _get_config() -> dict:
    """Load WIF template from static wif_config.json (cached across invocations)."""
    global _config_cache
    if _config_cache is None:
        config_path = Path(__file__).parent / "wif_config.json"
        with open(config_path) as f:
            _config_cache = json.load(f)
    return _config_cache


def _resolve_wif_config(config: dict, tenant_id: str) -> dict:
    """Resolve WIF config for a tenant using the template approach."""
    if "wif_template" in config:
        template = copy.deepcopy(config["wif_template"])
        template["service_account_impersonation_url"] = (
            template["service_account_impersonation_url"].replace("{tenant_id}", tenant_id)
        )
        return template

    # Fallback: per-tenant config (legacy)
    tenants = config.get("tenants", {})
    if tenant_id in tenants:
        return tenants[tenant_id]["wif_config"]

    raise ValueError(f"No WIF config for tenant: {tenant_id}")


def _get_bq_client(config: dict, tenant_id: str) -> bigquery.Client:
    """Get BigQuery client authenticated as the tenant's GCP service account."""
    wif_config = _resolve_wif_config(config, tenant_id)
    credentials = google_auth_aws.Credentials.from_info(wif_config)
    credentials = credentials.with_scopes(
        ["https://www.googleapis.com/auth/bigquery"]
    )
    return bigquery.Client(
        project=config["GCP_PROJECT_ID"], credentials=credentials
    )


def lambda_handler(event: dict, context) -> dict:
    """Handle BigQuery query tool invocations from AgentCore Gateway."""

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

    config = _get_config()

    try:
        client = _get_bq_client(config, tenant_id)
    except ValueError as e:
        return {"statusCode": 403, "body": json.dumps({"error": str(e)})}

    try:
        logger.info(json.dumps({"event": "bigquery_query", "tenant_id": tenant_id, "sql_preview": sql[:100]}))
        results = client.query(sql).result()
        rows = [dict(row) for row in results]

        return {
            "statusCode": 200,
            "body": json.dumps(
                {"results": rows, "row_count": len(rows), "tenant_id": tenant_id},
                default=str,
            ),
        }
    except Exception as e:
        logger.error(f"BigQuery query failed for {tenant_id}: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e), "tenant_id": tenant_id})}
