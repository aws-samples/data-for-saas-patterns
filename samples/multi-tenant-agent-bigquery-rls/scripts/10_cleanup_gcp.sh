#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 10_cleanup_gcp.sh — Remove all GCP resources created by setup scripts.

set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"

TENANTS="${TENANTS:-tenant-001 tenant-002}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Cleaning up GCP resources in ${GCP_PROJECT_ID}"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# 1. Drop RLS policies
echo "→ Dropping RLS policies..."
bq --project_id="$GCP_PROJECT_ID" query --use_legacy_sql=false --nouse_cache "
DROP ALL ROW ACCESS POLICIES ON \`${GCP_PROJECT_ID}.saas_pilot.orders\`;
DROP ALL ROW ACCESS POLICIES ON \`${GCP_PROJECT_ID}.saas_pilot.customer_data\`;" 2>/dev/null || true
echo "  ✓ RLS policies dropped"
echo ""

# 2. Delete dataset (includes tables)
echo "→ Deleting dataset: saas_pilot..."
bq rm -r -f "${GCP_PROJECT_ID}:saas_pilot" 2>/dev/null || true
echo "  ✓ Dataset deleted"
echo ""

# 3. Remove project-level IAM bindings and delete service accounts
echo "→ Removing service accounts and IAM bindings..."
for TENANT in $TENANTS; do
  SA_EMAIL="sa-${TENANT}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
  echo "  ${TENANT}: ${SA_EMAIL}"

  # Remove project-level role bindings
  for ROLE in roles/bigquery.dataViewer roles/bigquery.jobUser roles/bigquery.user roles/mcp.toolUser; do
    gcloud projects remove-iam-policy-binding "$GCP_PROJECT_ID" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="$ROLE" \
      --quiet --no-user-output-enabled 2>/dev/null || true
  done
  echo "    ✓ IAM bindings removed"

  # Delete service account
  gcloud iam service-accounts delete "$SA_EMAIL" \
    --project="$GCP_PROJECT_ID" --quiet 2>/dev/null || true
  echo "    ✓ Service account deleted"
done
echo ""

# 4. Delete WIF provider + pool
echo "→ Deleting WIF provider: aws-provider..."
gcloud iam workload-identity-pools providers delete aws-provider \
  --project="$GCP_PROJECT_ID" --location="global" \
  --workload-identity-pool="aws-saas-pool" --quiet 2>/dev/null || true
echo "  ✓ WIF provider deleted"
echo ""

echo "→ Deleting WIF pool: aws-saas-pool..."
gcloud iam workload-identity-pools delete aws-saas-pool \
  --project="$GCP_PROJECT_ID" --location="global" --quiet 2>/dev/null || true
echo "  ✓ WIF pool deleted"
echo ""

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅ GCP cleanup complete                                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  AWS cleanup: cd cdk && cdk destroy --all"
echo ""
echo "  To delete the GCP project entirely:"
echo "    gcloud projects delete ${GCP_PROJECT_ID}"
