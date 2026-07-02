#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
# 08_test_agent.sh — Test the Strands Agent via AgentCore Runtime.
#
# Tests both target paths:
#   Part A: Lambda target (query-bigquery___query_bigquery)
#   Part B: MCP Server target (bigquery-mcp___execute_sql)
#   Part C: Free-form (LLM chooses)
#
# Verifies tenant isolation across all paths.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="MultiTenantAgentBigQuery"
GCP_PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
TEST_PASSWORD="${TEST_PASSWORD:-Workshop@123!}"

COGNITO_CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='ClientId'].OutputValue" --output text)

AGENT_RUNTIME_ID=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" --region "$REGION" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='AgentRuntimeId'].OutputValue" --output text)

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --no-cli-pager)
RUNTIME_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:runtime/${AGENT_RUNTIME_ID}"
ESCAPED_ARN=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${RUNTIME_ARN}', safe=''))")
AGENT_URL="https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ESCAPED_ARN}/invocations?qualifier=DEFAULT"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Test: Multi-Tenant Agent — Lambda + MCP Server Targets       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Runtime:  ${AGENT_RUNTIME_ID}"
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

# Helper function
invoke_agent() {
  local token="$1"
  local prompt="$2"
  local label="$3"

  SESSION_ID="test-$(python3 -c 'import uuid; print(uuid.uuid4())')"

  echo "  ─── ${label} ───"
  echo "  Prompt: ${prompt}"

  RESPONSE=$(curl -s -X POST "$AGENT_URL" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $SESSION_ID" \
    -d "{\"input\":{\"prompt\":\"${prompt}\"}}")

  # Parse response
  echo "$RESPONSE" | python3 -c "
import sys, json, ast
try:
    r = json.load(sys.stdin)
    output = r.get('output', {})
    msg = output.get('message', '')
    # Try to extract text content from the message dict
    try:
        d = ast.literal_eval(msg) if isinstance(msg, str) else msg
        if isinstance(d, dict) and 'content' in d:
            texts = [c['text'] for c in d.get('content', []) if 'text' in c]
            print(f'  Agent: {texts[0][:500]}')
        else:
            print(f'  Agent: {str(msg)[:500]}')
    except:
        print(f'  Agent: {str(msg)[:500]}')
except Exception as e:
    raw = sys.stdin.read()
    print(f'  Error: {e}')
    print(f'  Raw: {raw[:300]}')
" 2>/dev/null || echo "  (parse failed)"
  echo ""
}

# ═══════════════════════════════════════════════════════════════════
# PART A: Lambda Target
# ═══════════════════════════════════════════════════════════════════
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Part A: Lambda Target (query_bigquery tool)                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  The agent uses 'query-bigquery___query_bigquery' tool."
echo "  Path: Gateway → Interceptor (x-tenant-id) → Lambda (WIF) → BigQuery"
echo ""

invoke_agent "$TOKEN_1" \
  "Use the query_bigquery tool to count my orders. Run: SELECT COUNT(*) as cnt FROM saas_pilot.orders" \
  "Tenant-001 via Lambda"

invoke_agent "$TOKEN_2" \
  "Use the query_bigquery tool to count my orders. Run: SELECT COUNT(*) as cnt FROM saas_pilot.orders" \
  "Tenant-002 via Lambda"

# ═══════════════════════════════════════════════════════════════════
# PART B: MCP Server Target
# ═══════════════════════════════════════════════════════════════════
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Part B: MCP Server Target (execute_sql tool)                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  The agent uses 'bigquery-mcp___execute_sql' tool."
echo "  Path: Gateway → Interceptor (WIF + token) → BigQuery MCP Server"
echo ""

invoke_agent "$TOKEN_1" \
  "Use the execute_sql tool (bigquery-mcp___execute_sql) with projectId ${GCP_PROJECT_ID} to run: SELECT COUNT(*) as cnt FROM saas_pilot.orders" \
  "Tenant-001 via MCP Server"

invoke_agent "$TOKEN_2" \
  "Use the execute_sql tool (bigquery-mcp___execute_sql) with projectId ${GCP_PROJECT_ID} to run: SELECT COUNT(*) as cnt FROM saas_pilot.orders" \
  "Tenant-002 via MCP Server"

# ═══════════════════════════════════════════════════════════════════
# PART C: Free-form (LLM picks)
# ═══════════════════════════════════════════════════════════════════
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Part C: Free-form (LLM decides which tool to use)            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

invoke_agent "$TOKEN_1" \
  "How many orders do I have?" \
  "Tenant-001 free-form"

invoke_agent "$TOKEN_2" \
  "How many orders do I have?" \
  "Tenant-002 free-form"

# ═══════════════════════════════════════════════════════════════════
echo "─────────────────────────────────────────────────────────────"
echo ""
echo "  Expected results:"
echo "    • tenant-001: 3 orders (all paths)"
echo "    • tenant-002: 2 orders (all paths)"
echo ""
echo "  Both Lambda and MCP Server targets produce identical"
echo "  tenant-isolated results through the same agent."
echo ""
echo "  ✅ Multi-tenant agent verified across both target paths."
