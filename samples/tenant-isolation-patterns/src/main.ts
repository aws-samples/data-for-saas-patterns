import { App, Aspects, Stack, StackProps } from 'aws-cdk-lib';
import { ManagedPolicy, Role } from 'aws-cdk-lib/aws-iam';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';
import { MyApi } from './api/api';
import { MyDynamodb } from './dynamodb/dynamodb';
import { MyIdentity } from './identity/identity';

const myName = 'TenantIsolationPatterns';
const myCell = 'Basic';

export class TenantIsolationPatterns extends Stack {
  public readonly api: MyApi;
  public readonly identity: MyIdentity;
  public readonly role: Role;
  constructor(scope: Construct, id: string, props: StackProps = {}) {
    super(scope, id, props);

    this.identity = new MyIdentity(this, 'Identity', {
      myCell: myCell,
      myName: myName,
    });

    this.role = new Role(this, 'Role', {
      assumedBy: this.identity.oidcPrincipal,
      managedPolicies: [
        ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });

    this.api = new MyApi(this, 'Api', {
      identity: this.identity,
      myCell: myCell,
      myName: myName,
      role: this.role,
    });
  }
}

// for development, use account/region from cdk cli
const devEnv = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION,
};

const app = new App();
Aspects.of(app).add(new AwsSolutionsChecks({}));

const shared = new TenantIsolationPatterns(app, 'Shared', { env: devEnv });
NagSuppressions.addStackSuppressions(shared, [
  { id: 'AwsSolutions-APIG2', reason: 'Request validation not required for proof-of-concept.' },
  { id: 'AwsSolutions-APIG3', reason: 'WAF not required for proof-of-concept.' },
  { id: 'AwsSolutions-APIG6', reason: 'Detailed logging not required for proof-of-concept.' },
  { id: 'AwsSolutions-COG1', reason: 'Password policy not required for proof-of-concept.' },
  { id: 'AwsSolutions-COG2', reason: 'MFA not required for proof-of-concept.' },
  { id: 'AwsSolutions-COG3', reason: 'No need for AdvancedSecurityMode set to ENFORCED.' },
  { id: 'AwsSolutions-COG4', reason: 'Uses request authorizer.' },
  { id: 'AwsSolutions-IAM4', reason: 'Uses AWSLambdaBasicExecutionRole.' },
  { id: 'AwsSolutions-L1', reason: 'Uses NODEJS_LATEST.' },
]);

const dynamodb = new MyDynamodb(app, 'DynamoDB', {
  api: shared.api,
  myCell: myCell,
  myName: myName,
  role: shared.role,
  env: devEnv,
});
NagSuppressions.addStackSuppressions(dynamodb, [
  { id: 'AwsSolutions-IAM4', reason: 'Uses AWSLambdaBasicExecutionRole.' },
  { id: 'AwsSolutions-L1', reason: 'Uses NODEJS_LATEST.' },
]);

app.synth();