import { Construct } from 'constructs';
import { StackProps, Stack, RemovalPolicy, Duration } from 'aws-cdk-lib'
import { CfnGlobalCluster } from 'aws-cdk-lib/aws-rds';
import { Key, KeySpec } from 'aws-cdk-lib/aws-kms';

export class AuroraGlobalClusterStack extends Stack {
    public readonly cfnGlobalCluster: CfnGlobalCluster;
    constructor(scope: Construct, id: string, props?: StackProps) {
        super(scope, id, props);

        // Aurora Global Cluster
        const cfnGlobalCluster = new CfnGlobalCluster(this, 'AuroraGlobalCluster', {
            engine: 'aurora-postgresql',
            engineVersion: '14.6',
            globalClusterIdentifier: 'aurora-serverless-global-cluster',
        });

        const key = new Key(this, 'Key', {
            keySpec: KeySpec.SYMMETRIC_DEFAULT,
            description: "DB Encryption Key",
            alias: "db-encryption-key",
            removalPolicy: RemovalPolicy.DESTROY,
            pendingWindow: Duration.days(7),
            enabled: true,
        });

        this.cfnGlobalCluster = cfnGlobalCluster;
    }
}

export interface GlobalClusterProps extends StackProps {
    cfnGlobalCluster: CfnGlobalCluster,
    isPrimary?: boolean
}