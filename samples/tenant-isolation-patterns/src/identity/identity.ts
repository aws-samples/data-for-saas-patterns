import { Aws, CfnOutput, Duration, RemovalPolicy } from 'aws-cdk-lib';
import { AdvancedSecurityMode, ClientAttributes, LambdaVersion, StringAttribute, UserPool, UserPoolOperation } from 'aws-cdk-lib/aws-cognito';
import { OpenIdConnectPrincipal, OpenIdConnectProvider } from 'aws-cdk-lib/aws-iam';
import { Runtime } from 'aws-cdk-lib/aws-lambda';
import { NodejsFunction } from 'aws-cdk-lib/aws-lambda-nodejs';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

interface MyIdentityProps {
  myCell: string;
  myName: string;
}

export class MyIdentity extends Construct {
  public readonly clientId: string;
  public readonly oidcPrincipal: OpenIdConnectPrincipal;
  public readonly oidcProvider: OpenIdConnectProvider;
  public readonly userPoolId: string;
  constructor(scope: Construct, id: string, props: MyIdentityProps) {
    super(scope, id);

    const userPool = new UserPool(this, 'UserPool', {
      userPoolName: props.myName + '-' + props.myCell,
      selfSignUpEnabled: false,
      autoVerify: { email: true },
      signInAliases: { email: true, username: true },
      customAttributes: {
        tenantId: new StringAttribute({ minLen: 1, maxLen: 36, mutable: false }), // Don't let anyone change the tenantId after creation!
      },
      removalPolicy: RemovalPolicy.DESTROY,
      advancedSecurityMode: AdvancedSecurityMode.AUDIT,
    });
    this.userPoolId = userPool.userPoolId;
    userPool.addDomain('CognitoDomain', {
      cognitoDomain: {
        domainPrefix: props.myName.toLowerCase() + '-auth',
      },
    });
    const userPoolClient = userPool.addClient('UserPoolClient', {
      userPoolClientName: props.myName + '-' + props.myCell,
      authFlows: { userPassword: true },
      readAttributes: new ClientAttributes()
        .withStandardAttributes({ email: true })
        .withCustomAttributes(...['tenantId']),
      writeAttributes: new ClientAttributes()
        .withStandardAttributes({ email: true })
        .withCustomAttributes(...['tenantId']),
      accessTokenValidity: Duration.days(1),
      idTokenValidity: Duration.days(1),
      refreshTokenValidity: Duration.days(3),
    });
    this.clientId = userPoolClient.userPoolClientId;
    const logGroup = new LogGroup(this, 'LogGroup', {
      logGroupName: '/'+props.myName+'/'+props.myCell+'/pre-token-generation',
      retention: RetentionDays.ONE_DAY,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const preTokenGenerationFn = new NodejsFunction(this, 'PreTokenGenerationFn', {
      entry: __dirname + '/pre-token-generation.function.ts',
      runtime: Runtime.NODEJS_LATEST,
      handler: 'handler',
      timeout: Duration.seconds(30),
      logGroup: logGroup,
    });
    userPool.addTrigger(UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG, preTokenGenerationFn, LambdaVersion.V2_0);
    const oidcEndpoint = 'https://cognito-idp.' + Aws.REGION + '.amazonaws.com/' + userPool.userPoolId;
    this.oidcProvider = new OpenIdConnectProvider(this, 'MyOidcProvider', {
      url: oidcEndpoint,
      clientIds: [
        userPoolClient.userPoolClientId,
      ],
    });
    const principal = new OpenIdConnectPrincipal(this.oidcProvider)
      .withConditions({
        'ForAllValues:StringEquals': {
          'cognito-identity.amazonaws.com:aud': userPoolClient.userPoolClientId,
        },
      });
    //@ts-ignore
    this.oidcPrincipal = principal.withSessionTags();
    new CfnOutput(this, 'ClientId', { key: 'ClientId', value: this.clientId });
    new CfnOutput(this, 'UserPoolId', { key: 'UserPoolId', value: this.userPoolId });
    new CfnOutput(this, 'OidcProvider', { key: 'OidcProvider', value: this.oidcProvider.openIdConnectProviderIssuer });
  }
}