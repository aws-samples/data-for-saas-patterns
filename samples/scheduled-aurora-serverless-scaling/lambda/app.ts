import { RDSClient, ModifyDBClusterCommand } from "@aws-sdk/client-rds"

const client = new RDSClient();

export const lambdaHandler = async (event: any, context: any) => {
    const desiredMinCapacity = event.desiredMinCapacity;
    const desiredMaxCapacity = event.desiredMaxCapacity;
    const targetDb = event.targetDbCluster;

    const input = {
        ServerlessV2ScalingConfiguration: {
            MinCapacity: desiredMinCapacity,
            MaxCapacity: desiredMaxCapacity
        },
        DBClusterIdentifier: targetDb
    }

    const command = new ModifyDBClusterCommand(input);

    try {
        const data = await client.send(command);
        console.log("Success", data);
    } catch (err) {
        console.log("Error", err);
    }
}