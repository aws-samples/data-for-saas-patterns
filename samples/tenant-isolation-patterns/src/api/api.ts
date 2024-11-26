import { Aws, CfnOutput, Duration, RemovalPolicy } from 'aws-cdk-lib';
import { AccessLogField, AccessLogFormat, ApiKeySourceType, HttpIntegration, IdentitySource, LambdaIntegration, LogGroupLogDestination, RequestAuthorizer, RestApi } from 'aws-cdk-lib/aws-apigateway';
import { HttpMethod } from 'aws-cdk-lib/aws-apigatewayv2';
import { Role } from 'aws-cdk-lib/aws-iam';
import { Runtime } from 'aws-cdk-lib/aws-lambda';
import { NodejsFunction } from 'aws-cdk-lib/aws-lambda-nodejs';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { MyIdentity } from '../identity/identity';

interface MyApiProps {
  identity: MyIdentity;
  myCell: string;
  myName: string;
  role: Role;
}

export class MyApi extends Construct {
  public readonly api: RestApi;
  public readonly authorizer: RequestAuthorizer;
  constructor(scope: Construct, id: string, props: MyApiProps) {
    super(scope, id);

    const logGroup = new LogGroup(this, 'LogGroup', {
      logGroupName: '/' + props.myName + '/' + props.myCell + '/api',
      retention: RetentionDays.ONE_DAY,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const authorizerFn = new NodejsFunction(this, 'AuthorizerFn', {
      entry: __dirname + '/authorizer.function.ts',
      runtime: Runtime.NODEJS_LATEST,
      handler: 'handler',
      timeout: Duration.seconds(30),
      logGroup: logGroup,
      environment: {
        USERPOOL_ID: props.identity.userPoolId,
        CLIENT_ID: props.identity.clientId,
        ASSUMED_ROLE: props.role.roleArn,
      },
    });
    this.authorizer = new RequestAuthorizer(this, 'Authorizer', {
      handler: authorizerFn,
      identitySources: [IdentitySource.header('Authorization')],
    });

    this.api = new RestApi(this, 'Api', {
      restApiName: props.myName + '-' + props.myCell,
      cloudWatchRoleRemovalPolicy: RemovalPolicy.DESTROY,
      apiKeySourceType: ApiKeySourceType.AUTHORIZER,
      deployOptions: {
        accessLogDestination: new LogGroupLogDestination(logGroup),
        accessLogFormat: AccessLogFormat.custom(JSON.stringify({
          path: AccessLogField.contextResourcePath(),
          requestId: AccessLogField.contextRequestId(),
          sourceIp: AccessLogField.contextIdentitySourceIp(),
          method: AccessLogField.contextHttpMethod(),
          authorizerLatency: AccessLogField.contextAuthorizerIntegrationLatency(),
          integrationLatency: AccessLogField.contextIntegrationLatency(),
          responseLatency: AccessLogField.contextResponseLatency(),
          authorizerStatus: AccessLogField.contextAuthorizerStatus(),
          integrationStatus: AccessLogField.contextIntegrationStatus(),
          status: AccessLogField.contextStatus(),
          transactionId: AccessLogField.contextAuthorizer('transactionId'),
          tenantId: AccessLogField.contextAuthorizer('tenantId'),
          tier: AccessLogField.contextAuthorizer('tier'),
          role: AccessLogField.contextAuthorizer('role'),
          stackName: Aws.STACK_NAME,
        }),
        ),
      },
    });
    this.api.root.addMethod('ANY', new HttpIntegration('https://github.com/aws-samples/data-for-saas-patterns'), { authorizer: this.authorizer });
    new CfnOutput(this, 'ApiUrl', { key: 'ApiUrl', value: this.api.url });
  }
  addTest(path: string, failFn: NodejsFunction, successFn: NodejsFunction) {
    const pathResource = this.api.root.addResource(path);
    const successPath = pathResource.addResource('success');
    const successLambdaIntegration = new LambdaIntegration(successFn);
    successPath.addMethod(HttpMethod.ANY, successLambdaIntegration, { authorizer: this.authorizer });
    const failPath = pathResource.addResource('fail');
    const failLambdaIntegration = new LambdaIntegration(failFn);
    failPath.addMethod(HttpMethod.ANY, failLambdaIntegration, { authorizer: this.authorizer });
  }
}