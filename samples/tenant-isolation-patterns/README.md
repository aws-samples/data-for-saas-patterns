# Tenant isolation patterns

## Introduction

These are tenant isolation patterns using [AssumeRoleWithWebIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html) and [Attribute Based Access Control (ABAC)](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction_attribute-based-access-control.html).

Cognito is used for the identity provider, along with pre-token-generation to include the tags necessary for `AssumeRoleWithWebIdentity`.

## Deployment

Install pre-requisite packages:

```bash
yarn install
```

Deploy the shared stack, and any other specific stack you'd like to use.

Ie. To deploy the DynamoDB example:

```bash
cdk deploy Shared DynamoDB
```

## Testing

To test a stack, source the test_interface and run the corresponding test.

Ie. For DynamoDB:

```bash
source test_interface.sh
test_dynamodb
```

## Clean up

```bash
cdk destroy --all
```
