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
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
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
    // One security group per cluster role. A shared group that allows itself lets every cluster
    // reach every other; here only the management cluster may reach the V1 clusters, for postgres_fdw.
    const dbSgFor = (id: string, description: string) => {
      const sg = new ec2.SecurityGroup(this, id, { vpc, description, allowAllOutbound: false });
      sg.addIngressRule(taskSg, ec2.Port.tcp(5432), 'migration job');
      return sg;
    };
    const v1Sg = dbSgFor('V1DbSg', 'V1 Aurora clusters');
    const cellSg = dbSgFor('CellDbSg', 'Cell messaging Aurora cluster');
    const mgmtSg = dbSgFor('MgmtDbSg', 'Management Aurora cluster');
    v1Sg.addIngressRule(mgmtSg, ec2.Port.tcp(5432), 'postgres_fdw from management to V1');
    mgmtSg.addEgressRule(v1Sg, ec2.Port.tcp(5432), 'postgres_fdw from management to V1');

    const cellKey = new kms.Key(this, 'Cell1Key', {
      alias: 'alias/tenantmig/cell-1', enableKeyRotation: true, removalPolicy: RemovalPolicy.DESTROY,
      pendingWindow: Duration.days(7), description: 'Cell 1 key for per-org buckets',
    });

    const cluster = (name: string, version: string, major: string, dbName: string, sg: ec2.SecurityGroup) => {
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
        securityGroups: [sg],
        storageEncrypted: true,
        backup: { retention: Duration.days(1) },
        deletionProtection: false,              // sandbox only, synthetic data
        removalPolicy: RemovalPolicy.DESTROY,
        // Only applies to logs this cluster actually exports. No exports are enabled here
        // (they would carry statement text, including any failing ALTER ROLE), so this is a
        // default for the day someone turns cloudwatchLogsExports on.
        cloudwatchLogsRetention: logs.RetentionDays.ONE_WEEK,
      });
      (c.node.defaultChild as rds.CfnDBCluster).addPropertyOverride(
        'ServerlessV2ScalingConfiguration.SecondsUntilAutoPause', 300);
      return c;
    };

    const dbs = {
      V1_MESSAGING: { c: cluster('V1Messaging', props.v1EngineVersion, '15', 'messaging', v1Sg), db: 'messaging' },
      V1_AUTH: { c: cluster('V1Auth', props.v1EngineVersion, '15', 'auth', v1Sg), db: 'auth' },
      V2_CELL_MESSAGING: { c: cluster('V2Cell1Messaging', props.v2EngineVersion, '18', 'messaging', cellSg), db: 'messaging' },
      V2_MANAGEMENT: { c: cluster('V2Management', props.v2EngineVersion, '18', 'management', mgmtSg), db: 'management' },
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
    // Two task definitions (review finding 6). The admin task runs the setup commands, which need
    // the owner credentials. The job task runs the migration steps with the tenant, migrator and
    // control identities only: no owner password is ever injected into it.
    const runtimePlatform = { cpuArchitecture: ecs.CpuArchitecture.ARM64, operatingSystemFamily: ecs.OperatingSystemFamily.LINUX };
    const taskDef = new ecs.FargateTaskDefinition(this, 'MigrationTask', { cpu: 1024, memoryLimitMiB: 2048, runtimePlatform });
    // The job role is the default only: it can read the code and nothing else. Each tenant's run
    // takes that tenant's own task role instead (tenantmig/task_roles.py, selected by aws-run.sh).
    const jobRole = new iam.Role(this, 'JobRole', { assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com') });
    const jobTaskDef = new ecs.FargateTaskDefinition(this, 'JobTask', { cpu: 1024, memoryLimitMiB: 2048, runtimePlatform, taskRole: jobRole });
    code.grantRead(taskDef.taskRole);
    code.grantRead(jobRole);
    const dataStatements: iam.PolicyStatement[] = [];

    const bucketPrefix = `tm-${this.account}-`;
    dataStatements.push(new iam.PolicyStatement({
      // Bucket lifecycle. No aws:ResourceAccount here: the bucket does not exist yet when
      // CreateBucket is evaluated, so the key would be absent and the condition would deny.
      // Creating or deleting a bucket owned by another account is impossible anyway.
      actions: ['s3:CreateBucket', 's3:DeleteBucket', 's3:PutEncryptionConfiguration',
        's3:PutBucketPublicAccessBlock'],
      resources: [`arn:aws:s3:::${bucketPrefix}*`],
    }));
    dataStatements.push(new iam.PolicyStatement({
      // Reading and writing objects, pinned to this account. Bucket names are globally unique
      // and this prefix is guessable, so without this an attacker could pre-create one of our
      // names in their own account and receive a tenant's content. The job also sends
      // ExpectedBucketOwner on every call, which fails closed even if the policy is loosened.
      actions: ['s3:ListBucket', 's3:GetObject', 's3:PutObject', 's3:DeleteObject'],
      resources: [`arn:aws:s3:::${bucketPrefix}*`, `arn:aws:s3:::${bucketPrefix}*/*`],
      conditions: { StringEquals: { 'aws:ResourceAccount': this.account } },
    }));
    dataStatements.push(new iam.PolicyStatement({ actions: ['s3:ListAllMyBuckets'], resources: ['*'] }));
    cellKey.grantEncryptDecrypt(taskDef.taskRole);

    // Step 8: S3 Batch Operations copies an org's objects out of the V1 shared bucket.
    const sharedBucket = `${bucketPrefix}v1-shared`;
    const batchRole = new iam.Role(this, 'BatchOpsRole', {
      // Confused-deputy guard: only Batch Operations jobs created in this account can assume it.
      assumedBy: new iam.ServicePrincipal('batchoperations.s3.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: { 'aws:SourceArn': `arn:aws:s3:${this.region}:${this.account}:job/*` },
        },
      }),
      description: 'S3 Batch Operations: copy V1 shared-bucket objects into per-org buckets',
    });
    batchRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:GetObjectVersion', 's3:GetObjectTagging', 's3:PutObject', 's3:PutObjectTagging'],
      resources: [`arn:aws:s3:::${bucketPrefix}*/*`],
      conditions: { StringEquals: { 'aws:ResourceAccount': this.account } },
    }));
    batchRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket', 's3:GetBucketLocation'], resources: [`arn:aws:s3:::${bucketPrefix}*`],
      conditions: { StringEquals: { 'aws:ResourceAccount': this.account } },
    }));
    cellKey.grantEncryptDecrypt(batchRole);
    dataStatements.push(new iam.PolicyStatement({
      // s3:CreateJob has no job resource to scope to before the job exists. UpdateJobStatus
      // is needed so a timed-out job is cancelled rather than left running.
      actions: ['s3:CreateJob', 's3:DescribeJob', 's3:UpdateJobStatus'], resources: ['*'],
    }));
    dataStatements.push(new iam.PolicyStatement({
      // Scoped to the one Batch Operations role. (s3:CreateJob does not set iam:PassedToService.)
      actions: ['iam:PassRole'], resources: [batchRole.roleArn],
    }));

    // Both tasks get the same data-plane permissions, as one managed policy.
    const dataPlane = new iam.ManagedPolicy(this, 'DataPlanePolicy', { statements: dataStatements });
    taskDef.taskRole.addManagedPolicy(dataPlane);
    // The admin task provisions tenants: it writes each tenant's secret of logins, and creates and
    // deletes that tenant's task role. Scoped to the sample's names.
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue', 'secretsmanager:CreateSecret', 'secretsmanager:PutSecretValue',
        'secretsmanager:DeleteSecret', 'secretsmanager:DescribeSecret'],
      resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:tenantmig/*`],
    }));
    // Listing secrets has no resource to scope to. Teardown uses it to find the run-time secrets.
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:ListSecrets'], resources: ['*'],
    }));
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['iam:CreateRole', 'iam:PutRolePolicy', 'iam:GetRole', 'iam:ListRolePolicies',
        'iam:DeleteRolePolicy', 'iam:DeleteRole', 'iam:ListRoles'],
      resources: [`arn:aws:iam::${this.account}:role/tenantmig-*`],
    }));
    taskDef.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({ actions: ['iam:ListRoles'], resources: ['*'] }));

    const environment: Record<string, string> = {
      AWS_REGION: this.region, ORG_BUCKET_PREFIX: bucketPrefix, CELL_KMS_KEY_ARN: cellKey.keyArn,
      TENANTMIG_SANDBOX: '1', PLACEMENT_CACHE_TTL_SECONDS: '5', MIGRATION_BUCKET: `${bucketPrefix}migration`,
      // The migrator reads V1 through postgres_fdw as each tenant's own login. The fdw hop stays on
      // sslmode=require: encrypted, but not verified. Verification needs a CA the *client* trusts, and
      // here the fdw client is the managed management instance, not this container. Tested on Aurora
      // PostgreSQL 18: verify-full fails with CERTIFICATE_VERIFY_FAILED both with a file path and with
      // sslrootcert=system. The hop is confined to isolated subnets, and only the management cluster's
      // security group can reach the V1 clusters. See GUIDE.md, security section.
      FDW_V1_MESSAGING_SSLMODE: 'require', FDW_V1_AUTH_SSLMODE: 'require',
      // Cutover refuses while a connector's secret doesn't resolve (needs DescribeSecret on the task role).
      CHECK_CONNECTOR_SECRETS: '1',
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
      'pip install --quiet --disable-pip-version-check --root-user-action=ignore boto3==1.43.112 >/dev/null',
      'python -c "import boto3,os; boto3.client(\'s3\').download_file(os.environ[\'CODE_BUCKET\'], os.environ[\'CODE_KEY\'], \'/tmp/code.zip\')"',
      'mkdir -p /app && cd /app && python -m zipfile -e /tmp/code.zip .',
      'pip install --quiet --disable-pip-version-check --root-user-action=ignore . >/dev/null',
      'exec tenantmig "$@"',
    ].join(' && ');

    const container = {
      image: ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/python:3.13-slim'),
      entryPoint: ['sh', '-c', bootstrap, 'tenantmig'],
      command: ['demo'],
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'tenantmig' }),
    };
    // Passwords come from Secrets Manager, not from the environment (TENANTMIG_SECRETS=aws).
    const runtimeEnvironment = { ...environment, TENANTMIG_SECRETS: 'aws' };
    taskDef.addContainer('tenantmig', { ...container, environment: runtimeEnvironment, secrets });
    jobTaskDef.addContainer('tenantmig', { ...container, environment: runtimeEnvironment });

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
    new CfnOutput(this, 'JobTaskDefinitionArn', { value: jobTaskDef.taskDefinitionArn });
    new CfnOutput(this, 'JobRoleArn', { value: jobRole.roleArn });
    new CfnOutput(this, 'JobSubnets', {
      value: vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnetIds.join(','),
    });
    new CfnOutput(this, 'JobSecurityGroup', { value: taskSg.securityGroupId });
    new CfnOutput(this, 'LogGroupName', { value: logGroup.logGroupName });
  }
}
