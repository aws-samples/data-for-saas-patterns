#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 04_test_mcp_target.sh — Test the MCP Server target path end-to-end.
#
# Verifies: Gateway → Interceptor (WIF + Authorization injection) → Databricks UC Functions MCP (Row Filter)

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="MultiTenantAgentDatabricks"
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
echo "  Target:   databricks-mcp (DYNAMIC listing mode)"
echo "  Endpoint: Databricks UC Functions MCP Server"
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

# Discover available MCP tools (paginate through all pages)
echo "→ Discovering MCP Server tools (DYNAMIC mode)..."
ALL_TOOLS=$(python3 -c "
import subprocess, json, sys

gateway_url = '$GATEWAY_URL'
token = '$TOKEN_1'
all_tools = []
cursor = None

for _ in range(5):  # max 5 pages
    payload = {'jsonrpc': '2.0', 'method': 'tools/list', 'id': 1}
    if cursor:
        payload['params'] = {'cursor': cursor}
    
    result = subprocess.run(
        ['curl', '-s', '--max-time', '15', '-X', 'POST', gateway_url,
         '-H', f'Authorization: Bearer {token}',
         '-H', 'Content-Type: application/json',
         '-H', 'Mcp-Protocol-Version: 2025-11-25',
         '-d', json.dumps(payload)],
        capture_output=True, text=True
    )
    
    if not result.stdout.strip():
        break
    
    try:
        r = json.loads(result.stdout)
    except json.JSONDecodeError:
        break
    
    tools = r.get('result', {}).get('tools', [])
    all_tools.extend(tools)
    cursor = r.get('result', {}).get('nextCursor')
    if not cursor:
        break

mcp_tools = [t for t in all_tools if 'databricks-mcp' in t['name']]
print(f'  Total tools: {len(all_tools)}')
print(f'  MCP Server tools: {len(mcp_tools)}')
for t in mcp_tools:
    print(f'    • {t[\"name\"]}')

# Output the tool name for use below
if mcp_tools:
    print(f'TOOL_NAME={mcp_tools[0][\"name\"]}')
else:
    print('TOOL_NAME=databricks-mcp___saas_workshop__saas_pilot__get_orders')
")
echo "$ALL_TOOLS"
echo ""

# Extract tool name
MCP_TOOL_NAME=$(echo "$ALL_TOOLS" | grep "^TOOL_NAME=" | cut -d= -f2)
echo "  Using tool: ${MCP_TOOL_NAME}"
echo ""

# Query as tenant-001
echo "→ Querying as tenant-001 via MCP Server..."
RESULT_1=$(curl -s --max-time 45 -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $TOKEN_1" \
  -H "Content-Type: application/json" \
  -H "Mcp-Protocol-Version: 2025-11-25" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"tools/call\",\"params\":{\"name\":\"${MCP_TOOL_NAME}\",\"arguments\":{}},\"id\":1}")
echo "$RESULT_1" | python3 -m json.tool
echo ""

# Query as tenant-002
echo "→ Querying as tenant-002 via MCP Server (same call)..."
RESULT_2=$(curl -s --max-time 45 -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer $TOKEN_2" \
  -H "Content-Type: application/json" \
  -H "Mcp-Protocol-Version: 2025-11-25" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"tools/call\",\"params\":{\"name\":\"${MCP_TOOL_NAME}\",\"arguments\":{}},\"id\":2}")
echo "$RESULT_2" | python3 -m json.tool
echo ""

# Verify
COUNT_1=$(echo "$RESULT_1" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
    content = r.get('result', {}).get('content', [])
    if content and not r.get('result', {}).get('isError'):
        text = content[0].get('text', '')
        try:
            data = json.loads(text)
            rows = data.get('value', data.get('results', data.get('rows', [])))
            if isinstance(rows, list):
                print(len(rows))
            else:
                print('?')
        except json.JSONDecodeError:
            # Count data rows in text
            lines = [l for l in text.strip().split('\n') if 'tenant-001' in l or 'ord-' in l]
            print(len(lines) if lines else '?')
    else:
        print('0')
except:
    print('?')
" 2>/dev/null || echo "?")

COUNT_2=$(echo "$RESULT_2" | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
    content = r.get('result', {}).get('content', [])
    if content and not r.get('result', {}).get('isError'):
        text = content[0].get('text', '')
        try:
            data = json.loads(text)
            rows = data.get('value', data.get('results', data.get('rows', [])))
            if isinstance(rows, list):
                print(len(rows))
            else:
                print('?')
        except json.JSONDecodeError:
            lines = [l for l in text.strip().split('\n') if 'tenant-002' in l or 'ord-' in l]
            print(len(lines) if lines else '?')
    else:
        print('0')
except:
    print('?')
" 2>/dev/null || echo "?")

echo "─────────────────────────────────────────────────────────────"
echo "  tenant-001: ${COUNT_1} rows (expected: 3)"
echo "  tenant-002: ${COUNT_2} rows (expected: 2)"
echo ""
if [[ "$COUNT_1" == "3" && "$COUNT_2" == "2" ]]; then
  echo "  ✅ PASS — Same function call, different data. MCP Server target row filter isolation confirmed."
  echo "     Zero custom code in the query path. Interceptor handled WIF exchange."
else
  echo "  ❌ FAIL — Expected 3 and 2 rows."
  echo "     Review the raw responses above to verify isolation."
  exit 1
fi
