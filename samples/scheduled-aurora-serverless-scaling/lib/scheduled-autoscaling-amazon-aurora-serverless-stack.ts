import * as cdk from 'aws-cdk-lib';
import { CfnSchedule } from 'aws-cdk-lib/aws-scheduler';
import { Role, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export class ScheduledAutoscalingAmazonAuroraServerlessStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const scheduleUp = this.node.tryGetContext('scheduleUp');
    const desiredCapacityUpMin: number = this.node.tryGetContext('desiredCapacityUpMin');
    const desiredCapacityUpMax: number = this.node.tryGetContext('desiredCapacityUpMax');
    const scheduleDown = this.node.tryGetContext('scheduleDown');
    const desiredCapacityDownMin: number = this.node.tryGetContext('desiredCapacityDownMin');
    const desiredCapacityDownMax: number = this.node.tryGetContext('desiredCapacityDownMax');
    const dbClusterId = this.node.tryGetContext('dbClusterId');

    const schedulerRole = new Role(this, 'SchedulerRole', {
      assumedBy: new ServicePrincipal('scheduler.amazonaws.com')
    });

    schedulerRole.addToPolicy(new PolicyStatement({
      actions: ['rds:ModifyDBCluster'],
      resources: [`arn:aws:rds:${this.region}:${this.account}:cluster:${dbClusterId}`]
    }));

    if (scheduleUp && desiredCapacityUpMin && desiredCapacityUpMax) {
      new CfnSchedule(this, 'ScaleUpSchedule', {
        flexibleTimeWindow: {
          mode: 'OFF'
        },
        scheduleExpression: scheduleUp,
        target: {
          arn: `arn:aws:scheduler:::aws-sdk:rds:modifyDBCluster`,
          roleArn: schedulerRole.roleArn,
          input: `{"DbClusterIdentifier":"${dbClusterId}","ServerlessV2ScalingConfiguration":{"MinCapacity":${desiredCapacityUpMin},"MaxCapacity":${desiredCapacityUpMax}}}`,
          retryPolicy: {
            maximumRetryAttempts: 3
          }
        },
        name: `${id}-scale-up`,
        description: 'Scale up Aurora Serverless cluster during business hours',
        state: 'ENABLED'
      });
    }

    if (scheduleDown && desiredCapacityDownMin && desiredCapacityDownMax) {
      new CfnSchedule(this, 'ScaleDownSchedule', {
        flexibleTimeWindow: {
          mode: 'OFF'
        },
        scheduleExpression: scheduleDown,
        target: {
          arn: `arn:aws:scheduler:::aws-sdk:rds:modifyDBCluster`,
          roleArn: schedulerRole.roleArn,
          input: `{"DbClusterIdentifier":"${dbClusterId}","ServerlessV2ScalingConfiguration":{"MinCapacity":${desiredCapacityDownMin},"MaxCapacity":${desiredCapacityDownMax}}}`,
          retryPolicy: {
            maximumRetryAttempts: 3
          }
        },
        name: `${id}-scale-down`,
        description: 'Scale down Aurora Serverless cluster during non-business hours',
        state: 'ENABLED'
      });
    }
  }
}