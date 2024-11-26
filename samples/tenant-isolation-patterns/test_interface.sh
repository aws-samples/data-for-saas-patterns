#!/bin/bash

STACK_NAME='Shared'
USER_NAME='testuser'$((1 + $RANDOM % 100))
TENANT_ID='tenant'$((1 + $RANDOM % 10000))
PASSWORD='Password123!'
API_URL=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' --output text)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --query 'Stacks[0].Outputs[?OutputKey==`ClientId`].OutputValue' --output text)
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name $STACK_NAME --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' --output text)
OIDC_PROVIDER='https://'$(aws cloudformation describe-stacks --stack-name $STACK_NAME --query 'Stacks[0].Outputs[?OutputKey==`OidcProvider`].OutputValue' --output text)'/.well-known/openid-configuration'
aws cognito-idp admin-create-user --user-pool-id $USER_POOL_ID --user-attributes Name=custom:tenantId,Value=$TENANT_ID --username $USER_NAME > /dev/null
aws cognito-idp admin-set-user-password --user-pool-id $USER_POOL_ID --username $USER_NAME --password $PASSWORD --permanent
ID_TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --auth-parameters USERNAME=$USER_NAME,PASSWORD=$PASSWORD --client-id $CLIENT_ID|grep 'IdToken'|cut -d ':' -f 2|cut -d '"' -f 2)

test_dynamodb() {
    curl -X GET -H Authorization:\ Bearer\ $ID_TOKEN $API_URL/dynamodb/success
    curl -X GET -H Authorization:\ Bearer\ $ID_TOKEN $API_URL/dynamodb/fail
}


