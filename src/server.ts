import http from 'http';
import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import compression from 'compression';
import * as Boom from '@hapi/boom';
import { setupMCP, getMCPServer } from './mcp/mcp.handler';
import { AppServices } from './config/config.types';
import { getLogger } from './shared/logger';

export function createApplicationServer(services: AppServices) {
  const app = express();
  const logger = getLogger();

  app.use(helmet());
  app.use(cors());
  app.use(compression());
  app.use(express.json());

  app.use((req, res, next) => {
    res.on('finish', () => {
      logger.info(`${req.method} ${req.url} ${res.statusCode}`);
    });
    next();
  });

  app.get('/health', (_req, res) => {
    if (services.cacheService.isHealthy()) {
      res.status(200).json({ status: 'ok' });
    } else {
      res.status(503).json({ status: 'unhealthy' });
    }
  });

  app.get('/metrics', (_req, res) => {
    res.json({
      uptime: process.uptime(),
      memory: process.memoryUsage(),
    });
  });

  app.use((req, _res, next) => {
    next(Boom.notFound(`Route ${req.path} not found`));
  });

  app.use((err: any, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
    const boom = Boom.isBoom(err) ? err : Boom.badImplementation(err.message);
    res.status(boom.output.statusCode).json(boom.output.payload);
  });

  const server = http.createServer(app);
  setupMCP(server, services);

  return {
    start: () =>
      new Promise<void>((resolve) => {
        server.listen(services.config.server.port, () => {
          logger.info(`Server listening on ${services.config.server.port}`);
          resolve();
        });
      }),
    stop: () =>
      new Promise<void>((resolve) => {
        getMCPServer()?.shutdown();
        server.close(() => resolve());
      }),
  };
}
