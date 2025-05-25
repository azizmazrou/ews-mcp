import { loadConfig } from './config';
import { initializeGlobalLogger } from './shared/logger';
import { BasicAuthStrategy } from './ews/authStrategies/BasicAuthStrategy';
import { EWSClient } from './ews/ews.client';
import { SOAPBuilder } from './ews/soap.builder';
import { CacheService } from './intelligence/cache.service';
import { AuditService } from './intelligence/audit.service';
import { createApplicationServer } from './server';

async function bootstrap() {
  const config = loadConfig();
  initializeGlobalLogger(config.log);

  const authStrategy = new BasicAuthStrategy(config.ews.auth);
  const ewsClient = new EWSClient(config.ews, authStrategy);
  const soapBuilder = new SOAPBuilder(config.ews);
  const cacheService = new CacheService(config.cache, config.redis);
  await cacheService.connect();
  const auditService = new AuditService(config.audit);

  const services = {
    config,
    ewsClient,
    soapBuilder,
    cacheService,
    auditService,
  };

  const server = createApplicationServer(services);
  await server.start();

  const shutdown = async () => {
    await server.stop();
    await cacheService.disconnect();
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

bootstrap().catch((err) => {
  console.error(err);
  process.exit(1);
});
