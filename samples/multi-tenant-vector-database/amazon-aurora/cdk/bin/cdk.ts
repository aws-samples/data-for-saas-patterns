#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AuroraCdkStack } from '../lib/aurora-cdk-stack';

const app = new cdk.App();
const aurora_cdk_stack = new AuroraCdkStack(app, 'AuroraCdkStack', {});