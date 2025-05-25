import dotenv from 'dotenv';
import fs from 'fs';
import path from 'path';
import { configSchema, ConfigSchema } from './schema';
import { AppConfig } from './config.types';

export function loadConfig(): AppConfig {
  const nodeEnv = process.env.NODE_ENV || 'development';
  const envFiles = ['.env', `.env.${nodeEnv}`];
  envFiles.forEach((file) => {
    const p = path.resolve(process.cwd(), file);
    if (fs.existsSync(p)) {
      dotenv.config({ path: p });
    }
  });

  const raw: ConfigSchema = {
    nodeEnv,
    server: { port: Number(process.env.MCP_PORT) || 3000 },
    log: { level: process.env.LOG_LEVEL || 'info' },
    ews: {
      url: process.env.EWS_URL || '',
      version: process.env.EWS_VERSION || 'Exchange2016',
      auth: {
        type: process.env.EWS_AUTH_TYPE || 'Basic',
        username: process.env.EWS_USERNAME,
        password: process.env.EWS_PASSWORD,
      },
      requestTimeout: Number(process.env.EWS_REQUEST_TIMEOUT) || 10000,
      maxRetries: Number(process.env.EWS_MAX_RETRIES) || 3,
    },
    redis: {
      url: process.env.REDIS_URL || 'redis://localhost:6379/0',
    },
    audit: { enabled: process.env.AUDIT_ENABLED !== 'false' },
    cache: {
      enabled: process.env.CACHE_ENABLED !== 'false',
      defaultTtlSeconds: Number(process.env.CACHE_DEFAULT_TTL_SECONDS) || 300,
    },
  } as unknown as ConfigSchema;

  const { value, error } = configSchema.validate(raw, { abortEarly: false });
  if (error) {
    throw new Error(`Config validation failed: ${error.message}`);
  }

  return value as AppConfig;
}
