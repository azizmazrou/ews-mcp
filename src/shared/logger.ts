import winston from 'winston';
import { LogConfig } from '../config/config.types';

let logger: winston.Logger | null = null;

export function initializeGlobalLogger(config: LogConfig) {
  logger = winston.createLogger({
    level: config.level,
    format: winston.format.combine(
      winston.format.timestamp(),
      process.env.NODE_ENV === 'development'
        ? winston.format.colorize()
        : winston.format.uncolorize(),
      winston.format.errors({ stack: true }),
      winston.format.json()
    ),
    transports: [new winston.transports.Console()],
  });
}

export function getLogger(): winston.Logger {
  if (!logger) {
    throw new Error('Logger not initialized');
  }
  return logger;
}
