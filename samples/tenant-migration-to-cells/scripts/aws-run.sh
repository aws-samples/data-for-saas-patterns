#!/usr/bin/env bash
# Run a tenantmig command as a one-off Fargate task in the sandbox and stream its logs.
#   scripts/aws-run.sh init
#   scripts/aws-run.sh demo --messages 2000
#   scripts/aws-run.sh demo --inject-bug
set -euo pipefail
# Uses your current AWS credentials (for example AWS_PROFILE). Region defaults to us-east-2.
export AWS_REGION="${AWS_REGION:-us-east-2}"
STACK=TenantMigrationSample
out() { aws cloudformation describe-stacks --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

CLUSTER=$(out ClusterName); TASKDEF=$(out TaskDefinitionArn); SUBNETS=$(out JobSubnets)
SG=$(out JobSecurityGroup); LOGS=$(out LogGroupName)
CMD=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:] or ["demo"]))' "$@")

TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASKDEF" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SG}],assignPublicIp=DISABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"tenantmig\",\"command\":${CMD}}]}" \
  --query 'tasks[0].taskArn' --output text)
echo "Started $TASK"
echo "Waiting for it to finish (Aurora resumes from pause on first connection, allow ~1 min)..."
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"

STREAM="tenantmig/tenantmig/${TASK##*/}"
aws logs get-log-events --log-group-name "$LOGS" --log-stream-name "$STREAM" --start-from-head \
  --query 'events[].message' --output text | tr '\t' '\n'
CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)
echo "Exit code: $CODE"
[ "$CODE" = "0" ]
