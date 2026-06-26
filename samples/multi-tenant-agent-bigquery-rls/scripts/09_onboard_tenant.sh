#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# onboard_tenant.sh — Add a new tenant to the system.
#
# This is the PRODUCTION workflow for tenant onboarding.
# Creates the GCP SA, grants roles, binds WIF trust, and adds RLS policies.
#
# NO AWS changes needed. No redeployment. No config update.
# The template-based WIF config resolves the new tenant automatically.
#
# Usage:
#   export GCP_PROJECT_ID=saas-workshop-bq
#   bash onboard_tenant.sh tenant-003
#
# After this, any JWT with custom:tenant_id="tenant-003" will:
#   1. Route through the same gateway
#   2. Interceptor resolves sa-tenant-003 via template
#   3. WIF exchange → GCP token for sa-tenant-003
#   4. BigQuery RLS filters to tenant-003 rows only

set -euo pipefail

TENANT_ID="${1:?Usage: bash onboard_tenant.sh <tenant-id>}"

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text --no-cli-pager)}"
GCP_PROJECT_NUM=$(gcloud projects describe "$GCP_PROJECT_ID" --format="value(projectNumber)")

QUERY_ROLE_NAME="${QUERY_ROLE_NAME:-multi-tenant-agent-bigquery-query-role}"
INTERCEPTOR_ROLE_NAME="${INTERCEPTOR_ROLE_NAME:-multi-tenant-agent-bigquery-interceptor-role}"
WIF_POOL="aws-saas-pool"

SA_NAME="sa-${TENANT_ID}"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Onboard Tenant: ${TENANT_ID}"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Step 1: Create service account
echo "→ [1/4] Creating service account: ${SA_NAME}"
gcloud iam service-accounts create "$SA_NAME" \
  --project="$GCP_PROJECT_ID" \
  --display-name="SaaS Agent - ${TENANT_ID}" 2>/dev/null || echo "  (already exists)"
echo "  ✓ ${SA_EMAIL}"
echo ""

# Step 2: Grant roles
echo "→ [2/4] Granting BigQuery + MCP permissions..."
for ROLE in roles/bigquery.dataViewer roles/bigquery.jobUser roles/bigquery.user roles/mcp.toolUser; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$ROLE" > /dev/null 2>&1
done
echo "  ✓ Roles: dataViewer, jobUser, user, mcp.toolUser"
echo ""

# Step 3: Bind WIF trust
echo "→ [3/4] Binding WIF trust (Lambda + Interceptor → SA)..."
for ROLE_NAME in "$QUERY_ROLE_NAME" "$INTERCEPTOR_ROLE_NAME"; do
  gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
    --project="$GCP_PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/${GCP_PROJECT_NUM}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.aws_role/arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${ROLE_NAME}" \
    > /dev/null 2>&1
  echo "  ✓ ${ROLE_NAME} → ${SA_NAME}"
done
echo ""

# Step 4: Add RLS policies
echo "→ [4/4] Adding Row-Level Security policies..."
POLICY_PREFIX="${TENANT_ID//-/_}"

bq --project_id="$GCP_PROJECT_ID" query --use_legacy_sql=false --nouse_cache "
CREATE OR REPLACE ROW ACCESS POLICY ${POLICY_PREFIX}_orders
ON \`${GCP_PROJECT_ID}.saas_pilot.orders\`
GRANT TO ('serviceAccount:${SA_EMAIL}')
FILTER USING (tenant_id = '${TENANT_ID}');

CREATE OR REPLACE ROW ACCESS POLICY ${POLICY_PREFIX}_customers
ON \`${GCP_PROJECT_ID}.saas_pilot.customer_data\`
GRANT TO ('serviceAccount:${SA_EMAIL}')
FILTER USING (tenant_id = '${TENANT_ID}');"
echo "  ✓ RLS: orders + customer_data"
echo ""

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Tenant Onboarded: ${TENANT_ID}"
echo "├──────────────────────────────────────────────────────────────┤"
echo "│                                                              │"
echo "│  SA:  ${SA_EMAIL}"
echo "│  RLS: FILTER USING (tenant_id = '${TENANT_ID}')"
echo "│                                                              │"
echo "│  AWS changes needed: NONE                                    │"
echo "│  Config update needed: NONE                                  │"
echo "│  Redeployment needed: NONE                                   │"
echo "│                                                              │"
echo "│  The template-based WIF config resolves sa-${TENANT_ID}     │"
echo "│  automatically at runtime.                                   │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
echo "  ✅ Tenant ${TENANT_ID} is ready."
echo "     Create a Cognito user with custom:tenant_id='${TENANT_ID}' to test."
