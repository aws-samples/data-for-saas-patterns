#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 03_setup_service_accounts.sh — Create per-tenant GCP service accounts + WIF bindings.
#
# Each tenant gets a dedicated GCP SA. The SA's identity IS the tenant boundary.
# BigQuery RLS (next step) binds row access to these SA identities.
#
# WIF bindings allow the Lambda/Interceptor IAM roles to impersonate each SA.
# No GCP secrets stored — the IAM role IS the credential.
#
# Prerequisites:
#   - 01_setup_wif.sh completed
#   - CDK deployed (provides IAM role names)
#
# Usage:
#   export GCP_PROJECT_ID=saas-workshop-bq
#   bash 03_setup_service_accounts.sh

set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"

AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text --no-cli-pager)}"
GCP_PROJECT_NUM=$(gcloud projects describe "$GCP_PROJECT_ID" --format="value(projectNumber)")

# IAM role names from CDK deployment
QUERY_ROLE_NAME="${QUERY_ROLE_NAME:-multi-tenant-agent-bigquery-query-role}"
INTERCEPTOR_ROLE_NAME="${INTERCEPTOR_ROLE_NAME:-multi-tenant-agent-bigquery-interceptor-role}"

WIF_POOL="aws-saas-pool"
TENANTS="${TENANTS:-tenant-001 tenant-002}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Step 3: Per-Tenant Service Accounts + WIF Bindings          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  GCP Project:      ${GCP_PROJECT_ID}"
echo "  Tenants:          ${TENANTS}"
echo "  Query Role:       ${QUERY_ROLE_NAME}"
echo "  Interceptor Role: ${INTERCEPTOR_ROLE_NAME}"
echo ""

for TENANT in $TENANTS; do
  SA_NAME="sa-${TENANT}"
  SA_EMAIL="${SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

  echo "→ Provisioning: ${TENANT}"

  # Create SA (or undelete if soft-deleted)
  SA_UID=$(gcloud iam service-accounts describe "$SA_EMAIL" \
    --project="$GCP_PROJECT_ID" --format="value(uniqueId)" 2>/dev/null || echo "")
  if [[ -z "$SA_UID" ]]; then
    gcloud iam service-accounts create "$SA_NAME" \
      --project="$GCP_PROJECT_ID" \
      --display-name="SaaS Agent - ${TENANT}" 2>/dev/null || true
    # Wait for SA to propagate before binding roles
    sleep 5
  fi

  # Grant BigQuery + MCP permissions
  for ROLE in roles/bigquery.dataViewer roles/bigquery.jobUser roles/bigquery.user roles/mcp.toolUser; do
    gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="$ROLE" \
      --quiet --no-user-output-enabled 2>/dev/null || true
  done

  # WIF binding: Lambda query role → SA
  gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
    --project="$GCP_PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/${GCP_PROJECT_NUM}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.aws_role/arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${QUERY_ROLE_NAME}" \
    --quiet --no-user-output-enabled 2>/dev/null || true

  # WIF binding: Interceptor role → SA
  gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
    --project="$GCP_PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/${GCP_PROJECT_NUM}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.aws_role/arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${INTERCEPTOR_ROLE_NAME}" \
    --quiet --no-user-output-enabled 2>/dev/null || true

  echo "  ✓ ${SA_EMAIL}"
  echo "    Roles: dataViewer, jobUser, user, mcp.toolUser"
  echo "    WIF:   ${QUERY_ROLE_NAME} + ${INTERCEPTOR_ROLE_NAME} can impersonate"
  echo ""
done

echo "  ✅ Step 3 complete. Next: bash 04_setup_bigquery_data.sh"
