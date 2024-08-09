import { Template } from 'aws-cdk-lib/assertions';
import { AuroraGlobalClusterStack } from '../lib/aurora-global-cluster-stack';
import { AuroraRegionalClusterStack } from '../lib/aurora-regional-cluster-stack'
import { FargateTestAppStack } from '../lib/fargate-test-app-stack';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag'

import { Annotations, Match } from 'aws-cdk-lib/assertions';
import { App, Aspects, Stack } from 'aws-cdk-lib';


test('Validate Stack Resources', () => {
    const app = new App();

    const account = app.node.tryGetContext('account') || process.env.CDK_INTEG_ACCOUNT || process.env.CDK_DEFAULT_ACCOUNT;
    const primaryRegion = { account: account, region: 'eu-west-1' };
    const secondaryRegion = { account: account, region: 'eu-west-2' };

    const globalclusterstack = new AuroraGlobalClusterStack(app, "AuroraGlobalCluster", {
        env: primaryRegion,
    });

    const primaryregionstack = new AuroraRegionalClusterStack(app, `AuroraPrimaryCluster-${primaryRegion.region}`, {
        env: primaryRegion, cfnGlobalCluster: globalclusterstack.cfnGlobalCluster, isPrimary: true
    });

    const secondaryregionstack = new AuroraRegionalClusterStack(app, `AuroraSecondaryCluster-${secondaryRegion.region}`, {
        env: secondaryRegion, cfnGlobalCluster: globalclusterstack.cfnGlobalCluster, isPrimary: false
    });

    const globalclustertemplate = Template.fromStack(globalclusterstack);
    const primarytemplate = Template.fromStack(primaryregionstack);
    const secondarytemplate = Template.fromStack(secondaryregionstack);

    globalclustertemplate.resourceCountIs('AWS::RDS::GlobalCluster', 1);

    primarytemplate.resourceCountIs('AWS::EC2::VPC', 1);
    primarytemplate.resourceCountIs('AWS::RDS::DBSubnetGroup', 1);
    primarytemplate.resourceCountIs('AWS::SecretsManager::Secret', 1);
    primarytemplate.resourceCountIs('AWS::RDS::DBCluster', 1);
    primarytemplate.resourceCountIs('AWS::RDS::DBInstance', 1);

    secondarytemplate.resourceCountIs('AWS::EC2::VPC', 1);
    secondarytemplate.resourceCountIs('AWS::RDS::DBSubnetGroup', 1);
    secondarytemplate.resourceCountIs('AWS::SecretsManager::Secret', 1);
    secondarytemplate.resourceCountIs('AWS::RDS::DBCluster', 1);
    secondarytemplate.resourceCountIs('AWS::RDS::DBInstance', 1);
});

describe('cdk-nag AwsSolutions Pack', () => {
    let primaryclusterstack: AuroraRegionalClusterStack;
    let primarytestappstack: FargateTestAppStack;
    let app: App;
    app = new App();

    const account = app.node.tryGetContext('account') || process.env.CDK_INTEG_ACCOUNT || process.env.CDK_DEFAULT_ACCOUNT;
    const primaryRegion = { account: account, region: 'eu-west-1' };

    const globalclusterstack = new AuroraGlobalClusterStack(app, "AuroraGlobalCluster", {
        env: primaryRegion,
        crossRegionReferences: true
    });

    // In this case we can use beforeAll() over beforeEach() since our tests 
    // do not modify the state of the application 
    beforeAll(() => {
        // GIVEN
        primaryclusterstack = new AuroraRegionalClusterStack(app, `AuroraPrimaryCluster-${primaryRegion.region}`, {
            env: primaryRegion, cfnGlobalCluster: globalclusterstack.cfnGlobalCluster, isPrimary: true
        });

        primarytestappstack = new FargateTestAppStack(app, `primary-test-app`, {
            env: primaryRegion,
            endpoint: primaryclusterstack.endpoint,
            port: primaryclusterstack.port,
            vpc: primaryclusterstack.vpc,
            isPrimary: true,
            region: primaryclusterstack.region,
            dbSecurityGroupId: primaryclusterstack.dbSecurityGroupId
        });

        // WHEN
        Aspects.of(primaryclusterstack).add(new AwsSolutionsChecks());
        NagSuppressions.addStackSuppressions(primaryclusterstack, [
            { id: 'AwsSolutions-SMG4', reason: '##Rule : The secret does not have automatic rotation scheduled. ##Suppress Reason## : CfnSecret does not support adding rotation rules and since this is a sample app. For production enable rotation.' },
            { id: 'AwsSolutions-RDS2', reason: '##Rule : The RDS instance or Aurora DB cluster does not have storage encryption enabled. ##Suppress Reason## : This is sample app. For production enable encryption' }
        ]);

        Aspects.of(primarytestappstack).add(new AwsSolutionsChecks());
        NagSuppressions.addStackSuppressions(primarytestappstack, [
            { id: 'AwsSolutions-ECS2', reason: '##Rule : The ECS Task Definition includes a container definition that directly specifies environment variables. ##Suppress Reason## : This is a demo app and hence using env variable. For production use secrets manager.' },
            { id: 'AwsSolutions-ELB2', reason: '##Rule : The ELB does not have access logs enabled. ##Suppress Reason## : The ALB for sample app is created implicitly using ApplicationLoadBalancedFargateService class and hence could not enable . For production use consider enabling access logs.' },
            { id: 'AwsSolutions-EC23', reason: '##Rule :  The Security Group allows for 0.0.0.0/0 or ::/0 inbound access. ##Suppress Reason## : To enable testing the API allowing ingress route on port 80 from public.' },
            { id: 'AwsSolutions-IAM5', reason: '##Rule :  The IAM entity contains wildcard permissions. ##Suppress Reason## : The TaskRole is added when the task is created, Hence unable to use its ARN as this will cause a loop. For production do not use wild cards and create the task role separately and add' }
        ]);
    });

    // THEN
    test('primaryclusterstack : No unsuppressed Warnings', () => {
        const warnings = Annotations.fromStack(primaryclusterstack).findWarning(
            '*',
            Match.stringLikeRegexp('AwsSolutions-.*')
        );
        expect(warnings).toHaveLength(0);
    });

    test('primaryclusterstack : No unsuppressed Errors', () => {
        const errors = Annotations.fromStack(primaryclusterstack).findError(
            '*',
            Match.stringLikeRegexp('AwsSolutions-.*')
        );
        expect(errors).toHaveLength(0);
    });

    test('primarytestappstack : No unsuppressed Warnings', () => {
        const warnings = Annotations.fromStack(primarytestappstack).findWarning(
            '*',
            Match.stringLikeRegexp('AwsSolutions-.*')
        );
        expect(warnings).toHaveLength(0);
    });

    test('primarytestappstack : No unsuppressed Errors', () => {
        const errors = Annotations.fromStack(primarytestappstack).findError(
            '*',
            Match.stringLikeRegexp('AwsSolutions-.*')
        );
        expect(errors).toHaveLength(0);
    });
});