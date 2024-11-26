import { Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import { AttributeType, TableV2 } from 'aws-cdk-lib/aws-dynamodb';
import { Effect, PolicyStatement, Role } from 'aws-cdk-lib/aws-iam';
import { Runtime } from 'aws-cdk-lib/aws-lambda';
import { NodejsFunction } from 'aws-cdk-lib/aws-lambda-nodejs';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { MyApi } from '../api/api';

const description = 'dynamodb';

interface MyDynamodbProps extends StackProps {
  api: MyApi;
  myCell: string;
  myName: string;
  role: Role;
}

export class MyDynamodb extends Stack {
  constructor(scope: Construct, id: string, props: MyDynamodbProps) {
    super(scope, id, props);

    const logGroup = new LogGroup(this, 'LogGroup', {
      logGroupName: '/' + props.myName + '/' + props.myCell + '/' + description,
      retention: RetentionDays.ONE_DAY,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const table = new TableV2(this, 'DynamoDB', {
      partitionKey: {
        name: 'pk',
        type: AttributeType.STRING,
      },
      sortKey: {
        name: 'sk',
        type: AttributeType.STRING,
      },
      removalPolicy: RemovalPolicy.DESTROY,
      pointInTimeRecovery: true,
    });

    const successFn = new NodejsFunction(this, 'SuccessFn', {
      entry: __dirname + '/dynamodb.function.ts',
      runtime: Runtime.NODEJS_LATEST,
      handler: 'success',
      timeout: Duration.seconds(30),
      logGroup: logGroup,
      environment: {
        TABLE_NAME: table.tableName,
      },
    });

    const failFn = new NodejsFunction(this, 'FailFn', {
      entry: __dirname + '/dynamodb.function.ts',
      runtime: Runtime.NODEJS_LATEST,
      handler: 'fail',
      timeout: Duration.seconds(30),
      logGroup: logGroup,
      environment: {
        TABLE_NAME: table.tableName,
      },
    });

    props.api.addTest('dynamodb', failFn, successFn);
    props.role.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: [
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
          'dynamodb:ConditionCheckItem',
          'dynamodb:DeleteItem',
          'dynamodb:DescribeTable',
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:Query',
          'dynamodb:Scan',
          'dynamodb:UpdateItem',
        ],
        resources: [
          table.tableArn,
        ],
        conditions: {
          'ForAllValues:StringEquals': {
            'dynamodb:LeadingKeys': [
              '${aws:PrincipalTag/tenantId}',
            ],
          },
        },
      }),
    );
  }
}