#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 06_onboard_tenant.sh — Add a new tenant to the system.
#
# This is the PRODUCTION workflow for tenant onboarding.
# Creates the Databricks SP, adds federation policy, inserts into DynamoDB,
# and updates tenant_user_map.
#
# NO AWS Lambda redeployment needed. Lambda reads DynamoDB at runtime.
#
# Usage:
#   export DATABRICKS_HOST=https://dbc-xxxxx.cloud.databricks.com
#   export DATABRICKS_TOKEN=dapi...
#   export DATABRICKS_ACCOUNT_ID=<account-id>
#   export DATABRICKS_ACCOUNT_TOKEN=<account-level-token>  (optional)
#   bash scripts/06_onboard_tenant.sh tenant-003
#
# After this, any JWT with custom:tenant_id="tenant-003" will:
#   1. Route through the same Gateway
#   2. Interceptor resolves tenant-003 from JWT
#   3. Lambda reads DynamoDB → gets client_id for tenant-003
#   4. WIF exchange → Databricks token for sp-tenant-003
#   5. Row filter: current_user() → tenant_user_map → only tenant-003 rows

set -euo pipefail

TENANT_ID="${1:?Usage: bash 06_onboard_tenant.sh <tenant-id>}"

: "${DATABRICKS_HOST:?Set DATABRICKS_HOST}"
: "${DATABRICKS_TOKEN:?Set DATABRICKS_TOKEN}"
: "${DATABRICKS_ACCOUNT_ID:?Set DATABRICKS_ACCOUNT_ID}"

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="MultiTenantAgentDatabricks"

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --no-cli-pager)
DATABRICKS_WAREHOUSE_ID="${DATABRICKS_WAREHOUSE_ID:?Set DATABRICKS_WAREHOUSE_ID}"

QUERY_ROLE_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='QueryRoleName'].OutputValue" --output text 2>/dev/null || echo "multi-tenant-agent-databricks-query-role")

INTERCEPTOR_ROLE_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='InterceptorRoleName'].OutputValue" --output text 2>/dev/null || echo "multi-tenant-agent-databricks-interceptor-role")

WIF_TABLE_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='WifConfigTableName'].OutputValue" --output text 2>/dev/null || echo "multi-tenant-databricks-wif-config")

SP_NAME="sp-${TENANT_ID}"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Onboard Tenant: ${TENANT_ID}"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Step 1: Create Databricks Service Principal
echo "→ [1/5] Creating service principal: ${SP_NAME}"
SP_RESULT=$(curl -s -X POST "${DATABRICKS_HOST}/api/2.0/preview/scim/v2/ServicePrincipals" \
  -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"displayName\": \"${SP_NAME}\", \"active\": true}")

APP_ID=$(echo "$SP_RESULT" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r.get('applicationId',''))" 2>/dev/null || echo "")
SP_ID=$(echo "$SP_RESULT" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r.get('id',''))" 2>/dev/null || echo "")

if [[ -z "$APP_ID" ]]; then
  # SP might already exist
  SP_INFO=$(curl -s "${DATABRICKS_HOST}/api/2.0/preview/scim/v2/ServicePrincipals?filter=displayName+eq+%22${SP_NAME}%22" \
    -H "Authorization: Bearer ${DATABRICKS_TOKEN}")
  APP_ID=$(echo "$SP_INFO" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r.get('Resources',[])[0]['applicationId'])" 2>/dev/null || echo "")
  SP_ID=$(echo "$SP_INFO" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r.get('Resources',[])[0]['id'])" 2>/dev/null || echo "")
  echo "  ✓ ${SP_NAME} (already exists, applicationId: ${APP_ID:0:8}...)"
else
  echo "  ✓ ${SP_NAME} (created, applicationId: ${APP_ID:0:8}...)"
fi
echo ""

# Step 2: Grant permissions
echo "→ [2/5] Granting Databricks permissions..."

run_sql() {
  curl -s -X POST "${DATABRICKS_HOST}/api/2.0/sql/statements/" \
    -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"warehouse_id\": \"${DATABRICKS_WAREHOUSE_ID}\", \"statement\": $(echo "$1" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'), \"wait_timeout\": \"30s\"}" > /dev/null 2>&1
}

run_sql "GRANT USE CATALOG ON CATALOG saas_workshop TO \`${SP_NAME}\`"
run_sql "GRANT USE SCHEMA ON SCHEMA saas_workshop.saas_pilot TO \`${SP_NAME}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.orders TO \`${SP_NAME}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.customer_data TO \`${SP_NAME}\`"
run_sql "GRANT SELECT ON TABLE saas_workshop.saas_pilot.tenant_user_map TO \`${SP_NAME}\`"
run_sql "GRANT EXECUTE ON FUNCTION saas_workshop.saas_pilot.tenant_filter TO \`${SP_NAME}\`"
echo "  ✓ USE CATALOG, USE SCHEMA, SELECT, EXECUTE"
echo ""

# Step 3: Add to tenant_user_map
echo "→ [3/5] Adding to tenant_user_map..."
run_sql "INSERT INTO saas_workshop.saas_pilot.tenant_user_map VALUES ('${APP_ID}', '${TENANT_ID}')"
echo "  ✓ ${APP_ID} → ${TENANT_ID}"
echo ""

# Step 4: Create federation policy (if account token available)
echo "→ [4/5] Creating federation policies..."
if [[ -n "${DATABRICKS_ACCOUNT_TOKEN:-}" ]]; then
  ACCOUNTS_HOST="https://accounts.cloud.databricks.com"

  # Get issuer URL
  ISSUER_URL=$(aws sts get-web-identity-token --audience databricks \
    --query 'Issuer' --output text --no-cli-pager 2>/dev/null || echo "")

  for ROLE_NAME in "$QUERY_ROLE_NAME" "$INTERCEPTOR_ROLE_NAME"; do
    curl -s -X POST \
      "${ACCOUNTS_HOST}/api/2.0/accounts/${DATABRICKS_ACCOUNT_ID}/servicePrincipals/${SP_ID}/credentials/federation-policies" \
      -H "Authorization: Bearer ${DATABRICKS_ACCOUNT_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{
        \"name\": \"aws-wif-${ROLE_NAME}\",
        \"oidc_policy\": {
          \"issuer\": \"${ISSUER_URL}\",
          \"audiences\": [\"databricks\"],
          \"subject\": \"arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}\"
        }
      }" > /dev/null 2>&1
    echo "  ✓ ${ROLE_NAME} → ${SP_NAME}"
  done
else
  echo "  ⚠ DATABRICKS_ACCOUNT_TOKEN not set — create federation policies manually"
fi
echo ""

# Step 5: Insert into DynamoDB
echo "→ [5/5] Inserting WIF config into DynamoDB..."
aws dynamodb put-item \
  --table-name "$WIF_TABLE_NAME" \
  --item "{
    \"tenant_id\": {\"S\": \"${TENANT_ID}\"},
    \"client_id\": {\"S\": \"${APP_ID}\"},
    \"sp_name\": {\"S\": \"${SP_NAME}\"},
    \"created_at\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}
  }" \
  --region "$REGION" --no-cli-pager
echo "  ✓ ${TENANT_ID} → client_id=${APP_ID:0:8}..."
echo ""

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ Tenant Onboarded: ${TENANT_ID}"
echo "├──────────────────────────────────────────────────────────────┤"
echo "│                                                              │"
echo "│  SP:        ${SP_NAME} (applicationId: ${APP_ID:0:12}...)    │"
echo "│  DynamoDB:  ${WIF_TABLE_NAME}                                │"
echo "│  Row Filter: current_user() → tenant_user_map → isolation    │"
echo "│                                                              │"
echo "│  AWS Lambda redeployment needed: NONE                        │"
echo "│  AWS config change needed: NONE                              │"
echo "│                                                              │"
echo "│  Lambda reads DynamoDB at runtime — immediate activation.    │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
echo "  ✅ Tenant ${TENANT_ID} is ready."
echo "     Create a Cognito user with custom:tenant_id='${TENANT_ID}' to test."
