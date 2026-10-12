import * as path from 'path';
import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Tags } from 'aws-cdk-lib';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as assets from 'aws-cdk-lib/aws-s3-assets';
import { Construct } from 'constructs';

export interface SandboxStackProps extends StackProps {
  notifyEmail?: string;
  monthlyBudgetUsd: number;
  v1EngineVersion: string;
  v2EngineVersion: string;
}

/**
 * Sandbox for the per-org V1 -> V2 migration demo, sized to stay well under the budget:
 * - Aurora Serverless v2 scaling to 0 ACU and auto-pausing when idle
 * - one NAT gateway; the job runs as an on-demand Fargate task in private subnets;
 *   databases sit in isolated subnets
 * - S3 gateway endpoint (free) for bucket traffic
 * A production platform could run the same container as a Kubernetes Job on Amazon EKS; Fargate
 * here avoids the EKS control-plane cost for a sample.
 */
export class SandboxStack extends Stack {
  constructor(scope: Construct, id: string, props: SandboxStackProps) {
    super(scope, id, props);
    Tags.of(this).add('project', 'tenant-migration-to-cells');
    Tags.of(this).add('environment', 'sandbox');

    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      // Egress through one NAT gateway: this also works in accounts with VPC Block Public Access
      // in block-ingress mode, where public IPs get no internet access.
      natGateways: 1,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'data', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
        { name: 'jobs', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
      gatewayEndpoints: { S3: { service: ec2.GatewayVpcEndpointAwsService.S3 } },
    });

    const taskSg = new ec2.SecurityGroup(this, 'JobSg', { vpc, description: 'Migration job (egress only)' });
    const dbSg = new ec2.SecurityGroup(this, 'DbSg', { vpc, description: 'Aurora clusters', allowAllOutbound: false });
    dbSg.addIngressRule(taskSg, ec2.Port.tcp(5432), 'migration job');
    dbSg.addIngressRule(dbSg, ec2.Port.tcp(5432), 'postgres_fdw from management to V1');
    dbSg.addEgressRule(dbSg, ec2.Port.tcp(5432), 'postgres_fdw from management to V1');

    const cellKey = new kms.Key(this, 'Cell1Key', {
      alias: 'alias/tenantmig/cell-1', enableKeyRotation: true, removalPolicy: RemovalPolicy.DESTROY,
      pendingWindow: Duration.days(7), description: 'Cell 1 key for per-org buckets',
    });

    const cluster = (name: string, version: string, major: string, dbName: string) => {
      const c = new rds.DatabaseCluster(this, name, {
        engine: rds.DatabaseClusterEngine.auroraPostgres({
          version: rds.AuroraPostgresEngineVersion.of(version, major),
        }),
        writer: rds.ClusterInstance.serverlessV2('writer'),
        serverlessV2MinCapacity: 0,
        serverlessV2MaxCapacity: 2,
        defaultDatabaseName: dbName,
        credentials: rds.Credentials.fromGeneratedSecret('postgres', { secretName: `tenantmig/${name}` }),
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
        securityGroups: [dbSg],
        storageEncrypted: true,
        backup: { retention: Duration.days(1) },
        deletionProtection: false,              // sandbox only, synthetic data
        removalPolicy: RemovalPolicy.DESTROY,
        cloudwatchLogsRetention: logs.RetentionDays.ONE_WEEK,
      });
      (c.node.defaultChild as rds.CfnDBCluster).addPropertyOverride(
        'ServerlessV2ScalingConfiguration.SecondsUntilAutoPause', 300);
      return c;
    };

    const dbs = {
      V1_MESSAGING: { c: cluster('V1Messaging', props.v1EngineVersion, '15', 'messaging'), db: 'messaging' },
      V1_AUTH: { c: cluster('V1Auth', props.v1EngineVersion, '15', 'auth'), db: 'auth' },
      V2_CELL_MESSAGING: { c: cluster('V2Cell1Messaging', props.v2EngineVersion, '18', 'messaging'), db: 'messaging' },
      V2_MANAGEMENT: { c: cluster('V2Management', props.v2EngineVersion, '18', 'management'), db: 'management' },
    };

    // Job code ships as an S3 asset and runs on a stock Python image (no image build needed).
    const code = new assets.Asset(this, 'JobCode', {
      path: path.join(__dirname, '..', '..'),
      exclude: ['infra/node_modules', 'infra/cdk.out', '.venv', '.local', '.git', '**/__pycache__', 'docs/*.png'],
    });

    const logGroup = new logs.LogGroup(this, 'JobLogs', {
      retention: logs.RetentionDays.ONE_WEEK, removalPolicy: RemovalPolicy.DESTROY,
    });
    const ecsCluster = new ecs.Cluster(this, 'Jobs', { vpc });
    const taskDef = new ecs.FargateTaskDefinition(this, 'MigrationTask', {
      cpu: 1024, memoryLimitMiB: 2048,
      runtimePlatform: { cpuArchitecture: ecs.CpuArchitecture.ARM64, operatingSystemFamily: ecs.OperatingSystemFamily.LINUX },
    });
    code.grantRead(taskDef.taskRole);

    const bucketPrefix = `tm-${this.account}-`;
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:CreateBucket', 's3:DeleteBucket', 's3:PutEncryptionConfiguration', 's3:PutBucketPublicAccessBlock',
        's3:ListBucket', 's3:GetObject', 's3:PutObject', 's3:DeleteObject'],
      resources: [`arn:aws:s3:::${bucketPrefix}*`, `arn:aws:s3:::${bucketPrefix}*/*`],
    }));
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['s3:ListAllMyBuckets'], resources: ['*'] }));
    cellKey.grantEncryptDecrypt(taskDef.taskRole);

    // Step 8: S3 Batch Operations copies an org's objects out of the V1 shared bucket.
    const sharedBucket = `${bucketPrefix}v1-shared`;
    const batchRole = new iam.Role(this, 'BatchOpsRole', {
      assumedBy: new iam.ServicePrincipal('batchoperations.s3.amazonaws.com'),
      description: 'S3 Batch Operations: copy V1 shared-bucket objects into per-org buckets',
    });
    batchRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:GetObjectVersion', 's3:GetObjectTagging', 's3:PutObject', 's3:PutObjectTagging'],
      resources: [`arn:aws:s3:::${bucketPrefix}*/*`],
    }));
    batchRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket', 's3:GetBucketLocation'], resources: [`arn:aws:s3:::${bucketPrefix}*`],
    }));
    cellKey.grantEncryptDecrypt(batchRole);
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:CreateJob', 's3:DescribeJob'], resources: ['*'],
    }));
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      // Scoped to the one Batch Operations role. (s3:CreateJob does not set iam:PassedToService.)
      actions: ['iam:PassRole'], resources: [batchRole.roleArn],
    }));

    const environment: Record<string, string> = {
      AWS_REGION: this.region, ORG_BUCKET_PREFIX: bucketPrefix, CELL_KMS_KEY_ARN: cellKey.keyArn,
      CODE_BUCKET: code.s3BucketName, CODE_KEY: code.s3ObjectKey,
      SQL_DIR: '/app/sql', V1_SHARED_BUCKET: sharedBucket, BATCH_OPS_ROLE_ARN: batchRole.roleArn, ACCOUNT_ID: this.account,
    };
    const secrets: Record<string, ecs.Secret> = {};
    for (const [prefix, { c, db }] of Object.entries(dbs)) {
      environment[`${prefix}_HOST`] = c.clusterEndpoint.hostname;
      environment[`${prefix}_DBNAME`] = db;
      secrets[`${prefix}_USER`] = ecs.Secret.fromSecretsManager(c.secret!, 'username');
      secrets[`${prefix}_PASSWORD`] = ecs.Secret.fromSecretsManager(c.secret!, 'password');
    }

    const bootstrap = [
      'set -e',
      'pip install --quiet --disable-pip-version-check --root-user-action=ignore boto3 >/dev/null',
      'python -c "import boto3,os; boto3.client(\'s3\').download_file(os.environ[\'CODE_BUCKET\'], os.environ[\'CODE_KEY\'], \'/tmp/code.zip\')"',
      'mkdir -p /app && cd /app && python -m zipfile -e /tmp/code.zip .',
      'pip install --quiet --disable-pip-version-check --root-user-action=ignore . >/dev/null',
      'exec tenantmig "$@"',
    ].join(' && ');

    taskDef.addContainer('tenantmig', {
      image: ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/python:3.13-slim'),
      entryPoint: ['sh', '-c', bootstrap, 'tenantmig'],
      command: ['demo'],
      environment,
      secrets,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'tenantmig' }),
    });

    // Optional monthly budget with email alerts: cdk deploy -c notifyEmail=you@example.com
    const email = props.notifyEmail;
    if (email) new budgets.CfnBudget(this, 'Budget', {
      budget: {
        budgetName: 'tenantmig-sandbox-monthly', budgetType: 'COST', timeUnit: 'MONTHLY',
        budgetLimit: { amount: props.monthlyBudgetUsd, unit: 'USD' },
      },
      notificationsWithSubscribers: [
        ...[50, 80, 100].map((threshold) => ({
          notification: { notificationType: 'ACTUAL', comparisonOperator: 'GREATER_THAN', threshold, thresholdType: 'PERCENTAGE' },
          subscribers: [{ subscriptionType: 'EMAIL', address: email }],
        })),
        {
          notification: { notificationType: 'FORECASTED', comparisonOperator: 'GREATER_THAN', threshold: 100, thresholdType: 'PERCENTAGE' },
          subscribers: [{ subscriptionType: 'EMAIL', address: email }],
        },
      ],
    });

    new CfnOutput(this, 'ClusterName', { value: ecsCluster.clusterName });
    new CfnOutput(this, 'TaskDefinitionArn', { value: taskDef.taskDefinitionArn });
    new CfnOutput(this, 'JobSubnets', {
      value: vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnetIds.join(','),
    });
    new CfnOutput(this, 'JobSecurityGroup', { value: taskSg.securityGroupId });
    new CfnOutput(this, 'LogGroupName', { value: logGroup.logGroupName });
  }
}
