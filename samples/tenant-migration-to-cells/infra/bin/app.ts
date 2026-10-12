#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { SandboxStack } from '../lib/sandbox-stack';

const app = new App();
new SandboxStack(app, 'TenantMigrationSample', {
  // Environment-agnostic: deploys to the account and Region of your current credentials.
  description: 'Per-tenant migration into a cell: sample sandbox (synthetic data only)',
  notifyEmail: app.node.tryGetContext('notifyEmail'),   // optional: -c notifyEmail=you@example.com
  monthlyBudgetUsd: Number(app.node.tryGetContext('monthlyBudgetUsd')),
  v1EngineVersion: app.node.tryGetContext('v1EngineVersion'),
  v2EngineVersion: app.node.tryGetContext('v2EngineVersion'),
});
