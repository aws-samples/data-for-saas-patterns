#!/usr/bin/env bash
# Run a tenantmig command as a one-off Fargate task in the sandbox and stream its logs.
#   scripts/aws-run.sh init
#   scripts/aws-run.sh demo --messages 2000
#   scripts/aws-run.sh demo --inject-bug
#   scripts/aws-run.sh migrate-content <org>     (runs in the job task, without owner credentials)
set -euo pipefail
# Uses your current AWS credentials (for example AWS_PROFILE). Region defaults to us-east-2.
export AWS_REGION="${AWS_REGION:-us-east-2}"
STACK=TenantMigrationSample
out() { aws cloudformation describe-stacks --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

CLUSTER=$(out ClusterName); SUBNETS=$(out JobSubnets)
SG=$(out JobSecurityGroup); LOGS=$(out LogGroupName)
CMD=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:] or ["demo"]))' "$@")
# Setup commands need the owner credentials, so they run in the admin task. Everything else runs in
# the job task, which has only the tenant, migrator and control identities.
case "${1:-demo}" in
  init|generate|reset|purge-buckets|demo|orgs) TASKDEF=$(out TaskDefinitionArn) ;;
  *) TASKDEF=$(out JobTaskDefinitionArn) ;;
esac
# A job for one tenant runs under that tenant's own task role, which can read only its secret and
# buckets. The org is the second argument of every job command.
OVERRIDES=$(python3 - "${1:-demo}" "${2:-}" <<'PY'
import json, subprocess, sys, uuid
cmd, org = sys.argv[1], sys.argv[2]
out = {"containerOverrides": [{"name": "tenantmig", "command": None}]}
if cmd not in ("init", "generate", "reset", "purge-buckets", "demo", "orgs") and org:
    account = subprocess.check_output(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"], text=True).strip()
    out["taskRoleArn"] = f"arn:aws:iam::{account}:role/tenantmig-{uuid.UUID(org).hex}"
print(json.dumps(out))
PY
)

TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASKDEF" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SG}],assignPublicIp=DISABLED}" \
  --overrides "$(python3 -c 'import json,sys; o=json.loads(sys.argv[1]); o["containerOverrides"][0]["command"]=json.loads(sys.argv[2]); print(json.dumps(o))' "$OVERRIDES" "$CMD")" \
  --query 'tasks[0].taskArn' --output text)
echo "Started $TASK"
echo "Task role: $(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].overrides.taskRoleArn' --output text 2>/dev/null)"
echo "Waiting for it to finish (Aurora resumes from pause on first connection, allow ~1 min)..."
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"

STREAM="tenantmig/tenantmig/${TASK##*/}"
# CloudWatch can lag the task's exit by a minute. Poll until the stream has events, then print them all.
for _ in $(seq 1 36); do
  OUT=$(aws logs get-log-events --log-group-name "$LOGS" --log-stream-name "$STREAM" --start-from-head \
    --query 'events[].message' --output text 2>/dev/null || true)
  [ -n "$OUT" ] && [ "$OUT" != "None" ] && break
  sleep 5
done
printf '%s\n' "$OUT" | tr '\t' '\n'
CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)
echo "Exit code: $CODE"
[ "$CODE" = "0" ]
