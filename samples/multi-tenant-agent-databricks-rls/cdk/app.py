#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""
CDK App — Multi-Tenant AI Agent with Databricks RLS

Single stack deployment:
  - Cognito User Pool + test users
  - DynamoDB table for WIF config (per-tenant client_id mappings)
  - AgentCore Gateway with JWT authorizer + interceptor
  - Databricks Query Lambda (WIF-based auth)
  - MCP Server target registration (DYNAMIC listing mode)

Prerequisites:
  - Run `bash scripts/build_layers.sh` before deploying
  - Set context: databricks_host, databricks_warehouse_id
"""

import aws_cdk as cdk

from stacks.multi_tenant_databricks_stack import MultiTenantDatabricksStack

app = cdk.App()

databricks_host = app.node.try_get_context("databricks_host") or ""
databricks_warehouse_id = app.node.try_get_context("databricks_warehouse_id") or ""

MultiTenantDatabricksStack(app, "MultiTenantAgentDatabricks",
    databricks_host=databricks_host,
    databricks_warehouse_id=databricks_warehouse_id,
    description="Multi-tenant AI agent with Databricks RLS — Gateway, Lambda, Interceptor, DynamoDB, Cognito",
)

app.synth()
