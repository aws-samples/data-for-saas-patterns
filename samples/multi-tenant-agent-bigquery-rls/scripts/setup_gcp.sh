#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# setup_gcp.sh — Express path: provision all GCP resources in sequence.
#
# Calls the individual setup scripts in order:
#   01_setup_wif.sh              — WIF pool + AWS provider + generates wif_config.json
#   03_setup_service_accounts.sh — Per-tenant SAs + WIF bindings
#   04_setup_bigquery_data.sh    — Dataset, tables, sample data
#   05_setup_rls.sh              — Row-Level Security policies
#
# For step-by-step exploration, run each script individually.
#
# Prerequisites:
#   - gcloud authenticated
#   - GCP project created with billing enabled
#   - CDK deployed (provides IAM role names)
#
# Usage:
#   export GCP_PROJECT_ID=saas-workshop-bq
#   bash setup_gcp.sh

set -euo pipefail

: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "════════════════════════════════════════════════════════════════"
echo "  GCP Setup — Express Path"
echo "════════════════════════════════════════════════════════════════"
echo ""

bash "${SCRIPT_DIR}/01_setup_wif.sh"
echo ""

bash "${SCRIPT_DIR}/03_setup_service_accounts.sh"
echo ""

bash "${SCRIPT_DIR}/04_setup_bigquery_data.sh"
echo ""

bash "${SCRIPT_DIR}/05_setup_rls.sh"
echo ""

echo "════════════════════════════════════════════════════════════════"
echo "  ✅ All GCP resources provisioned."
echo ""
echo "  Test: bash 06_test_lambda_target.sh"
echo ""
echo "  To add more tenants later:"
echo "    bash 09_onboard_tenant.sh tenant-003"
echo "════════════════════════════════════════════════════════════════"
