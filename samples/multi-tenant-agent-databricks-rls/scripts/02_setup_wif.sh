#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 02_setup_wif.sh — Configure WIF + populate DynamoDB with per-tenant client_ids
#
# Creates the trust relationship between AWS IAM and Databricks:
#   1. Verifies AWS IAM Outbound Identity Federation is enabled
#   2. Grants sts:GetWebIdentityToken to Lambda + Interceptor roles
#   3. Creates federation policies on each SP (Databricks Account API)
#   4. Populates DynamoDB with per-tenant client_id mappings
#
# Prerequisites:
#   - 01_setup_databricks.sh completed (.lab_env has SP IDs)
#   - CDK stack deployed (roles + DynamoDB table exist)
#   - export DATABRICKS_TOKEN=dapi...
#   - export DATABRICKS_ACCOUNT_ID=<account-id>
#   - export DATABRICKS_ACCOUNT_TOKEN=<account-level-token> (optional)
#
# Usage:
#   bash scripts/02_setup_wif.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/.lab_env" 2>/dev/null || true

# Validate required variables
MISSING=""
[ -z "${DATABRICKS_HOST:-}" ] && MISSING="${MISSING}  - DATABRICKS_HOST\n"
[ -z "${DATABRICKS_TOKEN:-}" ] && MISSING="${MISSING}  - DATABRICKS_TOKEN\n"
[ -z "${SP1_APP_ID:-}" ] && MISSING="${MISSING}  - SP1_APP_ID (run 01_setup_databricks.sh first)\n"
[ -z "${SP2_APP_ID:-}" ] && MISSING="${MISSING}  - SP2_APP_ID (run 01_setup_databricks.sh first)\n"
[ -z "${DATABRICKS_ACCOUNT_ID:-}" ] && MISSING="${MISSING}  - DATABRICKS_ACCOUNT_ID\n"
if [ -n "$MISSING" ]; then
  echo ""
  echo "  ✗ Missing required environment variables:"
  echo -e "$MISSING"
  echo "  Set them and re-run: bash scripts/02_setup_wif.sh"
  exit 1
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="MultiTenantAgentDatabricks"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --no-cli-pager)

QUERY_ROLE_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$AWS_REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='QueryRoleName'].OutputValue" --output text 2>/dev/null || echo "multi-tenant-agent-databricks-query-role")

INTERCEPTOR_ROLE_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$AWS_REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='InterceptorRoleName'].OutputValue" --output text 2>/dev/null || echo "multi-tenant-agent-databricks-interceptor-role")

WIF_TABLE_NAME=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$AWS_REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='WifConfigTableName'].OutputValue" --output text 2>/dev/null || echo "multi-tenant-databricks-wif-config")

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Configure WIF + Populate DynamoDB                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  AWS Account:      ${AWS_ACCOUNT_ID}"
echo "  Query Role:       ${QUERY_ROLE_NAME}"
echo "  Interceptor Role: ${INTERCEPTOR_ROLE_NAME}"
echo "  DynamoDB Table:   ${WIF_TABLE_NAME}"
echo "  SP tenant-001:    ${SP1_APP_ID}"
echo "  SP tenant-002:    ${SP2_APP_ID}"
echo ""

# ─── Step 1: Verify AWS IAM Outbound Identity Federation ──────────
echo "→ [1/4] Verifying AWS IAM Outbound Identity Federation..."

WIF_INFO=$(aws iam get-outbound-web-identity-federation-info --no-cli-pager 2>&1) || {
  echo "  AWS IAM Outbound Identity Federation is not enabled. Enabling now..."
  aws iam enable-outbound-web-identity-federation --no-cli-pager 2>/dev/null || {
    echo "  ✗ Failed to enable. Enable it manually:"
    echo "      aws iam enable-outbound-web-identity-federation"
    exit 1
  }
  echo "  ✓ Enabled. Waiting 5s for propagation..."
  sleep 5
  WIF_INFO=$(aws iam get-outbound-web-identity-federation-info --no-cli-pager 2>&1) || {
    echo "  ✗ Still not available after enabling. Wait a minute and re-run."
    exit 1
  }
}

ISSUER_URL=$(echo "$WIF_INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['IssuerIdentifier'])")
echo "  ✓ Issuer URL: ${ISSUER_URL}"
echo ""

# ─── Step 2: Grant sts:GetWebIdentityToken to roles ───────────────
echo "→ [2/4] Granting sts:GetWebIdentityToken to Lambda + Interceptor roles..."

for ROLE_NAME in "$QUERY_ROLE_NAME" "$INTERCEPTOR_ROLE_NAME"; do
  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name WifTokenExchange \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Action\": [\"sts:GetWebIdentityToken\"],
        \"Resource\": \"*\",
        \"Condition\": {
          \"ForAllValues:StringEquals\": {
            \"sts:IdentityTokenAudience\": \"databricks\"
          },
          \"NumericLessThanEquals\": {
            \"sts:DurationSeconds\": 300
          }
        }
      }]
    }" --no-cli-pager 2>/dev/null \
    && echo "  ✓ ${ROLE_NAME}: sts:GetWebIdentityToken granted" \
    || echo "  ⚠ ${ROLE_NAME}: failed to grant (may not exist yet)"
done
echo ""

# ─── Step 3: Create federation policies ───────────────────────────
echo "→ [3/4] Creating federation policies on Databricks service principals..."

# Get SP internal IDs
SP1_INTERNAL_ID=$(curl -s "${DATABRICKS_HOST}/api/2.0/preview/scim/v2/ServicePrincipals?filter=applicationId+eq+%22${SP1_APP_ID}%22" \
  -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
  | python3 -c "import sys,json; r=json.load(sys.stdin).get('Resources',[]); print(r[0]['id'] if r else '')" 2>/dev/null)

SP2_INTERNAL_ID=$(curl -s "${DATABRICKS_HOST}/api/2.0/preview/scim/v2/ServicePrincipals?filter=applicationId+eq+%22${SP2_APP_ID}%22" \
  -H "Authorization: Bearer ${DATABRICKS_TOKEN}" \
  | python3 -c "import sys,json; r=json.load(sys.stdin).get('Resources',[]); print(r[0]['id'] if r else '')" 2>/dev/null)

echo "  Trust parameters:"
echo "    Issuer:   ${ISSUER_URL}"
echo "    Subject1: arn:aws:iam::${AWS_ACCOUNT_ID}:role/${QUERY_ROLE_NAME}"
echo "    Subject2: arn:aws:iam::${AWS_ACCOUNT_ID}:role/${INTERCEPTOR_ROLE_NAME}"
echo "    Audience: databricks"
echo ""

ACCOUNT_TOKEN="${DATABRICKS_ACCOUNT_TOKEN:-}"

if [ -z "$ACCOUNT_TOKEN" ]; then
  echo "  ⚠ DATABRICKS_ACCOUNT_TOKEN not set — skipping automated policy creation."
  echo ""
  echo "  Create federation policies manually via:"
  echo "    https://accounts.cloud.databricks.com → User management → Service principals"
  echo "    For each SP, create 2 policies (one per IAM role):"
  echo "      Issuer:   ${ISSUER_URL}"
  echo "      Subject:  arn:aws:iam::${AWS_ACCOUNT_ID}:role/${QUERY_ROLE_NAME}"
  echo "      Audience: databricks"
  echo "    (Repeat with Subject = .../${INTERCEPTOR_ROLE_NAME})"
  echo ""
  echo "  Press Enter after creating policies to continue..."
  read -r
else
  for SP_INTERNAL_ID in "$SP1_INTERNAL_ID" "$SP2_INTERNAL_ID"; do
    SP_LABEL="sp-tenant-001"
    [ "$SP_INTERNAL_ID" = "$SP2_INTERNAL_ID" ] && SP_LABEL="sp-tenant-002"

    for SUBJECT in "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${QUERY_ROLE_NAME}" "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${INTERCEPTOR_ROLE_NAME}"; do
      ROLE_LABEL=$(echo "$SUBJECT" | grep -o '[^/]*$')
      RESULT=$(curl -s -X POST \
        "https://accounts.cloud.databricks.com/api/2.0/accounts/${DATABRICKS_ACCOUNT_ID}/servicePrincipals/${SP_INTERNAL_ID}/federationPolicies" \
        -H "Authorization: Bearer ${ACCOUNT_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"oidc_policy\":{\"issuer\":\"${ISSUER_URL}\",\"audiences\":[\"databricks\"],\"subject\":\"${SUBJECT}\"}}")

      if echo "$RESULT" | python3 -c "import sys,json; r=json.load(sys.stdin); sys.exit(0 if 'oidc_policy' in r or 'ALREADY_EXISTS' in str(r) or 'already exists' in str(r).lower() else 1)" 2>/dev/null; then
        echo "    ✓ ${SP_LABEL} + ${ROLE_LABEL}: policy created"
      else
        echo "    ⚠ ${SP_LABEL} + ${ROLE_LABEL}: $(echo $RESULT | python3 -c "import sys,json; print(json.load(sys.stdin).get('message','unknown error')[:80])" 2>/dev/null || echo 'failed')"
      fi
    done
  done
fi
echo ""

# ─── Step 4: Populate DynamoDB ────────────────────────────────────
echo "→ [4/4] Populating DynamoDB with per-tenant WIF config..."

aws dynamodb put-item \
  --table-name "$WIF_TABLE_NAME" \
  --item "{\"tenant_id\":{\"S\":\"tenant-001\"},\"client_id\":{\"S\":\"${SP1_APP_ID}\"},\"sp_name\":{\"S\":\"sp-tenant-001\"},\"created_at\":{\"S\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}" \
  --region "$AWS_REGION" --no-cli-pager \
  && echo "  ✓ tenant-001 → client_id=${SP1_APP_ID}" \
  || echo "  ✗ Failed to write tenant-001"

aws dynamodb put-item \
  --table-name "$WIF_TABLE_NAME" \
  --item "{\"tenant_id\":{\"S\":\"tenant-002\"},\"client_id\":{\"S\":\"${SP2_APP_ID}\"},\"sp_name\":{\"S\":\"sp-tenant-002\"},\"created_at\":{\"S\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}" \
  --region "$AWS_REGION" --no-cli-pager \
  && echo "  ✓ tenant-002 → client_id=${SP2_APP_ID}" \
  || echo "  ✗ Failed to write tenant-002"

echo ""

# Update .lab_env
cat >> "${SCRIPT_DIR}/.lab_env" <<EOF
export WIF_ISSUER_URL="${ISSUER_URL}"
export QUERY_ROLE_NAME="${QUERY_ROLE_NAME}"
export INTERCEPTOR_ROLE_NAME="${INTERCEPTOR_ROLE_NAME}"
export WIF_TABLE_NAME="${WIF_TABLE_NAME}"
EOF

echo "┌──────────────────────────────────────────────────────────────┐"
echo "│ WIF + DynamoDB Setup Complete                                 │"
echo "├──────────────────────────────────────────────────────────────┤"
echo "│                                                              │"
echo "│  Trust chain:                                                │"
echo "│    Lambda/Interceptor Role                                   │"
echo "│      → sts.get_web_identity_token(audience=databricks)       │"
echo "│      → AWS-signed OIDC token                                 │"
echo "│      → POST /oidc/v1/token (RFC 8693 token-exchange)         │"
echo "│      → Databricks validates federation policy on SP          │"
echo "│      → Returns access token (current_user = SP client_id)    │"
echo "│      → Row filter enforces tenant isolation                  │"
echo "│                                                              │"
echo "│  DynamoDB table: ${WIF_TABLE_NAME}                           │"
echo "│    tenant-001 → ${SP1_APP_ID}                                │"
echo "│    tenant-002 → ${SP2_APP_ID}                                │"
echo "│                                                              │"
echo "│  Zero secrets stored. IAM role is the credential.            │"
echo "└──────────────────────────────────────────────────────────────┘"
echo ""
echo "  ✅ Next: bash scripts/03_test_lambda_target.sh"
