import { MCPServer } from '@anthropic/mcp';
import { Server as HttpServer } from 'http';
import { WebSocketServer } from 'ws';
import { AppServices } from '../config/config.types';
import { SearchEmailsTool, ReadEmailTool } from '../tools/email.tool';

let mcpServer: MCPServer | null = null;

export function setupMCP(httpServer: HttpServer, services: AppServices) {
  mcpServer = new MCPServer();
  mcpServer.registerTool(new SearchEmailsTool(services));
  mcpServer.registerTool(new ReadEmailTool(services));

  const wss = new WebSocketServer({ server: httpServer, path: '/mcp' });
  wss.on('connection', (ws) => {
    mcpServer?.handleConnection(ws as any);
  });
  return mcpServer;
}

export function getMCPServer() {
  return mcpServer;
}
