#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 07_cleanup.sh — Clean up Databricks resources.
#
# Removes service principals, tables, catalog, and row filters created by this sample.
#
# Prerequisites:
#   export DATABRICKS_HOST=https://dbc-xxxxx.cloud.databricks.com
#   export DATABRICKS_TOKEN=dapi...
#
# Usage:
#   bash scripts/07_cleanup.sh

set -euo pipefail

: "${DATABRICKS_HOST:?Set DATABRICKS_HOST}"
: "${DATABRICKS_TOKEN:?Set DATABRICKS_TOKEN}"
: "${DATABRICKS_WAREHOUSE_ID:?Set DATABRICKS_WAREHOUSE_ID}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Cleanup: Databricks Resources                               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

run_sql() {
  curl -s -X POST "${DATABRICKS_HOST}/api/2.0/sql/statements/" \
    -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"warehouse_id\": \"${DATABRICKS_WAREHOUSE_ID}\", \"statement\": $(echo "$1" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'), \"wait_timeout\": \"30s\"}" > /dev/null 2>&1
}

# Remove row filters
echo "→ Removing row filters..."
run_sql "ALTER TABLE saas_workshop.saas_pilot.orders DROP ROW FILTER"
run_sql "ALTER TABLE saas_workshop.saas_pilot.customer_data DROP ROW FILTER"
echo "  ✓ Row filters removed"

# Drop tables and schema
echo "→ Dropping tables and schema..."
run_sql "DROP TABLE IF EXISTS saas_workshop.saas_pilot.orders"
run_sql "DROP TABLE IF EXISTS saas_workshop.saas_pilot.customer_data"
run_sql "DROP TABLE IF EXISTS saas_workshop.saas_pilot.tenant_user_map"
run_sql "DROP FUNCTION IF EXISTS saas_workshop.saas_pilot.tenant_filter"
run_sql "DROP SCHEMA IF EXISTS saas_workshop.saas_pilot CASCADE"
run_sql "DROP CATALOG IF EXISTS saas_workshop CASCADE"
echo "  ✓ Schema and catalog dropped"

# Delete service principals
echo "→ Deleting service principals..."

python3 -c "
import urllib.request, urllib.error, json, os

host = os.environ.get('DATABRICKS_HOST', '')
token = os.environ.get('DATABRICKS_TOKEN', '')
account_id = os.environ.get('DATABRICKS_ACCOUNT_ID', '')
account_token = os.environ.get('DATABRICKS_ACCOUNT_TOKEN', '')
deleted = 0

def api_get(url, auth_token):
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {auth_token}'})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError:
        return {}

def api_delete(url, auth_token):
    req = urllib.request.Request(url, method='DELETE', headers={'Authorization': f'Bearer {auth_token}'})
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError:
        pass

# Workspace-level deletion
data = api_get(f'{host}/api/2.0/preview/scim/v2/ServicePrincipals?filter=displayName+sw+%22sp-tenant-%22&count=100', token)
for sp in data.get('Resources', []):
    sp_id, name = sp['id'], sp.get('displayName', '?')
    api_delete(f'{host}/api/2.0/preview/scim/v2/ServicePrincipals/{sp_id}', token)
    print(f'  deleted {name} ({sp_id})')
    deleted += 1

# Account-level deletion
if account_id and account_token:
    data = api_get(f'https://accounts.cloud.databricks.com/api/2.0/accounts/{account_id}/scim/v2/ServicePrincipals?filter=displayName+sw+%22sp-tenant-%22&count=100', account_token)
    for sp in data.get('Resources', []):
        sp_id, name = sp['id'], sp.get('displayName', '?')
        api_delete(f'https://accounts.cloud.databricks.com/api/2.0/accounts/{account_id}/scim/v2/ServicePrincipals/{sp_id}', account_token)
        print(f'  deleted account SP: {name} ({sp_id})')
        deleted += 1
else:
    print('  Set DATABRICKS_ACCOUNT_ID and DATABRICKS_ACCOUNT_TOKEN to clean account-level SPs')

if deleted == 0:
    print('  (no sp-tenant-* service principals found)')
else:
    print(f'  removed {deleted} service principal(s)')
"
echo ""

echo "  ✅ Databricks cleanup complete."
echo ""
echo "  To clean up AWS resources:"
echo "    cd cdk && source .venv/bin/activate && cdk destroy --all"
