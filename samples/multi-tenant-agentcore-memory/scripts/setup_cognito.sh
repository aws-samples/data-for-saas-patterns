#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# setup_cognito.sh — Creates Cognito User Pool, App Client, and two test users
# for the AgentCore Memory multi-tenant example.
#
# The tenant id is the root of trust for every isolation check, so
# custom:tenant_id (and custom:tier) are created immutable, and the app client
# can read them but not write them. A signed-in user cannot move themselves to
# another tenant.
#
# A fresh random password for the test users is generated on every run and
# printed once at the end.
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
SAMPLE_TAG="sample=multi-tenant-agentcore-memory"
USER_A_EMAIL="tenant001@example.com"
USER_B_EMAIL="tenant002@example.com"

# Random password meeting the pool policy (upper, lower, digit, symbol).
USER_PASSWORD=$(python3 -c '
import secrets, string
chars = string.ascii_letters + string.digits
body = "".join(secrets.choice(chars) for _ in range(20))
print("Aa1!" + body)
')

READ_ATTRIBUTES=(email email_verified custom:tenant_id custom:tenant_name custom:tier)
WRITE_ATTRIBUTES=(email)

echo "→ Region : $REGION"
echo "→ Pool   : $POOL_NAME"
echo ""

# ---------------------------------------------------------------------------
# User Pool — reuse only a pool this sample created with the right schema
# ---------------------------------------------------------------------------
EXISTING_POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='${POOL_NAME}'].Id" --output text 2>/dev/null || true)

if [ "$(wc -w <<<"$EXISTING_POOL_ID")" -gt 1 ]; then
  echo "ERROR: more than one user pool is named '$POOL_NAME' ($EXISTING_POOL_ID)."
  echo "       Delete the ones you do not need, then re-run."
  exit 1
fi

if [ -n "$EXISTING_POOL_ID" ]; then
  POOL_ARN=$(aws cognito-idp describe-user-pool --user-pool-id "$EXISTING_POOL_ID" \
    --region "$REGION" --query "UserPool.Arn" --output text)
  OWNED=$(aws cognito-idp list-tags-for-resource --resource-arn "$POOL_ARN" \
    --region "$REGION" --query "Tags.sample" --output text 2>/dev/null || true)
  TENANT_MUTABLE=$(aws cognito-idp describe-user-pool --user-pool-id "$EXISTING_POOL_ID" \
    --region "$REGION" \
    --query "UserPool.SchemaAttributes[?Name=='custom:tenant_id'].Mutable | [0]" \
    --output text 2>/dev/null || true)

  if [ "$OWNED" != "multi-tenant-agentcore-memory" ] || [ "$TENANT_MUTABLE" != "False" ]; then
    echo "ERROR: user pool '$POOL_NAME' ($EXISTING_POOL_ID) exists but was not created"
    echo "       by this version of the script (it is untagged, or custom:tenant_id is"
    echo "       missing or mutable). Cognito cannot change that in place. Delete it"
    echo "       yourself if it is safe to do so, then re-run:"
    echo "         aws cognito-idp delete-user-pool --user-pool-id $EXISTING_POOL_ID --region $REGION"
    exit 1
  fi
  echo "→ User pool already exists with correct schema: $EXISTING_POOL_ID"
  USER_POOL_ID="$EXISTING_POOL_ID"
else
  echo "→ Creating user pool ..."
  USER_POOL_ID=$(aws cognito-idp create-user-pool \
    --pool-name "$POOL_NAME" \
    --region "$REGION" \
    --schema '[{"Name":"tenant_id","AttributeDataType":"String","Mutable":false},{"Name":"tenant_name","AttributeDataType":"String","Mutable":true},{"Name":"tier","AttributeDataType":"String","Mutable":false}]' \
    --policies "PasswordPolicy={MinimumLength=8,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=true}" \
    --auto-verified-attributes email \
    --admin-create-user-config "AllowAdminCreateUserOnly=true" \
    --user-pool-tags "$SAMPLE_TAG" \
    --query "UserPool.Id" --output text)
  echo "→ Created user pool: $USER_POOL_ID"
fi

# ---------------------------------------------------------------------------
# Enforce admin-only user creation (disable self-registration).
# Cognito defaults AllowAdminCreateUserOnly to false, which lets anyone sign
# themselves up. This sample provisions users only via admin-create-user, so
# self-registration must be off.
# ---------------------------------------------------------------------------
echo "→ Enforcing admin-only user creation (self-registration disabled) ..."
aws cognito-idp update-user-pool \
  --user-pool-id "$USER_POOL_ID" \
  --region "$REGION" \
  --admin-create-user-config "AllowAdminCreateUserOnly=true" \
  --auto-verified-attributes email \
  --policies "PasswordPolicy={MinimumLength=8,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=true}" \
  --user-pool-tags "$SAMPLE_TAG" \
  > /dev/null

# ---------------------------------------------------------------------------
# App Client — explicit read and write attribute lists. The client can read the
# tenant attributes into tokens, but users can only write their email.
# ---------------------------------------------------------------------------
EXISTING_CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
  --user-pool-id "$USER_POOL_ID" --region "$REGION" \
  --query "UserPoolClients[?ClientName=='${CLIENT_NAME}'].ClientId" --output text 2>/dev/null || true)

if [ -n "$EXISTING_CLIENT_ID" ]; then
  echo "→ App client already exists: $EXISTING_CLIENT_ID — applying attribute lists ..."
  CLIENT_ID="$EXISTING_CLIENT_ID"
  aws cognito-idp update-user-pool-client \
    --user-pool-id "$USER_POOL_ID" \
    --client-id "$CLIENT_ID" \
    --region "$REGION" \
    --client-name "$CLIENT_NAME" \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --read-attributes "${READ_ATTRIBUTES[@]}" \
    --write-attributes "${WRITE_ATTRIBUTES[@]}" \
    > /dev/null
else
  echo "→ Creating app client ..."
  CLIENT_ID=$(aws cognito-idp create-user-pool-client \
    --user-pool-id "$USER_POOL_ID" \
    --client-name "$CLIENT_NAME" \
    --region "$REGION" \
    --no-generate-secret \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --read-attributes "${READ_ATTRIBUTES[@]}" \
    --write-attributes "${WRITE_ATTRIBUTES[@]}" \
    --query "UserPoolClient.ClientId" --output text)
  echo "→ Created app client: $CLIENT_ID"
fi

# ---------------------------------------------------------------------------
# Helper: create user (if needed) + set this run's permanent password
# ---------------------------------------------------------------------------
create_user() {
  local USERNAME="$1"
  local TENANT_ID="$2"
  local TENANT_NAME="$3"
  local TIER="$4"

  if aws cognito-idp admin-get-user --user-pool-id "$USER_POOL_ID" \
       --username "$USERNAME" --region "$REGION" &>/dev/null; then
    echo "→ User already exists: $USERNAME — resetting password"
  else
    echo "→ Creating user: $USERNAME (tenant: $TENANT_ID) ..."
    aws cognito-idp admin-create-user \
      --user-pool-id "$USER_POOL_ID" \
      --username "$USERNAME" \
      --region "$REGION" \
      --message-action SUPPRESS \
      --user-attributes \
        Name=email,Value="$USERNAME" \
        Name=email_verified,Value=true \
        Name="custom:tenant_id",Value="$TENANT_ID" \
        Name="custom:tenant_name",Value="$TENANT_NAME" \
        Name="custom:tier",Value="$TIER" \
      > /dev/null
  fi

  aws cognito-idp admin-set-user-password \
    --user-pool-id "$USER_POOL_ID" \
    --username "$USERNAME" \
    --region "$REGION" \
    --password "$USER_PASSWORD" \
    --permanent
  echo "→ User confirmed: $USERNAME"
}

create_user "$USER_A_EMAIL" "tenant-001" "Acme Corp"   "premium"
create_user "$USER_B_EMAIL" "tenant-002" "Globex Inc"  "standard"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
echo ""
echo "✓ Done. Set these environment variables before running the tests."
echo "  The password is new on every run; keep it out of shared logs."
echo ""
echo "  export COGNITO_USER_POOL_ID=${USER_POOL_ID}"
echo "  export COGNITO_CLIENT_ID=${CLIENT_ID}"
echo "  export COGNITO_USER_A_USERNAME=${USER_A_EMAIL}"
echo "  export COGNITO_USER_B_USERNAME=${USER_B_EMAIL}"
echo "  export COGNITO_USER_PASSWORD='${USER_PASSWORD}'"
echo ""
