import express from 'express';
import { AppConfig } from './config/config.types';
import { logger } from './shared/logger';
import { ToolExecutor } from './mcp/tool.executor';
import { createMcpHandler } from './mcp/mcp.handler';
import { EWSClient } from './ews/ews.client';
import { BasicAuthStrategy } from './ews/authStrategies/BasicAuthStrategy';
import { EmailToolV1 } from './tools/email.tool';

export function createServer(config: AppConfig) {
  const app = express();
  app.use(express.json());

  const ews = new EWSClient(config);
  const emailTool = new EmailToolV1(ews);
  const executor = new ToolExecutor({ 'email_v1': emailTool });

  app.get('/health', (_req, res) => {
    res.json({ status: 'ok' });
  });

  app.post('/mcp', createMcpHandler(executor));

  return app;
}
