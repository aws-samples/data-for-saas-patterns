import * as cdk from 'aws-cdk-lib';
import { Rule, RuleTargetInput, Schedule } from 'aws-cdk-lib/aws-events';
import { LambdaFunction } from 'aws-cdk-lib/aws-events-targets';
import { Policy, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { NodejsFunction } from 'aws-cdk-lib/aws-lambda-nodejs';
import { Construct } from 'constructs';

export class ScheduledAutoscalingAmazonAuroraServerlessStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const scheduleUp = this.node.tryGetContext('scheduleUp');
    const desiredCapacityUpMin = this.node.tryGetContext('desiredCapacityUpMin');
    const desiredCapacityUpMax = this.node.tryGetContext('desiredCapacityUpMax');
    const scheduleDown = this.node.tryGetContext('scheduleDown');
    const desiredCapacityDownMin = this.node.tryGetContext('desiredCapacityDownMin');
    const desiredCapacityDownMax = this.node.tryGetContext('desiredCapacityDownMax');
    const dbClusterId = this.node.tryGetContext('dbClusterId');

    const scalingFunction = new NodejsFunction(this, 'ScalingFunction', {
      entry: 'lambda/app.ts',
      handler: 'lambdaHandler',
      runtime: cdk.aws_lambda.Runtime.NODEJS_20_X,
      bundling: {
        minify: true,
        externalModules: ['@aws-sdk/client-rds'],
      },
    });
    scalingFunction.role?.attachInlinePolicy(new Policy(this, 'ScalingFunctionPolicy', {
      statements: [new PolicyStatement({
          actions: [
            'rds:ModifyDBCluster'
          ],
          resources: [
            `arn:aws:rds:${this.region}:${this.account}:cluster:${dbClusterId}`
          ]
        })]
    }));

    const role = new Role(this, 'EBScheduleRole', {
      assumedBy: new ServicePrincipal('events.amazonaws.com')
    });
    scalingFunction.grantInvoke(role);

    if (scheduleUp && desiredCapacityUpMin && desiredCapacityUpMax) {
      const ebScheduleUp = new Rule(this, 'EBScheduleUp', {
        schedule: Schedule.expression(scheduleUp)
      });

      ebScheduleUp.addTarget(new LambdaFunction(scalingFunction, {
        event: RuleTargetInput.fromObject({
          desiredMinCapacity: desiredCapacityUpMin,
          desiredMaxCapacity: desiredCapacityUpMax,
          targetDbCluster: dbClusterId
        })
      }));
    }

    if (scheduleDown && desiredCapacityDownMin && desiredCapacityDownMax) {
      const ebScheduleDown = new Rule(this, 'EBScheduleDown', {
        schedule: Schedule.expression(scheduleDown)
      });

      ebScheduleDown.addTarget(new LambdaFunction(scalingFunction, {
        event: RuleTargetInput.fromObject({
          desiredMinCapacity: desiredCapacityDownMin,
          desiredMaxCapacity: desiredCapacityDownMax,
          targetDbCluster: dbClusterId
        })
      }));
    }
  }
}
