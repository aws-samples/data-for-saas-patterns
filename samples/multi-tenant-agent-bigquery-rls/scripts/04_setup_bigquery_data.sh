#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 04_setup_bigquery_data.sh — Create BigQuery dataset, tables, and sample data.
#
# Creates shared multi-tenant tables with a tenant_id column.
# Both tenants' data lives side-by-side in the same tables.
# At this point there is NO isolation — any authenticated user sees ALL rows.
# RLS (next step) adds the security boundary.
#
# Prerequisites:
#   - GCP project with BigQuery API enabled + billing linked
#
# Usage:
#   export GCP_PROJECT_ID=saas-workshop-bq
#   bash 04_setup_bigquery_data.sh

set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Step 4: BigQuery Dataset, Tables & Sample Data              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Create dataset
echo "→ Creating dataset: saas_pilot"
bq --project_id="$GCP_PROJECT_ID" mk --dataset --location=US saas_pilot 2>/dev/null || true
echo "  ✓ Dataset ready"
echo ""

# Create tables
echo "→ Creating tables..."
bq --project_id="$GCP_PROJECT_ID" query --use_legacy_sql=false --nouse_cache "
CREATE TABLE IF NOT EXISTS \`${GCP_PROJECT_ID}.saas_pilot.orders\` (
  order_id STRING NOT NULL, tenant_id STRING NOT NULL,
  product STRING, amount NUMERIC, order_date DATE, status STRING
);
CREATE TABLE IF NOT EXISTS \`${GCP_PROJECT_ID}.saas_pilot.customer_data\` (
  customer_id STRING NOT NULL, tenant_id STRING NOT NULL,
  name STRING, email STRING, plan_tier STRING, created_at TIMESTAMP
);"
echo "  ✓ Tables created (orders, customer_data)"
echo ""

# Drop any existing RLS (required for idempotent data insertion)
echo "→ Clearing existing RLS policies (for idempotent re-runs)..."
bq --project_id="$GCP_PROJECT_ID" query --use_legacy_sql=false --nouse_cache "
DROP ALL ROW ACCESS POLICIES ON \`${GCP_PROJECT_ID}.saas_pilot.orders\`;
DROP ALL ROW ACCESS POLICIES ON \`${GCP_PROJECT_ID}.saas_pilot.customer_data\`;" 2>/dev/null || true
echo "  ✓ RLS cleared"
echo ""

# Insert sample data
echo "→ Inserting sample data..."
bq --project_id="$GCP_PROJECT_ID" query --use_legacy_sql=false --nouse_cache "
DELETE FROM \`${GCP_PROJECT_ID}.saas_pilot.orders\` WHERE TRUE;
INSERT INTO \`${GCP_PROJECT_ID}.saas_pilot.orders\` VALUES
  ('ord-001','tenant-001','Widget Pro',99.99,'2026-01-15','completed'),
  ('ord-002','tenant-001','Widget Basic',29.99,'2026-02-01','completed'),
  ('ord-003','tenant-001','Widget Enterprise',499.99,'2026-03-10','pending'),
  ('ord-004','tenant-002','Gadget X',149.99,'2026-01-20','completed'),
  ('ord-005','tenant-002','Gadget Y',79.99,'2026-02-15','shipped');

DELETE FROM \`${GCP_PROJECT_ID}.saas_pilot.customer_data\` WHERE TRUE;
INSERT INTO \`${GCP_PROJECT_ID}.saas_pilot.customer_data\` VALUES
  ('cust-001','tenant-001','Alice Smith','alice@tenant1.com','premium','2025-06-01 00:00:00 UTC'),
  ('cust-002','tenant-001','Bob Jones','bob@tenant1.com','standard','2025-08-15 00:00:00 UTC'),
  ('cust-003','tenant-002','Carol White','carol@tenant2.com','premium','2025-07-01 00:00:00 UTC');"
echo "  ✓ 5 orders + 3 customers across 2 tenants"
echo ""

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Data Summary                                                 │"
echo "├──────────────────────────────────────────────────────────────┤"
echo "│ tenant-001: 3 orders, 2 customers                           │"
echo "│ tenant-002: 2 orders, 1 customer                            │"
echo "│                                                              │"
echo "│ ⚠ Currently NO isolation — RLS adds the boundary next.      │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
echo "  ✅ Step 4 complete. Next: bash 05_setup_rls.sh"
