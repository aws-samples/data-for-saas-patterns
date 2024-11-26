import 'source-map-support/register';
import { AssumeRoleWithWebIdentityCommand, AssumeRoleWithWebIdentityCommandInput, STSClient } from '@aws-sdk/client-sts';
import { APIGatewayAuthorizerResult, APIGatewayRequestAuthorizerEvent, APIGatewayRequestAuthorizerEventHeaders } from 'aws-lambda/trigger/api-gateway-authorizer';
import { jwtDecode } from 'jwt-decode';

const stsClient = new STSClient();

const defaultDenyAllPolicy: APIGatewayAuthorizerResult = {
  principalId: 'user',
  policyDocument: {
    Version: '2012-10-17',
    Statement: [
      {
        Action: '*',
        Effect: 'Deny',
        Resource: '*',
      },
    ],
  },
};

export const handler = async (event: APIGatewayRequestAuthorizerEvent): Promise<APIGatewayAuthorizerResult> => {
  try {
    const headers: APIGatewayRequestAuthorizerEventHeaders = event.headers as APIGatewayRequestAuthorizerEventHeaders;
    const parsed = JSON.parse(JSON.stringify(headers));
    const authorization: string = parsed.Authorization.split(' ');
    if (authorization[0] != 'Bearer') {
      return defaultDenyAllPolicy;
    }
    const jwt = authorization[1];
    console.log('JWT: ', jwtDecode(jwt));
    const decodedJwt = JSON.parse(JSON.stringify(jwtDecode(jwt)), function(key, value) {
      if ( key == 'custom:tenantId' ) {
        this.tenantId = value;
      } else {return value;}
    });
    const tenantId = decodedJwt.tenantId;
    const input: AssumeRoleWithWebIdentityCommandInput = {
      RoleArn: process.env.ASSUMED_ROLE,
      WebIdentityToken: jwt,
      RoleSessionName: tenantId,
    };
    const command = new AssumeRoleWithWebIdentityCommand(input);
    const credentials = await stsClient.send(command);
    console.log(credentials);

    const context = {
      tenantId: tenantId,
      accessKeyId: credentials.Credentials?.AccessKeyId,
      secretAccessKey: credentials.Credentials?.SecretAccessKey,
      sessionToken: credentials.Credentials?.SessionToken,
    };
    console.log('Context: ', JSON.stringify(context));

    const arn = event.methodArn.split('/');

    const response: APIGatewayAuthorizerResult = {
      principalId: 'user',
      context,
      policyDocument: {
        Version: '2012-10-17',
        Statement: [
          {
            Action: 'execute-api:Invoke',
            Effect: 'Allow',
            Resource: arn[0]+'/*',
          },
        ],
      },
    };
    console.log('Response: ', JSON.stringify(response));
    return response;
  } catch (error) {
    console.log(error);
    return defaultDenyAllPolicy;
  }
};
