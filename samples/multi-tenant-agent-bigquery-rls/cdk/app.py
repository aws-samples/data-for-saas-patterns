#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""
CDK App — Multi-Tenant AI Agent with BigQuery RLS

Single stack deployment:
  - Cognito User Pool + test users
  - AgentCore Gateway with JWT authorizer + interceptor
  - BigQuery Query Lambda (WIF-based auth)
  - MCP Server target registration

Prerequisites:
  - Run `bash scripts/build_layers.sh` before deploying
  - Run `bash scripts/01_setup_wif.sh` first to generate wif_config.json files
"""

import aws_cdk as cdk

from stacks.multi_tenant_bigquery_stack import MultiTenantBigQueryStack

app = cdk.App()

MultiTenantBigQueryStack(app, "MultiTenantAgentBigQuery",
    description="Multi-tenant AI agent with BigQuery RLS — Gateway, Lambda, Interceptor, Cognito",
)

app.synth()
