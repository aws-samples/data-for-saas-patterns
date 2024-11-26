import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';
import { APIGatewayProxyEvent, APIGatewayProxyResult } from 'aws-lambda';
import { StatusCodes } from 'http-status-codes';

const tableName = process.env.TABLE_NAME;

export const fail = async (event: APIGatewayProxyEvent): Promise<APIGatewayProxyResult> => {
  console.log(event);
  const client = new DynamoDBClient({
    credentials: {
      accessKeyId: event.requestContext.authorizer?.accessKeyId,
      secretAccessKey: event.requestContext.authorizer?.secretAccessKey,
      sessionToken: event.requestContext.authorizer?.sessionToken,
    },
  });
  const docClient = DynamoDBDocumentClient.from(client);
  const tenantId=event.requestContext.authorizer?.tenantId;
  console.log(tenantId);
  console.log(tableName);
  const command = new PutCommand({
    TableName: tableName,
    Item: {
      pk: tenantId+'foo', // trying to put another tenant's item
      sk: 'Stevie Ray Vaughan',
      guitar: 'Stratocaster',
    },
  });
  // We expect this to throw an error, because tenant isolation should not let us write to another tenant
  try {
    await docClient.send(command);
    return {
      statusCode: StatusCodes.INTERNAL_SERVER_ERROR,
      body: JSON.stringify({ message: 'Unexpected behaviour. Tenant isolation breached.' }),
    };
  } catch (error) {
    return {
      statusCode: StatusCodes.OK,
      body: JSON.stringify({ message: 'Expected behaviour. Tenant isolation worked. Cant write to another tenant.' }),
    };
  }
};

export const success = async (event: APIGatewayProxyEvent): Promise<APIGatewayProxyResult> => {
  console.log(event);
  const client = new DynamoDBClient({
    credentials: {
      accessKeyId: event.requestContext.authorizer?.accessKeyId,
      secretAccessKey: event.requestContext.authorizer?.secretAccessKey,
      sessionToken: event.requestContext.authorizer?.sessionToken,
    },
  });
  const docClient = DynamoDBDocumentClient.from(client);
  const tenantId=event.requestContext.authorizer?.tenantId;
  console.log(tenantId);
  console.log(tableName);
  const command = new PutCommand({
    TableName: tableName,
    Item: {
      pk: tenantId,
      sk: 'Duane Allman',
      guitar: 'Les Paul',
    },
  });
  try {
    await docClient.send(command);
    return {
      statusCode: StatusCodes.OK,
      body: JSON.stringify({ message: 'Expected behaviour. Can write to our own tenant.' }),
    };
  } catch (error) {
    return {
      statusCode: StatusCodes.INTERNAL_SERVER_ERROR,
      body: JSON.stringify({ message: 'Unexpected behaviour. Cant write to our own tenant.', error: error }),
    };
  }
};
