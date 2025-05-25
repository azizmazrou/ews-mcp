import winston from 'winston';
import { AppConfig } from '../config/config.types';

export let logger = winston.createLogger({
  level: 'info',
  transports: [new winston.transports.Console({ format: winston.format.simple() })]
});

export function createLogger(config: AppConfig['log']) {
  return winston.createLogger({
    level: config?.level || 'info',
    format: winston.format.json(),
    transports: [new winston.transports.Console()]
  });
}

export function initializeGlobalLogger(config: AppConfig) {
  logger = createLogger(config.log);
}
