#!/usr/bin/env bash
# setup_tvm_role.sh — Creates the S3 Vectors TVM IAM role
# Usage: bash setup_tvm_role.sh <s3-vector-bucket-name>
set -euo pipefail

BUCKET_NAME="${1:?Usage: bash setup_tvm_role.sh <s3-vector-bucket-name>}"
ROLE_NAME="workshop-module3-lab1-s3vectors-tvm-role"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="${AWS_REGION:-us-east-1}"

echo "→ Account : $ACCOUNT_ID"
echo "→ Region  : $REGION"
echo "→ Bucket  : $BUCKET_NAME"
echo "→ Role    : $ROLE_NAME"
echo ""

# ---------------------------------------------------------------------------
# Trust policy — allows the current account to assume + tag the role
# ---------------------------------------------------------------------------
TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::${ACCOUNT_ID}:root" },
      "Action": ["sts:AssumeRole", "sts:TagSession"]
    }
  ]
}
EOF
)

# ---------------------------------------------------------------------------
# S3 Vectors policy — scoped to TenantID session tag via ABAC condition.
# The Resource ARN embeds ${aws:PrincipalTag/TenantID} so credentials for
# tenant-002 can only access index "memory-tenant-002", never "memory-tenant-001".
# The bucket/* wildcard means no policy change is needed when adding new buckets.
# ---------------------------------------------------------------------------
S3VECTORS_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3vectors:PutVectors",
        "s3vectors:QueryVectors",
        "s3vectors:GetVectors",
        "s3vectors:ListVectors"
      ],
      "Resource": "arn:aws:s3vectors:${REGION}:${ACCOUNT_ID}:bucket/*/index/memory-\${aws:PrincipalTag/TenantID}"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3vectors:GetIndex",
        "s3vectors:ListIndexes"
      ],
      "Resource": "arn:aws:s3vectors:${REGION}:${ACCOUNT_ID}:bucket/${BUCKET_NAME}/index/memory-\${aws:PrincipalTag/TenantID}"
    }
  ]
}
EOF
)

# ---------------------------------------------------------------------------
# Create or update the role
# ---------------------------------------------------------------------------
if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null; then
  echo "→ Role already exists — updating trust policy ..."
  aws iam update-assume-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-document "$TRUST_POLICY"
else
  echo "→ Creating role $ROLE_NAME ..."
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --description "TVM role for S3 Vectors tenant isolation" \
    > /dev/null
fi

echo "→ Attaching S3 Vectors policy ..."
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "S3VectorsTenantPolicy" \
  --policy-document "$S3VECTORS_POLICY"

ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)

echo ""
echo "✓ Done. Set these environment variables before running the demo:"
echo ""
echo "  export S3_VECTOR_BUCKET_NAME=${BUCKET_NAME}"
echo "  export S3_VECTOR_TVM_ROLE_ARN=${ROLE_ARN}"
echo ""
