"""One AWS task role per tenant. Used on AWS only; local runs have no IAM.

A job run for a tenant takes this role (a task-role override, set by aws-run.sh). The role can read
that tenant's secret, the one that holds its three logins, and touch that tenant's buckets and
objects. Nothing else. The admin task creates the role when the tenant is provisioned, and deletes
it at teardown.

The Batch Operations and KMS permissions are shared, and the CreateJob action can't be scoped
to one tenant (the job doesn't exist before the call). Those are the two places a tenant's role
is wider than its own data.
"""
import json
import os
import uuid

import boto3

from tenantmig import identities, settings
from tenantmig.store import org_bucket

ROLE_PREFIX = "tenantmig-"


def role_name(org_id: str) -> str:
    return ROLE_PREFIX + uuid.UUID(str(org_id)).hex


def role_arn(org_id: str) -> str:
    return f"arn:aws:iam::{settings.ACCOUNT_ID}:role/{role_name(org_id)}"


def policy(org_id: str) -> dict:
    org = str(uuid.UUID(str(org_id)))
    region, account = settings.S3_REGION, settings.ACCOUNT_ID
    dest, shared, migration = org_bucket(org), settings.V1_SHARED_BUCKET, settings.MIGRATION_BUCKET
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "OwnSecret", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
         "Resource": f"arn:aws:secretsmanager:{region}:{account}:secret:{identities.secret_name(org)}-*"},
        {"Sid": "OwnBucket", "Effect": "Allow",
         "Action": ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": [f"arn:aws:s3:::{dest}", f"arn:aws:s3:::{dest}/*"],
         "Condition": {"StringEquals": {"aws:ResourceAccount": account}}},
        {"Sid": "CreateOwnBucket", "Effect": "Allow",
         "Action": ["s3:CreateBucket", "s3:PutEncryptionConfiguration", "s3:PutBucketPublicAccessBlock"],
         "Resource": f"arn:aws:s3:::{dest}"},
        # s3:prefix only exists on ListBucket, so the listing gets its own statement. Read the
        # tenant's V1 objects (and their HeadObject checks) in a statement without it.
        {"Sid": "OwnV1List", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{shared}",
         "Condition": {"StringLike": {"s3:prefix": [f"{org}/*"]}, "StringEquals": {"aws:ResourceAccount": account}}},
        {"Sid": "OwnV1Objects", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{shared}/{org}/*",
         "Condition": {"StringEquals": {"aws:ResourceAccount": account}}},
        {"Sid": "OwnMigrationPrefixes", "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject"],
         "Resource": [f"arn:aws:s3:::{migration}/_manifests/{org}/*", f"arn:aws:s3:::{migration}/_reports/{org}/*"],
         "Condition": {"StringEquals": {"aws:ResourceAccount": account}}},
        {"Sid": "MigrationBucket", "Effect": "Allow", "Action": ["s3:CreateBucket", "s3:ListBucket"],
         "Resource": f"arn:aws:s3:::{migration}"},
        {"Sid": "OwnConnectorSecrets", "Effect": "Allow", "Action": "secretsmanager:DescribeSecret",
         "Resource": f"arn:aws:secretsmanager:{region}:{account}:secret:tenantmig/connector/{org}/*"},
        {"Sid": "BatchCopy", "Effect": "Allow",
         "Action": ["s3:CreateJob", "s3:DescribeJob", "s3:UpdateJobStatus"], "Resource": "*"},
        {"Sid": "BatchRole", "Effect": "Allow", "Action": "iam:PassRole",
         "Resource": settings.BATCH_OPS_ROLE_ARN},
        {"Sid": "CellKey", "Effect": "Allow",
         "Action": ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*", "kms:ReEncrypt*"],
         "Resource": settings.CELL_KMS_KEY_ARN},
        {"Sid": "Code", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{os.environ['CODE_BUCKET']}/*"},
    ]}


def ensure_task_role(org_id: str) -> None:
    """Admin step: create or update the tenant's task role. Idempotent."""
    iam = boto3.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        iam.create_role(RoleName=role_name(org_id), AssumeRolePolicyDocument=json.dumps(trust),
                        Description=f"tenantmig job role for tenant {org_id}")
    except iam.exceptions.EntityAlreadyExistsException:
        pass
    iam.put_role_policy(RoleName=role_name(org_id), PolicyName="tenant", PolicyDocument=json.dumps(policy(org_id)))


def delete_task_roles() -> list[str]:
    """Sandbox teardown: remove every tenant role this sample created."""
    iam = boto3.client("iam")
    removed = []
    for page in iam.get_paginator("list_roles").paginate(PathPrefix="/"):
        for role in page["Roles"]:
            if role["RoleName"].startswith(ROLE_PREFIX):
                for name in iam.list_role_policies(RoleName=role["RoleName"])["PolicyNames"]:
                    iam.delete_role_policy(RoleName=role["RoleName"], PolicyName=name)
                iam.delete_role(RoleName=role["RoleName"])
                removed.append(role["RoleName"])
    return removed
