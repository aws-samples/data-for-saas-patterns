# SPDX-License-Identifier: MIT-0
"""
app.py — Multi-Tenant BigQuery Agent (Strands on AgentCore Runtime)

FastAPI app that exposes /invocations and /ping endpoints per AgentCore contract.
Connects to AgentCore Gateway as an MCP client using the caller's JWT.
Tenant isolation is enforced by the Gateway + Interceptor + BigQuery RLS.

The agent code is 100% tenant-unaware. Same code, same model, same prompt.
"""

import json
import logging
import os
import traceback
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Dict, Any

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")

SYSTEM_PROMPT = f"""You are a helpful data analyst assistant for a multi-tenant SaaS application.
You have access to BigQuery tools. The GCP project is {GCP_PROJECT_ID}.
Available tables: saas_pilot.orders, saas_pilot.customer_data

Important:
- You do NOT need to filter by tenant_id in your SQL queries.
- Row-Level Security (RLS) automatically ensures you only see data belonging to the current tenant.
- Write simple, clean SQL without WHERE tenant_id = ... clauses.
- Always use the query_bigquery tool to answer data questions.

When users ask about their data, use the query tools to fetch results and present them clearly.
"""

app = FastAPI(title="Multi-Tenant BigQuery Agent", version="1.0.0")


class InvocationRequest(BaseModel):
    input: Dict[str, Any]


class InvocationResponse(BaseModel):
    output: Dict[str, Any]


@app.post("/invocations", response_model=InvocationResponse)
async def invoke_agent(request: Request):
    """AgentCore Runtime invocation endpoint.

    Reads the JWT from the Authorization header (propagated by Runtime)
    and forwards it to the Gateway for tenant-scoped tool access.
    """
    try:
        body = await request.json()
        user_message = body.get("input", {}).get("prompt", "")
        if not user_message:
            raise HTTPException(status_code=400, detail="No prompt in input")

        # Read JWT from request headers (propagated by Runtime's requestHeaderAllowlist)
        auth_header = request.headers.get("Authorization", request.headers.get("authorization", ""))
        auth_token = auth_header.replace("Bearer ", "").replace("bearer ", "") if auth_header else ""

        if not auth_token:
            raise HTTPException(status_code=401, detail="Authorization header required")

        logger.info(f"Processing: {user_message[:100]}")

        # Create agent with MCP client pointing to Gateway
        model = BedrockModel(model_id=MODEL_ID)

        mcp_client = MCPClient(
            lambda: streamablehttp_client(
                url=GATEWAY_URL,
                headers={
                    "Authorization": f"Bearer {auth_token}",
                    "Mcp-Protocol-Version": "2025-11-25",
                },
            )
        )

        with mcp_client:
            tools = mcp_client.list_tools_sync()
            logger.info(f"Discovered {len(tools)} tools from Gateway")

            agent = Agent(
                model=model,
                tools=tools,
                system_prompt=SYSTEM_PROMPT,
            )
            result = agent(user_message)

        response = {
            "message": str(result.message) if hasattr(result, 'message') else str(result),
            "timestamp": datetime.utcnow().isoformat(),
        }

        return InvocationResponse(output=response)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent error: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Agent failed: {str(e)}")


@app.get("/ping")
async def ping():
    """Health check endpoint (required by AgentCore Runtime)."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
