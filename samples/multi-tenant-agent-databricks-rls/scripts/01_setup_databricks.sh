#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 01_setup_databricks.sh — Databricks-side multi-tenant setup
#
# Handles Databricks-only setup:
#   Phase 1: Unity Catalog (catalog, schema, tables, sample data)
#   Phase 2: Per-tenant service principals
#   Phase 3: Row filter function + tenant_user_map table + grants
#   Phase 4: SQL Warehouse access for SPs
#   Phase 5: UC Function for MCP Server target
#
# Prerequisites:
#   export DATABRICKS_HOST=https://dbc-xxxxx.cloud.databricks.com
#   export DATABRICKS_TOKEN=dapi...
#   export DATABRICKS_WAREHOUSE_ID=<warehouse-id>
#
# Usage:
#   bash scripts/01_setup_databricks.sh

trap 'echo ""; echo "  ⚠ Script interrupted."; exit 1' INT

if [ -z "${DATABRICKS_HOST:-}" ]; then echo "  ✗ DATABRICKS_HOST not set."; exit 1; fi
if [ -z "${DATABRICKS_TOKEN:-}" ]; then echo "  ✗ DATABRICKS_TOKEN not set."; exit 1; fi
if [ -z "${DATABRICKS_WAREHOUSE_ID:-}" ]; then echo "  ✗ DATABRICKS_WAREHOUSE_ID not set."; exit 1; fi

AWS_REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Helper: execute SQL via Databricks Statement API
run_sql() {
  local sql="$1"
  local result
  result=$(curl -s -X POST "${DATABRICKS_HOST}/api/2.0/sql/statements/" \
    -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"warehouse_id\": \"${DATABRICKS_WAREHOUSE_ID}\", \"statement\": \"${sql}\", \"wait_timeout\": \"30s\"}")

  local state
  state=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',{}).get('state','FAILED'))" 2>/dev/null || echo "PARSE_ERROR")

  if [ "$state" = "SUCCEEDED" ]; then
    echo "    ✓ OK"
  else
    local error
    error=$(echo "$result" | python3 -c "
import sys, json
r = json.load(sys.stdin)
# Check for top-level API error (e.g. warehouse stopped)
if 'error_code' in r:
    print(f\"{r.get('error_code')}: {r.get('message', 'Unknown')}\")
else:
    print(r.get('status',{}).get('error',{}).get('message','Unknown error'))
" 2>/dev/null || echo "Failed to parse response: ${result:0:200}")
    echo "    ✗ ${error}"
    return 1
  fi
}

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   Databricks Multi-Tenant Setup                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Workspace:  ${DATABRICKS_HOST}"
echo "  Warehouse:  ${DATABRICKS_WAREHOUSE_ID}"
echo ""

# ═══════════════════════════════════════════════════════════════════
# PHASE 1: Catalog, Schema & Tables
# ═══════════════════════════════════════════════════════════════════
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Phase 1: Catalog, Schema & Tables                            │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""

echo "  [1/4] Creating catalog + schema"
run_sql "CREATE CATALOG IF NOT EXISTS saas_workshop"
run_sql "CREATE SCHEMA IF NOT EXISTS saas_workshop.saas_pilot"

echo "  [2/4] Creating tables"
run_sql "CREATE TABLE IF NOT EXISTS saas_workshop.saas_pilot.orders (order_id STRING, tenant_id STRING, product STRING, amount DECIMAL(10,2), order_date DATE, status STRING)"
run_sql "CREATE TABLE IF NOT EXISTS saas_workshop.saas_pilot.customer_data (customer_id STRING, tenant_id STRING, name STRING, email STRING, plan_tier STRING, created_at TIMESTAMP)"

echo "  [3/4] Inserting sample data (tenant-001: 3 orders, tenant-002: 2 orders)"
run_sql "DELETE FROM saas_workshop.saas_pilot.orders"
run_sql "INSERT INTO saas_workshop.saas_pilot.orders VALUES ('ord-001', 'tenant-001', 'Widget Pro', 99.99, '2026-01-15', 'completed'), ('ord-002', 'tenant-001', 'Widget Basic', 29.99, '2026-02-01', 'completed'), ('ord-003', 'tenant-001', 'Widget Enterprise', 499.99, '2026-03-10', 'pending'), ('ord-004', 'tenant-002', 'Gadget X', 149.99, '2026-01-20', 'completed'), ('ord-005', 'tenant-002', 'Gadget Y', 79.99, '2026-02-15', 'shipped')"
run_sql "DELETE FROM saas_workshop.saas_pilot.customer_data"
run_sql "INSERT INTO saas_workshop.saas_pilot.customer_data VALUES ('cust-001', 'tenant-001', 'Alice Smith', 'alice@tenant1.com', 'premium', '2025-06-01T00:00:00'), ('cust-002', 'tenant-001', 'Bob Jones', 'bob@tenant1.com', 'standard', '2025-08-15T00:00:00'), ('cust-003', 'tenant-002', 'Carol White', 'carol@tenant2.com', 'premium', '2025-07-01T00:00:00')"

echo "  [4/4] ✓ 5 orders + 3 customers across 2 tenants"
echo ""
echo "  ✅ Phase 1 complete"
echo ""

# ═══════════════════════════════════════════════════════════════════
# PHASE 2: Per-Tenant Service Principals
# ═══════════════════════════════════════════════════════════════════
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Phase 2: Per-Tenant Service Principals                       │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
echo "  Note: With WIF, service principals do NOT need OAuth secrets."
echo "  The SPs are identified by applicationId, which serves as the"
echo "  client_id in the WIF token exchange. No client_secret needed."
echo ""

create_sp() {
  local display_name="$1"
  local sp_result
  sp_result=$(curl -s -X POST "${DATABRICKS_HOST}/api/2.0/preview/scim/v2/ServicePrincipals" \
    -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"displayName\": \"${display_name}\", \"active\": true}")

  local app_id
  app_id=$(echo "$sp_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('applicationId',''))" 2>/dev/null)
  local sp_id
  sp_id=$(echo "$sp_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

  if [ -z "$app_id" ] || [ "$app_id" = "None" ]; then
    # SP may already exist — search for it
    local search_result
    search_result=$(curl -s "${DATABRICKS_HOST}/api/2.0/preview/scim/v2/ServicePrincipals?filter=displayName+eq+%22${display_name}%22" \
      -H "Authorization: Bearer ${DATABRICKS_TOKEN}")
    app_id=$(echo "$search_result" | python3 -c "import sys,json; r=json.load(sys.stdin).get('Resources',[]); print(r[0]['applicationId'] if r else '')" 2>/dev/null)
    sp_id=$(echo "$search_result" | python3 -c "import sys,json; r=json.load(sys.stdin).get('Resources',[]); print(r[0]['id'] if r else '')" 2>/dev/null)
  fi

  echo "${app_id}:${sp_id}"
}

echo "  [1/2] Creating service principal: sp-tenant-001"
SP1_RESULT=$(create_sp "sp-tenant-001")
SP1_APP_ID=$(echo "$SP1_RESULT" | cut -d: -f1)
SP1_INTERNAL_ID=$(echo "$SP1_RESULT" | cut -d: -f2)
echo "    ✓ applicationId: ${SP1_APP_ID}"

echo "  [2/2] Creating service principal: sp-tenant-002"
SP2_RESULT=$(create_sp "sp-tenant-002")
SP2_APP_ID=$(echo "$SP2_RESULT" | cut -d: -f1)
SP2_INTERNAL_ID=$(echo "$SP2_RESULT" | cut -d: -f2)
echo "    ✓ applicationId: ${SP2_APP_ID}"

echo ""
echo "  ✅ Phase 2 complete (no OAuth secrets generated — WIF replaces M2M)"
echo ""

# ═══════════════════════════════════════════════════════════════════
# PHASE 3: Row Filter + Mapping Table + Grants
# ═══════════════════════════════════════════════════════════════════
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Phase 3: Row Filter + Mapping Table + Grants                 │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""

echo "  [1/5] Creating tenant_user_map table"
run_sql "CREATE TABLE IF NOT EXISTS saas_workshop.saas_pilot.tenant_user_map (user_email STRING, tenant_id STRING)"
run_sql "DELETE FROM saas_workshop.saas_pilot.tenant_user_map"
run_sql "INSERT INTO saas_workshop.saas_pilot.tenant_user_map VALUES ('${SP1_APP_ID}', 'tenant-001'), ('${SP2_APP_ID}', 'tenant-002')"

echo "  [2/5] Creating row filter function"
run_sql "CREATE OR REPLACE FUNCTION saas_workshop.saas_pilot.tenant_filter(row_tenant_id STRING) RETURNS BOOLEAN RETURN EXISTS (SELECT 1 FROM saas_workshop.saas_pilot.tenant_user_map WHERE user_email = current_user() AND tenant_id = row_tenant_id)"

echo "  [3/5] Applying row filter to tables"
run_sql "ALTER TABLE saas_workshop.saas_pilot.orders DROP ROW FILTER" 2>/dev/null || true
run_sql "ALTER TABLE saas_workshop.saas_pilot.customer_data DROP ROW FILTER" 2>/dev/null || true
run_sql "ALTER TABLE saas_workshop.saas_pilot.orders SET ROW FILTER saas_workshop.saas_pilot.tenant_filter ON (tenant_id)"
run_sql "ALTER TABLE saas_workshop.saas_pilot.customer_data SET ROW FILTER saas_workshop.saas_pilot.tenant_filter ON (tenant_id)"

echo "  [4/5] Granting permissions to sp-tenant-001"
run_sql "GRANT USE CATALOG ON CATALOG saas_workshop TO \`${SP1_APP_ID}\`"
run_sql "GRANT USE SCHEMA ON SCHEMA saas_workshop.saas_pilot TO \`${SP1_APP_ID}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.orders TO \`${SP1_APP_ID}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.customer_data TO \`${SP1_APP_ID}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.tenant_user_map TO \`${SP1_APP_ID}\`"
run_sql "GRANT EXECUTE ON FUNCTION saas_workshop.saas_pilot.tenant_filter TO \`${SP1_APP_ID}\`"

echo "  [5/5] Granting permissions to sp-tenant-002"
run_sql "GRANT USE CATALOG ON CATALOG saas_workshop TO \`${SP2_APP_ID}\`"
run_sql "GRANT USE SCHEMA ON SCHEMA saas_workshop.saas_pilot TO \`${SP2_APP_ID}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.orders TO \`${SP2_APP_ID}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.customer_data TO \`${SP2_APP_ID}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.tenant_user_map TO \`${SP2_APP_ID}\`"
run_sql "GRANT EXECUTE ON FUNCTION saas_workshop.saas_pilot.tenant_filter TO \`${SP2_APP_ID}\`"

echo ""
echo "  ✅ Phase 3 complete"
echo ""

# ═══════════════════════════════════════════════════════════════════
# PHASE 4: SQL Warehouse Access
# ═══════════════════════════════════════════════════════════════════
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Phase 4: SQL Warehouse Access for Service Principals         │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
echo "  Granting CAN_USE on warehouse ${DATABRICKS_WAREHOUSE_ID} to both SPs..."

curl -s -X PUT "${DATABRICKS_HOST}/api/2.0/permissions/sql/warehouses/${DATABRICKS_WAREHOUSE_ID}" \
  -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"access_control_list\": [{\"service_principal_name\": \"${SP1_APP_ID}\", \"permission_level\": \"CAN_USE\"}, {\"service_principal_name\": \"${SP2_APP_ID}\", \"permission_level\": \"CAN_USE\"}]}" > /dev/null 2>&1 \
  && echo "    ✓ Both SPs granted CAN_USE on SQL warehouse" \
  || echo "    ⚠ Failed to grant warehouse access — grant manually via SQL Warehouses → Permissions"

echo ""
echo "  ✅ Phase 4 complete"
echo ""

# ═══════════════════════════════════════════════════════════════════
# PHASE 5: UC Function for MCP Server Target
# ═══════════════════════════════════════════════════════════════════
echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Phase 5: UC Function (for MCP Server target)                 │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""

echo "  [1/3] Creating function: saas_workshop.saas_pilot.get_orders"
run_sql "CREATE OR REPLACE FUNCTION saas_workshop.saas_pilot.get_orders() RETURNS TABLE(order_id STRING, tenant_id STRING, product STRING, amount DECIMAL(10,2), order_date DATE, status STRING) RETURN SELECT * FROM saas_workshop.saas_pilot.orders"

echo "  [2/3] Granting EXECUTE to sp-tenant-001"
run_sql "GRANT EXECUTE ON FUNCTION saas_workshop.saas_pilot.get_orders TO \`${SP1_APP_ID}\`"

echo "  [3/3] Granting EXECUTE to sp-tenant-002"
run_sql "GRANT EXECUTE ON FUNCTION saas_workshop.saas_pilot.get_orders TO \`${SP2_APP_ID}\`"

echo ""
echo "  ✅ Phase 5 complete"
echo ""

# ─── Write .lab_env ───────────────────────────────────────────────
cat > "${SCRIPT_DIR}/.lab_env" <<EOF
export DATABRICKS_HOST="${DATABRICKS_HOST}"
export DATABRICKS_WAREHOUSE_ID="${DATABRICKS_WAREHOUSE_ID}"
export SP1_APP_ID="${SP1_APP_ID}"
export SP2_APP_ID="${SP2_APP_ID}"
export AWS_REGION="${AWS_REGION}"
EOF

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   ✅ Databricks Setup Complete                               ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  Catalog:       saas_workshop.saas_pilot                     ║"
echo "║  Row Filter:    tenant_filter (current_user() → map)         ║"
echo "║  SP tenant-001: ${SP1_APP_ID}"
echo "║  SP tenant-002: ${SP2_APP_ID}"
echo "║                                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Next: bash scripts/02_setup_wif.sh"
