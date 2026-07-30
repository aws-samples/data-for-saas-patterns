# SPDX-License-Identifier: MIT-0
"""
Custom Resource Provider — AgentCore Gateway lifecycle management.

Used with CDK's cr.Provider — returns dict (framework handles cfnresponse).
"""

import json
import time
import boto3

client = boto3.client("bedrock-agentcore-control")


def handler(event, context):
    request_type = event["RequestType"]
    props = event["ResourceProperties"]

    if request_type == "Delete":
        gateway_id = event.get("PhysicalResourceId", "")
        if gateway_id and gateway_id != "NONE":
            try:
                # Must delete all targets before deleting the gateway
                targets = client.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
                for target in targets:
                    target_id = target.get("targetId", "")
                    if target_id:
                        try:
                            client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
                            print(f"Deleted target: {target_id}")
                        except Exception as te:
                            print(f"Delete target warning: {te}")

                # Wait for targets to be deleted
                if targets:
                    time.sleep(10)

                client.delete_gateway(gatewayIdentifier=gateway_id)
                print(f"Deleted gateway: {gateway_id}")
            except Exception as e:
                print(f"Delete gateway warning: {e}")
        return {"PhysicalResourceId": gateway_id}

    # Create or Update
    gateway_name = props["GatewayName"]
    role_arn = props["RoleArn"]
    discovery_url = props["DiscoveryUrl"]
    allowed_audience = props["AllowedAudience"]
    interceptor_arn = props["InterceptorArn"]
    lambda_target_arn = props["LambdaTargetArn"]
    mcp_endpoint = props["McpServerEndpoint"]
    tool_schema = props["ToolSchema"]

    # Step 1: Create Gateway
    gateway_response = client.create_gateway(
        name=gateway_name,
        roleArn=role_arn,
        protocolType="MCP",
        protocolConfiguration={"mcp": {"supportedVersions": ["2025-11-25"]}},
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedAudience": [allowed_audience],
            }
        },
        interceptorConfigurations=[{
            "interceptor": {"lambda": {"arn": interceptor_arn}},
            "interceptionPoints": ["REQUEST"],
            "inputConfiguration": {"passRequestHeaders": True},
        }],
        exceptionLevel="DEBUG",
    )

    gateway_id = gateway_response["gatewayId"]
    gateway_url = gateway_response["gatewayUrl"]
    print(f"Gateway created: {gateway_id}")

    # Wait for READY
    for _ in range(30):
        status = client.get_gateway(gatewayIdentifier=gateway_id)["status"]
        if status == "READY":
            break
        time.sleep(10)

    # Step 2: Register Lambda target
    client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name="query-databricks",
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": lambda_target_arn,
                    "toolSchema": {
                        "inlinePayload": json.loads(tool_schema),
                    },
                }
            }
        },
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
        metadataConfiguration={
            "allowedRequestHeaders": ["x-tenant-id"],
        },
    )
    print("Lambda target registered: query-databricks")

    # Step 3: Register MCP Server target (DYNAMIC listing mode)
    client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name="databricks-mcp",
        targetConfiguration={
            "mcp": {
                "mcpServer": {
                    "endpoint": mcp_endpoint,
                    "listingMode": "DYNAMIC",
                }
            }
        },
        metadataConfiguration={
            "allowedRequestHeaders": ["x-tenant-id"],
        },
    )
    print("MCP Server target registered: databricks-mcp (DYNAMIC)")

    return {
        "PhysicalResourceId": gateway_id,
        "Data": {
            "GatewayId": gateway_id,
            "GatewayUrl": gateway_url,
        },
    }
