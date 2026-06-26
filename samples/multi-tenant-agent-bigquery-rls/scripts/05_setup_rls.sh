#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 05_setup_rls.sh — Apply BigQuery Row-Level Security policies.
#
# This is the CRITICAL isolation step. Once applied:
#   - sa-tenant-001 sees ONLY tenant-001 rows
#   - sa-tenant-002 sees ONLY tenant-002 rows
#   - Any other identity sees ZERO rows (deny-by-default)
#
# The agent writes tenant-unaware SQL (SELECT * FROM orders).
# RLS filters based on WHO is asking, not WHAT they asked.
#
# Prerequisites:
#   - 03_setup_service_accounts.sh completed (SAs exist)
#   - 04_setup_bigquery_data.sh completed (tables + data exist)
#
# Usage:
#   export GCP_PROJECT_ID=saas-workshop-bq
#   bash 05_setup_rls.sh

set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"

TENANTS="${TENANTS:-tenant-001 tenant-002}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Step 5: Row-Level Security (Deny-by-Default)                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

echo "→ Applying RLS policies..."
echo ""

for TENANT in $TENANTS; do
  SA_EMAIL="sa-${TENANT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

  echo "  ${TENANT} → ${SA_EMAIL}"

  bq --project_id="$GCP_PROJECT_ID" query --use_legacy_sql=false --nouse_cache "
  CREATE OR REPLACE ROW ACCESS POLICY ${TENANT//-/_}_orders
  ON \`${GCP_PROJECT_ID}.saas_pilot.orders\`
  GRANT TO ('serviceAccount:${SA_EMAIL}')
  FILTER USING (tenant_id = '${TENANT}');

  CREATE OR REPLACE ROW ACCESS POLICY ${TENANT//-/_}_customers
  ON \`${GCP_PROJECT_ID}.saas_pilot.customer_data\`
  GRANT TO ('serviceAccount:${SA_EMAIL}')
  FILTER USING (tenant_id = '${TENANT}');"

  echo "    ✓ RLS: orders + customer_data"
  echo ""
done

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ RLS Behavior                                                 │"
echo "├──────────────────────────────────────────────────────────────┤"
echo "│                                                              │"
echo "│  SELECT * FROM orders (same SQL for all):                    │"
echo "│    sa-tenant-001 → 3 rows (tenant-001 only)                  │"
echo "│    sa-tenant-002 → 2 rows (tenant-002 only)                  │"
echo "│    any other SA  → 0 rows (deny-by-default!)                 │"
echo "│                                                              │"
echo "│  The identity IS the filter. No WHERE clause needed.         │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
echo "  ✅ Step 5 complete. GCP setup done!"
echo ""
echo "  Test: bash 06_test_lambda_target.sh"
