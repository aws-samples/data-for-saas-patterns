#!/usr/bin/env bash
# setup_cognito.sh — Creates Cognito User Pool, App Client, and two test users
# for the AgentCore Memory multi-tenant example.
#
# Usage: bash setup_cognito.sh
#
# Outputs (export these before running the agent or tests):
#   export COGNITO_USER_POOL_ID=...
#   export COGNITO_CLIENT_ID=...
#   export COGNITO_USER_A_USERNAME=...
#   export COGNITO_USER_B_USERNAME=...
#   export COGNITO_USER_PASSWORD=...
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
POOL_NAME="agentcore-memory-user-pool"
CLIENT_NAME="agentcore-memory-client"
USER_A_EMAIL="tenant001@example.com"
USER_B_EMAIL="tenant002@example.com"
TEMP_PASSWORD="Temp@12345!"
FINAL_PASSWORD="Test@12345!"

echo "→ Region : $REGION"
echo "→ Pool   : $POOL_NAME"
echo ""

# ---------------------------------------------------------------------------
# User Pool — check if already exists
# ---------------------------------------------------------------------------
EXISTING_POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='${POOL_NAME}'].Id" --output text 2>/dev/null || true)

if [ -n "$EXISTING_POOL_ID" ]; then
  HAS_TENANT_ID=$(aws cognito-idp describe-user-pool --user-pool-id "$EXISTING_POOL_ID" \
    --region "$REGION" \
    --query "UserPool.SchemaAttributes[?Name=='custom:tenant_id'].Name" \
    --output text 2>/dev/null || true)

  if [ -z "$HAS_TENANT_ID" ]; then
    echo "→ Existing pool missing custom attributes — deleting and recreating ..."
    aws cognito-idp delete-user-pool --user-pool-id "$EXISTING_POOL_ID" --region "$REGION"
    EXISTING_POOL_ID=""
  else
    echo "→ User pool already exists with correct schema: $EXISTING_POOL_ID"
    USER_POOL_ID="$EXISTING_POOL_ID"
  fi
fi

if [ -z "$EXISTING_POOL_ID" ]; then
  echo "→ Creating user pool ..."
  USER_POOL_ID=$(aws cognito-idp create-user-pool \
    --pool-name "$POOL_NAME" \
    --region "$REGION" \
    --schema '[{"Name":"tenant_id","AttributeDataType":"String","Mutable":true},{"Name":"tenant_name","AttributeDataType":"String","Mutable":true},{"Name":"tier","AttributeDataType":"String","Mutable":true}]' \
    --policies "PasswordPolicy={MinimumLength=8,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=true}" \
    --auto-verified-attributes email \
    --admin-create-user-config "AllowAdminCreateUserOnly=true" \
    --query "UserPool.Id" --output text)
  echo "→ Created user pool: $USER_POOL_ID"
fi

# ---------------------------------------------------------------------------
# Enforce admin-only user creation (disable self-registration).
# Cognito defaults AllowAdminCreateUserOnly to false, which lets anyone sign
# themselves up. This sample provisions users only via admin-create-user, so
# self-registration must be off. Applied on every run so a pool created before
# this hardening is remediated on reuse.
# ---------------------------------------------------------------------------
echo "→ Enforcing admin-only user creation (self-registration disabled) ..."
aws cognito-idp update-user-pool \
  --user-pool-id "$USER_POOL_ID" \
  --region "$REGION" \
  --admin-create-user-config "AllowAdminCreateUserOnly=true" \
  --auto-verified-attributes email \
  --policies "PasswordPolicy={MinimumLength=8,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=true}" \
  > /dev/null

# ---------------------------------------------------------------------------
# App Client — check if already exists
# ---------------------------------------------------------------------------
EXISTING_CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
  --user-pool-id "$USER_POOL_ID" --region "$REGION" \
  --query "UserPoolClients[?ClientName=='${CLIENT_NAME}'].ClientId" --output text 2>/dev/null || true)

if [ -n "$EXISTING_CLIENT_ID" ]; then
  echo "→ App client already exists: $EXISTING_CLIENT_ID"
  CLIENT_ID="$EXISTING_CLIENT_ID"
else
  echo "→ Creating app client ..."
  CLIENT_ID=$(aws cognito-idp create-user-pool-client \
    --user-pool-id "$USER_POOL_ID" \
    --client-name "$CLIENT_NAME" \
    --region "$REGION" \
    --no-generate-secret \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --query "UserPoolClient.ClientId" --output text)
  echo "→ Created app client: $CLIENT_ID"
fi

# ---------------------------------------------------------------------------
# Helper: create user + set permanent password + set tenant attributes
# ---------------------------------------------------------------------------
create_user() {
  local USERNAME="$1"
  local TENANT_ID="$2"
  local TENANT_NAME="$3"
  local TIER="$4"

  if aws cognito-idp admin-get-user --user-pool-id "$USER_POOL_ID" \
       --username "$USERNAME" --region "$REGION" &>/dev/null; then
    echo "→ User already exists: $USERNAME"
  else
    echo "→ Creating user: $USERNAME (tenant: $TENANT_ID) ..."
    aws cognito-idp admin-create-user \
      --user-pool-id "$USER_POOL_ID" \
      --username "$USERNAME" \
      --region "$REGION" \
      --temporary-password "$TEMP_PASSWORD" \
      --message-action SUPPRESS \
      --user-attributes \
        Name=email,Value="$USERNAME" \
        Name=email_verified,Value=true \
        Name="custom:tenant_id",Value="$TENANT_ID" \
        Name="custom:tenant_name",Value="$TENANT_NAME" \
        Name="custom:tier",Value="$TIER" \
      > /dev/null

    aws cognito-idp admin-set-user-password \
      --user-pool-id "$USER_POOL_ID" \
      --username "$USERNAME" \
      --region "$REGION" \
      --password "$FINAL_PASSWORD" \
      --permanent
    echo "→ User confirmed: $USERNAME"
  fi
}

create_user "$USER_A_EMAIL" "tenant-001" "Acme Corp"   "premium"
create_user "$USER_B_EMAIL" "tenant-002" "Globex Inc"  "standard"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
echo ""
echo "✓ Done. Set these environment variables before running the tests:"
echo ""
echo "  export COGNITO_USER_POOL_ID=${USER_POOL_ID}"
echo "  export COGNITO_CLIENT_ID=${CLIENT_ID}"
echo "  export COGNITO_USER_A_USERNAME=${USER_A_EMAIL}"
echo "  export COGNITO_USER_B_USERNAME=${USER_B_EMAIL}"
echo "  export COGNITO_USER_PASSWORD=${FINAL_PASSWORD}"
echo ""
