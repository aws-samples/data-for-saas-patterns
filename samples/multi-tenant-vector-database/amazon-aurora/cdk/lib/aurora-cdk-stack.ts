import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Duration } from 'aws-cdk-lib'
import { SubnetType, Vpc, SecurityGroup, IpAddresses } from 'aws-cdk-lib/aws-ec2';
import { FlowLogMaxAggregationInterval, FlowLogTrafficType } from 'aws-cdk-lib/aws-ec2';
import { AuroraCapacityUnit, CfnDBSubnetGroup } from 'aws-cdk-lib/aws-rds';
import { DatabaseCluster, DatabaseClusterEngine, AuroraPostgresEngineVersion, ClusterInstance } from 'aws-cdk-lib/aws-rds';
export class AuroraCdkStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const databasename = 'postgres';

    // VPC
    const vpc = new Vpc(this, 'Vpc', {
      ipAddresses: IpAddresses.cidr('10.0.0.0/16'),
      natGateways: 0,
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'aurora_isolated_',
          subnetType: SubnetType.PRIVATE_ISOLATED
        },
        {
          cidrMask: 24,
          name: 'public',
          subnetType: SubnetType.PUBLIC
        }
      ]
    });

    vpc.addFlowLog('FlowLogCloudWatch', {
      trafficType: FlowLogTrafficType.REJECT,
      maxAggregationInterval: FlowLogMaxAggregationInterval.ONE_MINUTE,
    });

    // Security group
    const dbSecurityGroup: SecurityGroup = new SecurityGroup(this, 'db-security-group', {
      securityGroupName: 'db-security-group',
      description: 'db-security-group',
      allowAllOutbound: true,
      vpc: vpc,
    });

    // DB Subnet Group
    const subnetIds: string[] = [];
    vpc.isolatedSubnets.forEach((subnet, index) => { subnetIds.push(subnet.subnetId); });

    const dbSubnetGroup: CfnDBSubnetGroup = new CfnDBSubnetGroup(this, 'AuroraSubnetGroup', {
      dbSubnetGroupDescription: 'Subnet group to access aurora',
      dbSubnetGroupName: 'aurora-serverless-subnet-group',
      subnetIds
    });

    const dbCluster = new DatabaseCluster(this, 'DbCluster', {
      engine: DatabaseClusterEngine.auroraPostgres({
        version: AuroraPostgresEngineVersion.VER_16_2,
      }),
      iamAuthentication: true,
      storageEncrypted: true,
      deletionProtection: true,
      writer: ClusterInstance.serverlessV2('writer', {
      }),
      vpc: vpc,
      securityGroups: [dbSecurityGroup],
      vpcSubnets: vpc.selectSubnets({
        subnetType: SubnetType.PRIVATE_ISOLATED,
      }),
      serverlessV2MaxCapacity: AuroraCapacityUnit.ACU_4,
      serverlessV2MinCapacity: AuroraCapacityUnit.ACU_2,
      port: 5432, // use port 5432 instead of 3306
      enableDataApi: true
    })

    dbCluster.addRotationSingleUser({
      automaticallyAfter: Duration.days(30),
      excludeCharacters: ' %\'@',
      vpcSubnets: vpc.selectSubnets({
        subnetType: SubnetType.PRIVATE_ISOLATED,
      }),
    })

    new cdk.CfnOutput(this, 'Aurora Cluster ARN', { value: dbCluster.clusterArn });

  }
}