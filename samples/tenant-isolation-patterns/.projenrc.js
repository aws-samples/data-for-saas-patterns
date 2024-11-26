const { awscdk } = require('projen');
const project = new awscdk.AwsCdkTypeScriptApp({
  cdkVersion: '2.171.0',
  defaultReleaseBranch: 'main',
  github: false,
  name: 'tenant-isolation-patterns',
  deps: [
    '@aws-sdk/client-sts',
    '@types/aws-lambda',
    'aws-jwt-verify',
    'aws-lambda',
    '@aws-sdk/client-dynamodb',
    '@aws-sdk/lib-dynamodb',
    'cdk-nag',
    'http-status-codes',
    'jwt-decode',
    'source-map-support',
  ],

  // deps: [],                /* Runtime dependencies of this module. */
  // description: undefined,  /* The description is just a string that helps people understand the purpose of the package. */
  // devDeps: [],             /* Build dependencies for this module. */
  // packageName: undefined,  /* The "name" in package.json. */
});
project.tasks.tryFind('deploy')?.reset('cdk deploy --require-approval=never');
project.tasks.tryFind('destroy')?.reset('cdk destroy --force');
project.synth();