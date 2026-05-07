#!/usr/bin/env bash
# test_examples.sh -- End-to-end tests for single-tenant and multi-tenant examples.
#
# Runs the full setup (TVM role, Cognito, indexes, sessions) then exercises
# the HTTP API locally or against a deployed AgentCore Runtime.
#
# Usage -- local (starts the server automatically):
#   bash scripts/test_examples.sh
#   bash scripts/test_examples.sh --mode single
#   bash scripts/test_examples.sh --mode multi
#
# Usage -- deploy to AgentCore Runtime then test:
#   bash scripts/test_examples.sh --agentcore --mode multi
#   bash scripts/test_examples.sh --agentcore --mode single
#
# Usage -- test against an already-deployed AgentCore Runtime (skip deploy):
#   AGENT_ARN=arn:aws:bedrock-agentcore:... \
#     bash scripts/test_examples.sh --agentcore --mode multi
#
# Required env vars:
#   S3_VECTOR_BUCKET_NAME
#   AWS_REGION              (default: us-east-1)
#
# The script:
#   1. Runs setup_tvm_role.sh  -- creates/updates the TVM IAM role
#   2. Runs setup_cognito.sh   -- creates Cognito pool + two tenant test users
#   3. Obtains real JWTs via Cognito USER_PASSWORD_AUTH flow
#   4. Deletes and recreates vector indexes (clean state)
#   5a. Local:      starts the server, runs tests, stops the server
#   5b. AgentCore:  deploys the example (unless AGENT_ARN already set), runs tests

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MODE="both"           # single | multi | both
AGENTCORE_MODE=false  # true = deploy/test against AgentCore Runtime
AGENT_ARN="${AGENT_ARN:-}"   # skip deploy if already set
PORT=8080
SESSION_HDR="X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case $1 in
    --mode)       MODE="$2";      shift 2 ;;
    --agentcore)  AGENTCORE_MODE=true; shift ;;
    --agent-arn)  AGENT_ARN="$2"; shift 2 ;;
    --port)       PORT="$2";      shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

LOCAL_MODE=true
$AGENTCORE_MODE && LOCAL_MODE=false
BASE_URL="http://localhost:$PORT"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass()  { echo -e "${GREEN}  ✓ $1${NC}"; }
fail()  { echo -e "${RED}  ✗ $1${NC}"; FAILURES=$((FAILURES+1)); }
info()  { echo -e "${YELLOW}  → $1${NC}"; }
FAILURES=0

# ---------------------------------------------------------------------------
# Validate required env vars
# ---------------------------------------------------------------------------
: "${S3_VECTOR_BUCKET_NAME:?Set S3_VECTOR_BUCKET_NAME}"

# ---------------------------------------------------------------------------
# Step 1 -- TVM role (multi-tenant only)
# ---------------------------------------------------------------------------
setup_tvm() {
  if [[ "$MODE" == "multi" || "$MODE" == "both" ]]; then
    info "Running setup_tvm_role.sh ..."
    TVM_OUTPUT=$(bash "$SCRIPT_DIR/setup_tvm_role.sh" "$S3_VECTOR_BUCKET_NAME" 2>&1)
    echo "$TVM_OUTPUT" | grep -E "^(->|✓)" || true
    export S3_VECTOR_TVM_ROLE_ARN
    S3_VECTOR_TVM_ROLE_ARN=$(echo "$TVM_OUTPUT" | grep "S3_VECTOR_TVM_ROLE_ARN=" | sed 's/.*S3_VECTOR_TVM_ROLE_ARN=//')
    pass "TVM role ready: $S3_VECTOR_TVM_ROLE_ARN"
  fi
}

# ---------------------------------------------------------------------------
# Step 2 -- Cognito setup + JWT acquisition (multi-tenant only)
# ---------------------------------------------------------------------------
JWT1=""
JWT2=""
COGNITO_USER_POOL_ID=""
COGNITO_CLIENT_ID=""

setup_cognito_and_get_jwts() {
  if [[ "$MODE" != "multi" && "$MODE" != "both" ]]; then return; fi

  info "Running setup_cognito.sh ..."
  COGNITO_OUTPUT=$(bash "$SCRIPT_DIR/setup_cognito.sh" 2>&1)
  echo "$COGNITO_OUTPUT" | grep -E "^(->|✓)" || true

  COGNITO_USER_POOL_ID=$(echo "$COGNITO_OUTPUT" | grep "COGNITO_USER_POOL_ID=" | sed 's/.*COGNITO_USER_POOL_ID=//')
  COGNITO_CLIENT_ID=$(echo "$COGNITO_OUTPUT"    | grep "COGNITO_CLIENT_ID="    | sed 's/.*COGNITO_CLIENT_ID=//')
  USER_A=$(echo "$COGNITO_OUTPUT"               | grep "COGNITO_USER_A_USERNAME=" | sed 's/.*COGNITO_USER_A_USERNAME=//')
  USER_B=$(echo "$COGNITO_OUTPUT"               | grep "COGNITO_USER_B_USERNAME=" | sed 's/.*COGNITO_USER_B_USERNAME=//')
  PASSWORD=$(echo "$COGNITO_OUTPUT"             | grep "COGNITO_USER_PASSWORD=" | sed 's/.*COGNITO_USER_PASSWORD=//')

  pass "Cognito pool: $COGNITO_USER_POOL_ID  client: $COGNITO_CLIENT_ID"

  info "Obtaining JWT for tenant-001 ($USER_A) ..."
  JWT1=$(aws cognito-idp initiate-auth \
    --auth-flow USER_PASSWORD_AUTH \
    --client-id "$COGNITO_CLIENT_ID" \
    --auth-parameters "USERNAME=$USER_A,PASSWORD=$PASSWORD" \
    --region "$REGION" \
    --query "AuthenticationResult.IdToken" \
    --output text)
  pass "JWT obtained for tenant-001"

  info "Obtaining JWT for tenant-002 ($USER_B) ..."
  JWT2=$(aws cognito-idp initiate-auth \
    --auth-flow USER_PASSWORD_AUTH \
    --client-id "$COGNITO_CLIENT_ID" \
    --auth-parameters "USERNAME=$USER_B,PASSWORD=$PASSWORD" \
    --region "$REGION" \
    --query "AuthenticationResult.IdToken" \
    --output text)
  pass "JWT obtained for tenant-002"
}

# ---------------------------------------------------------------------------
# Step 3 -- Deploy to AgentCore Runtime (skipped if AGENT_ARN already set)
# ---------------------------------------------------------------------------
deploy_to_agentcore() {
  local example_file="$1"   # e.g. multi_tenant_agent.py
  local agent_name="$2"     # e.g. strands_s3_vector_memory_agent

  if [[ -n "$AGENT_ARN" ]]; then
    pass "Skipping deploy -- AGENT_ARN already set: $AGENT_ARN"
    return
  fi

  info "Deploying $example_file to AgentCore Runtime via setup_agentcore.sh ..."

  # setup_agentcore.sh requires COGNITO_USER_POOL_ID and COGNITO_CLIENT_ID
  if [[ -z "$COGNITO_USER_POOL_ID" || -z "$COGNITO_CLIENT_ID" ]]; then
    fail "COGNITO_USER_POOL_ID and COGNITO_CLIENT_ID must be set before deploying"
    exit 1
  fi

  DEPLOY_OUTPUT=$(
    S3_VECTOR_BUCKET_NAME="$S3_VECTOR_BUCKET_NAME" \
    S3_VECTOR_TVM_ROLE_ARN="$S3_VECTOR_TVM_ROLE_ARN" \
    COGNITO_USER_POOL_ID="$COGNITO_USER_POOL_ID" \
    COGNITO_CLIENT_ID="$COGNITO_CLIENT_ID" \
    AWS_REGION="$REGION" \
    EXAMPLE_FILE="$example_file" \
    AGENT_NAME="$agent_name" \
    bash "$SCRIPT_DIR/setup_agentcore.sh" 2>&1
  )
  echo "$DEPLOY_OUTPUT"

  AGENT_ARN=$(echo "$DEPLOY_OUTPUT" | grep "^  export AGENT_ARN=" | sed 's/.*AGENT_ARN=//')
  if [[ -z "$AGENT_ARN" ]]; then
    fail "Deploy failed -- AGENT_ARN not found in output"
    exit 1
  fi
  pass "Deployed: $AGENT_ARN"
}

# ---------------------------------------------------------------------------
# Infrastructure helpers
# ---------------------------------------------------------------------------
reset_indexes() {
  local indexes=("$@")
  info "Resetting vector indexes: ${indexes[*]}"
  for idx in "${indexes[@]}"; do
    aws s3vectors delete-index \
      --vector-bucket-name "$S3_VECTOR_BUCKET_NAME" \
      --index-name "$idx" \
      --region "$REGION" 2>/dev/null || true
    aws s3vectors create-index \
      --vector-bucket-name "$S3_VECTOR_BUCKET_NAME" \
      --index-name "$idx" \
      --data-type float32 \
      --dimension 1024 \
      --distance-metric cosine \
      --metadata-configuration '{"nonFilterableMetadataKeys":["content","stored_at","conversation_id","type"]}' \
      --region "$REGION" > /dev/null
    pass "Index reset: $idx"
  done
}

wait_for_summary() {
  local index_name="$1" max_wait="${2:-5}" interval=1
  info "Waiting for summary vector in index '$index_name' (max ${max_wait}s) ..."
  local elapsed=0
  while [[ $elapsed -lt $max_wait ]]; do
    local count
    count=$(aws s3vectors list-vectors \
      --vector-bucket-name "$S3_VECTOR_BUCKET_NAME" \
      --index-name "$index_name" \
      --region "$REGION" \
      --query 'length(vectors)' \
      --output text 2>/dev/null || echo "0")
    if [[ "$count" -gt 0 ]]; then
      pass "Summary stored in '$index_name' after ~${elapsed}s"
      return 0
    fi
    sleep $interval
    elapsed=$((elapsed + interval))
  done
  info "Timed out after ${max_wait}s -- proceeding anyway"
}

wait_for_server() {
  info "Waiting for server at $BASE_URL/ping ..."
  for i in $(seq 1 20); do
    if curl -sf "$BASE_URL/ping" > /dev/null 2>&1; then
      pass "Server is up"
      return 0
    fi
    sleep 1
  done
  fail "Server did not start within 20s"
  exit 1
}

# ---------------------------------------------------------------------------
# HTTP invocation helpers
# ---------------------------------------------------------------------------

# _invoke_raw: returns the raw response body; used by timed_invoke
_invoke_raw_local() {
  local session_id="$1" payload="$3" auth="${4:-}"
  local tmpfile
  tmpfile=$(mktemp)
  printf '%s' "$payload" > "$tmpfile"
  if [[ -n "$auth" ]]; then
    curl -sf -X POST "$BASE_URL/invocations" \
      -H "Content-Type: application/json" \
      -H "$SESSION_HDR: $session_id" \
      -H "Authorization: Bearer $auth" \
      --data-binary "@$tmpfile"
  else
    curl -sf -X POST "$BASE_URL/invocations" \
      -H "Content-Type: application/json" \
      -H "$SESSION_HDR: $session_id" \
      --data-binary "@$tmpfile"
  fi
  local rc=$?
  rm -f "$tmpfile"
  return $rc
}

_invoke_raw_agentcore() {
  local session_id="$1" user_id="$2" payload="$3" bearer="${4:-}"
  local padded_session="agentcore-runtime-session-${session_id}"
  python3 - <<PYEOF
import json, sys, urllib.parse, requests

agent_arn   = '$AGENT_ARN'
region      = '$REGION'
session_id  = '$padded_session'
bearer      = '$bearer'
payload_str = '''$payload'''

escaped_arn = urllib.parse.quote(agent_arn, safe='')
url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped_arn}/invocations?qualifier=DEFAULT"

headers = {
    "Content-Type": "application/json",
    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
}
if bearer:
    headers["Authorization"] = f"Bearer {bearer}"

resp = requests.post(url, headers=headers, data=payload_str, timeout=120)
resp.raise_for_status()
print(resp.text)
PYEOF
}

# timed_invoke: calls the appropriate backend, logs timing + USER/AGENT messages
invoke() {
  local session_id="$1" user_id="$2" payload="$3" auth="${4:-}"

  # Extract the prompt from the payload for logging
  local prompt
  prompt=$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('prompt',''))" <<< "$payload" 2>/dev/null || echo "")

  local t_start t_end elapsed_ms response
  t_start=$(python3 -c "import time; print(int(time.time()*1000))")

  if $LOCAL_MODE; then
    response=$(_invoke_raw_local "$session_id" "$user_id" "$payload" "$auth")
  else
    response=$(_invoke_raw_agentcore "$session_id" "$user_id" "$payload" "$auth")
  fi

  t_end=$(python3 -c "import time; print(int(time.time()*1000))")
  elapsed_ms=$(( t_end - t_start ))

  # Extract agent response text for logging
  local agent_text
  agent_text=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    r = d.get('response', d)
    print(str(r)[:200])
except Exception:
    print('(unparseable)')
" <<< "$response" 2>/dev/null || echo "(error)")

  echo -e "    \033[0;36mUSER  [${elapsed_ms}ms]\033[0m ${prompt:0:120}" >&2
  echo -e "    \033[0;35mAGENT\033[0m ${agent_text}" >&2

  printf '%s' "$response"
}

# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------
assert_contains() {
  local label="$1" text="$2" pattern="$3"
  if echo "$text" | grep -qiE "$pattern"; then
    pass "$label"
  else
    fail "$label -- expected pattern '$pattern'"
    echo "    Response: $(echo "$text" | head -c 200)"
  fi
}

assert_not_contains() {
  local label="$1" text="$2" pattern="$3"
  if echo "$text" | grep -qiE "$pattern"; then
    fail "$label -- pattern '$pattern' should NOT appear"
    echo "    Response: $(echo "$text" | head -c 200)"
  else
    pass "$label"
  fi
}

# ---------------------------------------------------------------------------
# Single-tenant test suite
# ---------------------------------------------------------------------------
run_single_tenant_tests() {
  echo ""
  echo "  ┌─────────────────────────────────────────────────────┐"
  echo "  │  SINGLE-TENANT TESTS                                │"
  echo "  └─────────────────────────────────────────────────────┘"

  if $AGENTCORE_MODE; then
    deploy_to_agentcore "single_tenant_agent.py" "strands_s3_vector_memory_single"
  fi

  reset_indexes "memory"

  SERVER_PID=""
  if $LOCAL_MODE; then
    lsof -ti tcp:"$PORT" | xargs kill -9 2>/dev/null || true
    sleep 1
    info "Starting single_tenant_agent.py ..."
    PYTHONPATH="$REPO_ROOT/src" \
    S3_VECTOR_BUCKET_NAME="$S3_VECTOR_BUCKET_NAME" \
    AWS_REGION="$REGION" \
      python3 "$REPO_ROOT/examples/single_tenant_agent.py" &
    SERVER_PID=$!
    wait_for_server
  fi

  info "Session 1 / Turn 1 -- storing a fact"
  R=$(invoke "conv-s1-001" "user-001" \
    '{"prompt": "My favourite framework is Strands Agents.", "end_session": false, "user_id": "user-001"}')
  assert_contains "Turn 1 responds" "$R" "Strands"

  info "Session 1 / Turn 2 -- end_session=true (triggers background summarization)"
  R=$(invoke "conv-s1-001" "user-001" \
    '{"prompt": "Thanks, bye.", "end_session": true, "user_id": "user-001"}')
  assert_contains "Turn 2 responds" "$R" "."

  wait_for_summary "memory"

  info "Session 2 -- long-term memory injected on first turn"
  R=$(invoke "conv-s1-002" "user-001" \
    '{"prompt": "What do you know about my preferences?", "user_id": "user-001"}')
  assert_contains "Long-term memory recalled in session 2" "$R" "Strands"

  info "Session 3 -- memory_tool mid-turn recall"
  R=$(invoke "conv-s1-003" "user-001" \
    '{"prompt": "I am evaluating new tools. Remind me -- what framework did I mention I liked in a previous session?", "user_id": "user-001"}')
  assert_contains "memory_tool recalls framework" "$R" "Strands"

  if $LOCAL_MODE && [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    for i in $(seq 1 10); do
      if ! lsof -ti tcp:"$PORT" > /dev/null 2>&1; then break; fi
      sleep 1
    done
    pass "Server stopped"
  fi
}

# ---------------------------------------------------------------------------
# Multi-tenant test suite
# ---------------------------------------------------------------------------
run_multi_tenant_tests() {
  echo ""
  echo "  ┌─────────────────────────────────────────────────────┐"
  echo "  │  MULTI-TENANT TESTS                                 │"
  echo "  └─────────────────────────────────────────────────────┘"

  if $AGENTCORE_MODE; then
    deploy_to_agentcore "multi_tenant_agent.py" "strands_s3_vector_memory_agent"
  fi

  reset_indexes "memory-tenant-001" "memory-tenant-002"

  SERVER_PID=""
  if $LOCAL_MODE; then
    lsof -ti tcp:"$PORT" | xargs kill -9 2>/dev/null || true
    sleep 1
    info "Starting multi_tenant_agent.py ..."
    PYTHONPATH="$REPO_ROOT/src" \
    S3_VECTOR_BUCKET_NAME="$S3_VECTOR_BUCKET_NAME" \
    S3_VECTOR_TVM_ROLE_ARN="$S3_VECTOR_TVM_ROLE_ARN" \
    AWS_REGION="$REGION" \
      python3 "$REPO_ROOT/examples/multi_tenant_agent.py" &
    SERVER_PID=$!
    wait_for_server
  fi

  info "Session 1 / Turn 1 -- tenant-001 stores a confidential fact"
  R=$(invoke "conv-mt-001" "user-001" \
    '{"prompt": "Our Q4 budget is 2 million dollars and it is confidential.", "end_session": false}' "$JWT1")
  assert_contains "Turn 1 responds" "$R" "."

  info "Session 1 / Turn 2 -- tenant-001 end_session=true"
  R=$(invoke "conv-mt-001" "user-001" \
    '{"prompt": "Got it, thanks.", "end_session": true}' "$JWT1")
  assert_contains "Turn 2 responds" "$R" "."

  wait_for_summary "memory-tenant-001"

  info "Session 2 -- tenant-001 recalls long-term memory"
  R=$(invoke "conv-mt-002" "user-001" \
    '{"prompt": "What did I tell you about our budget?"}' "$JWT1")
  assert_contains "tenant-001 recalls \$2M budget" "$R" "2 million|million|confidential"

  info "Session 3 -- tenant-002 isolation check (must NOT see \$2M)"
  R=$(invoke "conv-mt-003" "user-002" \
    '{"prompt": "What did I tell you about our budget?"}' "$JWT2")
  assert_not_contains "tenant-002 cannot see tenant-001 budget" "$R" "2M|2 million"
  assert_contains     "tenant-002 has no memory" "$R" "don.t have|no information|haven.t|not aware|not find|couldn.t find|did not"

  info "Session 4 -- tenant-001 memory_tool mid-turn recall"
  R=$(invoke "conv-mt-004" "user-001" \
    '{"prompt": "We are planning Q1. Remind me what our Q4 budget was and any constraints I mentioned?"}' "$JWT1")
  assert_contains "tenant-001 recalls budget via memory_tool" "$R" "2 million|million|confidential"

  if $LOCAL_MODE && [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    for i in $(seq 1 10); do
      if ! lsof -ti tcp:"$PORT" > /dev/null 2>&1; then break; fi
      sleep 1
    done
    pass "Server stopped"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  S3 Vector Memory Plugin -- Example Tests                ║"
if $LOCAL_MODE; then
echo "║  Mode: LOCAL  (port $PORT)                                  ║"
else
echo "║  Mode: AGENTCORE RUNTIME                                  ║"
[[ -n "$AGENT_ARN" ]] && echo "║  ARN:  $AGENT_ARN"
fi
echo "╚══════════════════════════════════════════════════════════╝"

setup_tvm
setup_cognito_and_get_jwts

[[ "$MODE" == "single" || "$MODE" == "both" ]] && run_single_tenant_tests
[[ "$MODE" == "multi"  || "$MODE" == "both" ]] && run_multi_tenant_tests

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ $FAILURES -eq 0 ]]; then
  echo -e "${GREEN}  All tests passed ✓${NC}"
  exit 0
else
  echo -e "${RED}  $FAILURES test(s) failed ✗${NC}"
  exit 1
fi
