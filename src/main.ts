import { loadConfig } from './config';
import { createServer } from './server';
import { logger, initializeGlobalLogger } from './shared/logger';

async function bootstrap() {
  const config = loadConfig();
  initializeGlobalLogger(config);
  const app = createServer(config);

  const port = config.port || 3000;
  app.listen(port, () => {
    logger.info(`Server listening on port ${port}`);
  });
}

bootstrap().catch((err) => {
  console.error('Failed to start server', err);
  process.exit(1);
});
