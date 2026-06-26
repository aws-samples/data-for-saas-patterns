#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 01_setup_wif.sh — Create Workload Identity Federation pool + AWS provider.
#
# One-time setup that establishes cross-cloud trust between your AWS account
# and GCP project. This allows AWS IAM roles to obtain GCP tokens without
# storing any GCP secrets.
#
# Prerequisites:
#   - gcloud authenticated
#   - GCP project created with billing enabled
#
# Usage:
#   export GCP_PROJECT_ID=saas-workshop-bq
#   bash 01_setup_wif.sh

set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text --no-cli-pager)}"
WIF_POOL="aws-saas-pool"
WIF_PROVIDER="aws-provider"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Step 1: Workload Identity Federation (Cross-Cloud Trust)    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  GCP Project:  ${GCP_PROJECT_ID}"
echo "  AWS Account:  ${AWS_ACCOUNT_ID}"
echo "  WIF Pool:     ${WIF_POOL}"
echo "  WIF Provider: ${WIF_PROVIDER}"
echo ""

# Enable required APIs
echo "→ Enabling GCP APIs..."
gcloud services enable \
  iam.googleapis.com \
  sts.googleapis.com \
  iamcredentials.googleapis.com \
  bigquery.googleapis.com \
  --project="$GCP_PROJECT_ID" --quiet 2>/dev/null
echo "  ✓ APIs enabled"
echo ""

# Create WIF Pool (or undelete if soft-deleted)
echo "→ Creating Workload Identity Pool: ${WIF_POOL}"
echo "  This pool defines which external identity providers GCP trusts."
POOL_STATE=$(gcloud iam workload-identity-pools describe "$WIF_POOL" \
  --project="$GCP_PROJECT_ID" --location="global" --format="value(state)" 2>/dev/null || echo "NOT_FOUND")

if [[ "$POOL_STATE" == "DELETED" ]]; then
  echo "  (undeleting soft-deleted pool)"
  gcloud iam workload-identity-pools undelete "$WIF_POOL" \
    --project="$GCP_PROJECT_ID" --location="global" --quiet 2>/dev/null
  sleep 5
elif [[ "$POOL_STATE" == "NOT_FOUND" ]]; then
  gcloud iam workload-identity-pools create "$WIF_POOL" \
    --project="$GCP_PROJECT_ID" \
    --location="global" \
    --display-name="AWS SaaS Agent Pool" 2>/dev/null || echo "  (already exists)"
else
  echo "  (already exists)"
fi
echo "  ✓ Pool ready"
echo ""

# Add AWS Provider (or undelete if soft-deleted)
echo "→ Adding AWS provider: ${WIF_PROVIDER}"
echo "  Tells GCP: 'Trust IAM roles from AWS account ${AWS_ACCOUNT_ID}'"
PROVIDER_STATE=$(gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" \
  --project="$GCP_PROJECT_ID" --location="global" \
  --workload-identity-pool="$WIF_POOL" --format="value(state)" 2>/dev/null || echo "NOT_FOUND")

if [[ "$PROVIDER_STATE" == "DELETED" ]]; then
  echo "  (undeleting soft-deleted provider)"
  gcloud iam workload-identity-pools providers undelete "$WIF_PROVIDER" \
    --project="$GCP_PROJECT_ID" --location="global" \
    --workload-identity-pool="$WIF_POOL" --quiet 2>/dev/null
  sleep 5
elif [[ "$PROVIDER_STATE" == "NOT_FOUND" ]]; then
  gcloud iam workload-identity-pools providers create-aws "$WIF_PROVIDER" \
    --project="$GCP_PROJECT_ID" \
    --location="global" \
    --workload-identity-pool="$WIF_POOL" \
    --account-id="$AWS_ACCOUNT_ID" 2>/dev/null || echo "  (already exists)"
else
  echo "  (already exists)"
fi

# Ensure attribute mapping is configured (required for WIF bindings)
echo "  Configuring attribute mapping..."
gcloud iam workload-identity-pools providers update-aws "$WIF_PROVIDER" \
  --project="$GCP_PROJECT_ID" \
  --location="global" \
  --workload-identity-pool="$WIF_POOL" \
  --account-id="$AWS_ACCOUNT_ID" \
  --attribute-mapping="google.subject=assertion.arn,attribute.aws_role=assertion.arn.contains('assumed-role') ? assertion.arn.extract('{account_arn}assumed-role/') + 'assumed-role/' + assertion.arn.extract('assumed-role/{role_name}/') : assertion.arn" \
  --quiet 2>/dev/null || true
echo "  ✓ AWS provider configured"
echo ""

# Output the WIF audience (needed for CDK context or Secrets Manager)
GCP_PROJECT_NUM=$(gcloud projects describe "$GCP_PROJECT_ID" --format="value(projectNumber)")
WIF_AUDIENCE="//iam.googleapis.com/projects/${GCP_PROJECT_NUM}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}"

# Generate wif_config.json for Lambda functions
echo "→ Generating wif_config.json for Lambda functions..."
WIF_CONFIG=$(cat <<EOF
{
  "GCP_PROJECT_ID": "${GCP_PROJECT_ID}",
  "wif_template": {
    "type": "external_account",
    "audience": "${WIF_AUDIENCE}",
    "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
    "token_url": "https://sts.googleapis.com/v1/token",
    "credential_source": {
      "environment_id": "aws1",
      "regional_cred_verification_url": "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15",
      "imdsv2_session_token_url": "http://169.254.169.254/latest/api/token"
    },
    "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/sa-{tenant_id}@${GCP_PROJECT_ID}.iam.gserviceaccount.com:generateAccessToken"
  }
}
EOF
)

echo "$WIF_CONFIG" > "${ROOT_DIR}/lambda/bigquery_query/wif_config.json"
echo "$WIF_CONFIG" > "${ROOT_DIR}/lambda/interceptor/wif_config.json"
echo "  ✓ lambda/bigquery_query/wif_config.json"
echo "  ✓ lambda/interceptor/wif_config.json"
echo ""

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ WIF Configuration                                            │"
echo "├──────────────────────────────────────────────────────────────┤"
echo "│ Pool:     ${WIF_POOL}                                        │"
echo "│ Provider: ${WIF_PROVIDER}                                    │"
echo "│ Audience: ${WIF_AUDIENCE}"
echo "│ Project#: ${GCP_PROJECT_NUM}                                 │"
echo "│                                                              │"
echo "│ Trust: Any IAM role in account ${AWS_ACCOUNT_ID} can request │"
echo "│        GCP tokens (specific SA binding required separately). │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
echo "  ✅ Step 1 complete. Next: bash 02_build_layers.sh"
