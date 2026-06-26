#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# test_mcp_target.sh — Test the MCP Server target path end-to-end.
#
# Verifies: Gateway → Interceptor (WIF + Authorization injection) → BigQuery MCP Server (RLS)

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
echo "║  Test: MCP Server Target — Multi-Tenant Isolation            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Gateway:  ${GATEWAY_URL}"
echo "  Target:   bigquery-mcp___execute_sql"
echo "  Endpoint: https://bigquery.googleapis.com/mcp"
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

# Discover available MCP tools
echo "→ Discovering MCP Server tools..."
TOOLS=$(curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $TOKEN_1" \
  -H "Content-Type: application/json" \
  -H "Mcp-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":0}')

echo "$TOOLS" | python3 -c "
import sys, json
r = json.load(sys.stdin)
tools = r.get('result', {}).get('tools', [])
mcp_tools = [t for t in tools if 'bigquery-mcp' in t['name']]
print(f'  Found {len(mcp_tools)} MCP Server tool(s):')
for t in mcp_tools:
    print(f'    • {t[\"name\"]}')
" 2>/dev/null
echo ""

# Query as tenant-001
echo "→ Querying as tenant-001 via MCP Server..."
echo "  SQL: SELECT * FROM saas_pilot.orders"
RESULT_1=$(curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $TOKEN_1" \
  -H "Content-Type: application/json" \
  -H "Mcp-Protocol-Version: 2025-11-25" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"tools/call\",\"params\":{\"name\":\"bigquery-mcp___execute_sql\",\"arguments\":{\"projectId\":\"${GCP_PROJECT_ID}\",\"query\":\"SELECT * FROM saas_pilot.orders\"}},\"id\":1}")
echo "$RESULT_1" | python3 -m json.tool
echo ""

# Query as tenant-002
echo "→ Querying as tenant-002 via MCP Server (same SQL)..."
RESULT_2=$(curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $TOKEN_2" \
  -H "Content-Type: application/json" \
  -H "Mcp-Protocol-Version: 2025-11-25" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"tools/call\",\"params\":{\"name\":\"bigquery-mcp___execute_sql\",\"arguments\":{\"projectId\":\"${GCP_PROJECT_ID}\",\"query\":\"SELECT * FROM saas_pilot.orders\"}},\"id\":2}")
echo "$RESULT_2" | python3 -m json.tool
echo ""

# Verify
COUNT_1=$(echo "$RESULT_1" | python3 -c "import sys,json; r=json.load(sys.stdin); rows=r.get('result',{}).get('structuredContent',{}).get('rows',[]); print(len(rows))" 2>/dev/null || echo "?")
COUNT_2=$(echo "$RESULT_2" | python3 -c "import sys,json; r=json.load(sys.stdin); rows=r.get('result',{}).get('structuredContent',{}).get('rows',[]); print(len(rows))" 2>/dev/null || echo "?")

echo "─────────────────────────────────────────────────────────────"
echo "  tenant-001: ${COUNT_1} rows (expected: 3)"
echo "  tenant-002: ${COUNT_2} rows (expected: 2)"
echo ""
if [[ "$COUNT_1" == "3" && "$COUNT_2" == "2" ]]; then
  echo "  ✅ PASS — Same SQL, different data. MCP Server target RLS isolation confirmed."
  echo "     Zero custom code in the query path. Interceptor handled WIF exchange."
else
  echo "  ❌ FAIL — Expected 3 and 2 rows."
  exit 1
fi
