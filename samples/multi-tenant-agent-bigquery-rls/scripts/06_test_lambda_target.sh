#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# test_lambda_target.sh — Test the Lambda target path end-to-end.
#
# Verifies: Gateway → Interceptor (x-tenant-id) → Lambda (WIF) → BigQuery (RLS)

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="MultiTenantAgentBigQuery"
GCP_PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
TEST_PASSWORD="${TEST_PASSWORD:-Workshop@123!}"

COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='ClientId'].OutputValue" --output text)

GATEWAY_URL=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue" --output text)

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Test: Lambda Target — Multi-Tenant Isolation                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Gateway: ${GATEWAY_URL}"
echo "  Target:  query-bigquery___query_bigquery"
echo ""

# Get tokens
echo "→ Fetching tenant tokens..."
TOKEN_1=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id "$COGNITO_CLIENT_ID" \
  --auth-parameters "USERNAME=testuser@example.com,PASSWORD=${TEST_PASSWORD}" \
  --query 'AuthenticationResult.IdToken' --output text --no-cli-pager --region "$REGION")
echo "  ✓ tenant-001"

TOKEN_2=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id "$COGNITO_CLIENT_ID" \
  --auth-parameters "USERNAME=testuser2@example.com,PASSWORD=${TEST_PASSWORD}" \
  --query 'AuthenticationResult.IdToken' --output text --no-cli-pager --region "$REGION")
echo "  ✓ tenant-002"
echo ""

# Query as tenant-001
echo "→ Querying as tenant-001..."
echo "  SQL: SELECT * FROM saas_pilot.orders"
RESULT_1=$(curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $TOKEN_1" \
  -H "Content-Type: application/json" \
  -H "Mcp-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"query-bigquery___query_bigquery","arguments":{"sql":"SELECT * FROM saas_pilot.orders"}},"id":1}')
echo "$RESULT_1" | python3 -m json.tool
echo ""

# Query as tenant-002
echo "→ Querying as tenant-002 (same SQL)..."
RESULT_2=$(curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $TOKEN_2" \
  -H "Content-Type: application/json" \
  -H "Mcp-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"query-bigquery___query_bigquery","arguments":{"sql":"SELECT * FROM saas_pilot.orders"}},"id":2}')
echo "$RESULT_2" | python3 -m json.tool
echo ""

# Verify
COUNT_1=$(echo "$RESULT_1" | python3 -c "import sys,json; r=json.load(sys.stdin); text=r['result']['content'][0]['text']; outer=json.loads(text); body=json.loads(outer['body']); print(body['row_count'])" 2>/dev/null || echo "?")
COUNT_2=$(echo "$RESULT_2" | python3 -c "import sys,json; r=json.load(sys.stdin); text=r['result']['content'][0]['text']; outer=json.loads(text); body=json.loads(outer['body']); print(body['row_count'])" 2>/dev/null || echo "?")

echo "─────────────────────────────────────────────────────────────"
echo "  tenant-001: ${COUNT_1} rows (expected: 3)"
echo "  tenant-002: ${COUNT_2} rows (expected: 2)"
echo ""
if [[ "$COUNT_1" == "3" && "$COUNT_2" == "2" ]]; then
  echo "  ✅ PASS — Same SQL, different data. Lambda target RLS isolation confirmed."
else
  echo "  ❌ FAIL — Expected 3 and 2 rows."
  exit 1
fi
