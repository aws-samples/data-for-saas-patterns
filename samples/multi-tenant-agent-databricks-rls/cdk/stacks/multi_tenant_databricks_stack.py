# SPDX-License-Identifier: MIT-0
"""
MainStack — Single consolidated stack for the multi-tenant Databricks agent sample.

Creates:
  - Cognito User Pool + test users (tenant-001, tenant-002)
  - DynamoDB table for WIF config (per-tenant client_id lookup)
  - Lambda layers (pre-built by scripts/build_layers.sh)
  - Databricks Query Lambda + IAM role
  - Interceptor Lambda + IAM role
  - AgentCore Gateway (via custom resource)
  - Gateway targets (Lambda + MCP Server DYNAMIC)

Prerequisites:
  Run `bash scripts/build_layers.sh` before `cdk deploy`.
"""

import json

from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    RemovalPolicy,
    CustomResource,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_iam as iam,
    custom_resources as cr,
)
from constructs import Construct


USER_SETUP_CODE = """
import json
import boto3
import cfnresponse

def handler(event, context):
    try:
        if event['RequestType'] == 'Delete':
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return
        client = boto3.client('cognito-idp')
        pool_id = event['ResourceProperties']['UserPoolId']
        users = event['ResourceProperties']['Users']
        for user in users:
            try:
                client.admin_create_user(
                    UserPoolId=pool_id, Username=user['email'],
                    UserAttributes=[
                        {'Name': 'email', 'Value': user['email']},
                        {'Name': 'email_verified', 'Value': 'true'},
                        {'Name': 'custom:tenant_id', 'Value': user['tenant_id']},
                    ],
                    MessageAction='SUPPRESS',
                )
            except client.exceptions.UsernameExistsException:
                pass
            client.admin_set_user_password(
                UserPoolId=pool_id, Username=user['email'],
                Password=user['password'], Permanent=True,
            )
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {'Status': 'Users created'})
    except Exception as e:
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
"""


class MultiTenantDatabricksStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 databricks_host: str,
                 databricks_warehouse_id: str,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ═══════════════════════════════════════════════════════════════
        # COGNITO
        # ═══════════════════════════════════════════════════════════════

        user_pool = cognito.UserPool(self, "UserPool",
            user_pool_name="multi-tenant-databricks-agent-pool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            custom_attributes={
                "tenant_id": cognito.StringAttribute(mutable=False),
            },
        )

        client = user_pool.add_client("AppClient",
            user_pool_client_name="multi-tenant-databricks-agent-client",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            generate_secret=False,
        )

        # Create test users via custom resource
        user_setup_role = iam.Role(self, "UserSetupRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        user_setup_role.add_to_policy(iam.PolicyStatement(
            actions=["cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword"],
            resources=[user_pool.user_pool_arn],
        ))

        user_setup_fn = _lambda.Function(self, "UserSetupFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_inline(USER_SETUP_CODE),
            role=user_setup_role,
            timeout=Duration.seconds(30),
        )

        provider = cr.Provider(self, "UserSetupProvider", on_event_handler=user_setup_fn)

        CustomResource(self, "TestUsers",
            service_token=provider.service_token,
            properties={
                "UserPoolId": user_pool.user_pool_id,
                "Users": [
                    {"email": "testuser@example.com", "tenant_id": "tenant-001", "password": "Workshop@123!"},
                    {"email": "testuser2@example.com", "tenant_id": "tenant-002", "password": "Workshop@123!"},
                ],
            },
        )

        # ═══════════════════════════════════════════════════════════════
        # DYNAMODB (WIF Config — per-tenant client_id lookup)
        # ═══════════════════════════════════════════════════════════════

        wif_config_table = dynamodb.Table(self, "WifConfigTable",
            table_name="multi-tenant-databricks-wif-config",
            partition_key=dynamodb.Attribute(
                name="tenant_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery=True,
        )

        # ═══════════════════════════════════════════════════════════════
        # LAMBDA LAYERS (pre-built by scripts/build_layers.sh)
        # ═══════════════════════════════════════════════════════════════

        query_layer = _lambda.LayerVersion(self, "QueryLayer",
            layer_version_name="multi-tenant-databricks-query-deps",
            description="requests + boto3 extensions (Linux x86_64)",
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            code=_lambda.Code.from_asset("../build/query-layer"),
        )

        interceptor_layer = _lambda.LayerVersion(self, "InterceptorLayer",
            layer_version_name="multi-tenant-databricks-interceptor-deps",
            description="PyJWT + requests (Linux x86_64)",
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            code=_lambda.Code.from_asset("../build/interceptor-layer"),
        )

        # ═══════════════════════════════════════════════════════════════
        # DATABRICKS QUERY LAMBDA
        # ═══════════════════════════════════════════════════════════════

        query_role = iam.Role(self, "QueryRole",
            role_name="multi-tenant-agent-databricks-query-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )

        # Allow STS GetWebIdentityToken for WIF
        query_role.add_to_policy(iam.PolicyStatement(
            actions=["sts:GetWebIdentityToken"],
            resources=["*"],
        ))

        query_function = _lambda.Function(self, "QueryFunction",
            function_name="multi-tenant-agent-databricks-query",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset("../lambda/databricks_query"),
            role=query_role,
            layers=[query_layer],
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "WIF_CONFIG_TABLE": wif_config_table.table_name,
                "DATABRICKS_HOST": databricks_host,
                "DATABRICKS_WAREHOUSE_ID": databricks_warehouse_id,
            },
        )
        wif_config_table.grant_read_data(query_function)

        # ═══════════════════════════════════════════════════════════════
        # INTERCEPTOR LAMBDA
        # ═══════════════════════════════════════════════════════════════

        interceptor_role = iam.Role(self, "InterceptorRole",
            role_name="multi-tenant-agent-databricks-interceptor-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )

        # Allow STS GetWebIdentityToken for WIF
        interceptor_role.add_to_policy(iam.PolicyStatement(
            actions=["sts:GetWebIdentityToken"],
            resources=["*"],
        ))

        interceptor_function = _lambda.Function(self, "InterceptorFunction",
            function_name="multi-tenant-agent-databricks-interceptor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.lambda_handler",
            code=_lambda.Code.from_asset("../lambda/interceptor"),
            role=interceptor_role,
            layers=[interceptor_layer],
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "WIF_CONFIG_TABLE": wif_config_table.table_name,
                "DATABRICKS_HOST": databricks_host,
            },
        )
        wif_config_table.grant_read_data(interceptor_function)

        # ═══════════════════════════════════════════════════════════════
        # AGENTCORE GATEWAY (via Custom Resource)
        # ═══════════════════════════════════════════════════════════════

        gateway_role = iam.Role(self, "GatewayRole",
            role_name="multi-tenant-agent-databricks-gateway-role",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        query_function.grant_invoke(gateway_role)

        # Lambda resource-based policy for Gateway invocation
        query_function.add_permission("GatewayInvoke",
            principal=iam.ArnPrincipal(gateway_role.role_arn),
            action="lambda:InvokeFunction",
        )
        interceptor_function.add_permission("GatewayInvoke",
            principal=iam.ArnPrincipal(gateway_role.role_arn),
            action="lambda:InvokeFunction",
        )

        # Gateway provider custom resource
        provider_role = iam.Role(self, "GatewayProviderRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        provider_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:*"],
            resources=["*"],
        ))
        provider_role.add_to_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[gateway_role.role_arn],
        ))

        gateway_provider_fn = _lambda.Function(self, "GatewayProviderFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_asset("../lambda/gateway_provider"),
            role=provider_role,
            timeout=Duration.minutes(5),
        )

        gateway_provider = cr.Provider(self, "GatewayProvider",
            on_event_handler=gateway_provider_fn,
        )

        discovery_url = (
            f"https://cognito-idp.{self.region}.amazonaws.com/"
            f"{user_pool.user_pool_id}/.well-known/openid-configuration"
        )

        # Databricks UC Functions MCP endpoint
        mcp_endpoint = (
            f"{databricks_host}/api/2.0/mcp/functions/saas_workshop/saas_pilot/get_orders"
            if databricks_host else "https://PLACEHOLDER/api/2.0/mcp/functions/saas_workshop/saas_pilot/get_orders"
        )

        tool_schema = json.dumps([{
            "name": "query_databricks",
            "description": "Query tenant-isolated data from Databricks. Unity Catalog row filters enforce isolation.",
            "inputSchema": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "SQL query to execute against Databricks."}},
                "required": ["sql"],
            },
        }])

        gateway_resource = CustomResource(self, "Gateway",
            service_token=gateway_provider.service_token,
            properties={
                "Action": "CREATE_GATEWAY",
                "GatewayName": "multi-tenant-databricks-gateway",
                "RoleArn": gateway_role.role_arn,
                "DiscoveryUrl": discovery_url,
                "AllowedAudience": client.user_pool_client_id,
                "InterceptorArn": interceptor_function.function_arn,
                "LambdaTargetArn": query_function.function_arn,
                "McpServerEndpoint": mcp_endpoint,
                "ToolSchema": tool_schema,
            },
        )

        # ═══════════════════════════════════════════════════════════════
        # AGENTCORE RUNTIME (Strands Agent)
        # ═══════════════════════════════════════════════════════════════

        agent_role = iam.Role(self, "AgentRole",
            role_name="multi-tenant-agent-databricks-agent-role",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
            inline_policies={
                "AgentPermissions": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                        resources=[f"arn:aws:bedrock:{self.region}::foundation-model/*"],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                        resources=[f"arn:aws:bedrock:us-*::foundation-model/*"],
                    ),
                    iam.PolicyStatement(
                        actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                        resources=[f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*"],
                    ),
                    iam.PolicyStatement(
                        actions=[
                            "ecr:GetAuthorizationToken",
                            "ecr:BatchGetImage",
                            "ecr:GetDownloadUrlForLayer",
                            "ecr:BatchCheckLayerAvailability",
                        ],
                        resources=["*"],
                    ),
                ]),
            },
        )

        import aws_cdk.aws_bedrockagentcore as agentcore
        from aws_cdk.aws_ecr_assets import DockerImageAsset, Platform

        # Build and push agent container image to ECR (ARM64 required by AgentCore)
        agent_image = DockerImageAsset(self, "AgentImage",
            directory="../agent",
            platform=Platform.LINUX_ARM64,
        )

        agent_runtime = agentcore.CfnRuntime(self, "AgentContainerRuntime",
            agent_runtime_name="mt_databricks_agent",
            role_arn=agent_role.role_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
            authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=discovery_url,
                    allowed_audience=[client.user_pool_client_id],
                ),
            ),
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=agent_image.image_uri,
                ),
            ),
            environment_variables={
                "GATEWAY_URL": gateway_resource.get_att_string("GatewayUrl"),
                "MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            },
            description="Multi-tenant Databricks agent — connects to Gateway for tenant-isolated tools",
            request_header_configuration=agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                request_header_allowlist=["Authorization"],
            ),
        )

        agent_endpoint = agentcore.CfnRuntimeEndpoint(self, "AgentContainerEndpoint",
            agent_runtime_id=agent_runtime.attr_agent_runtime_id,
            name="mt_databricks_agent_ep",
            description="Invoke endpoint for multi-tenant Databricks agent",
        )

        # ═══════════════════════════════════════════════════════════════
        # OUTPUTS
        # ═══════════════════════════════════════════════════════════════

        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "ClientId", value=client.user_pool_client_id)
        CfnOutput(self, "WifConfigTableName", value=wif_config_table.table_name)
        CfnOutput(self, "GatewayId", value=gateway_resource.get_att_string("GatewayId"))
        CfnOutput(self, "GatewayUrl", value=gateway_resource.get_att_string("GatewayUrl"))
        CfnOutput(self, "QueryFunctionArn", value=query_function.function_arn)
        CfnOutput(self, "QueryRoleName", value=query_role.role_name)
        CfnOutput(self, "InterceptorRoleName", value=interceptor_role.role_name)
        CfnOutput(self, "AgentRuntimeId", value=agent_runtime.attr_agent_runtime_id)
        CfnOutput(self, "AgentEndpointArn", value=agent_endpoint.attr_agent_runtime_endpoint_arn)
