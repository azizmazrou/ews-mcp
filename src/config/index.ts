import fs from 'fs';
import path from 'path';
import Joi from 'joi';
import { AppConfig } from './config.types';
import { schema } from './schema';

export function loadConfig(): AppConfig {
  const env = process.env.NODE_ENV || 'development';
  const filePath = path.join(__dirname, '../../config', `${env}.json`);
  let fileConfig: any = {};
  if (fs.existsSync(filePath)) {
    fileConfig = JSON.parse(fs.readFileSync(filePath, 'utf8'));
  }
  const merged = { ...fileConfig, ...process.env };
  const { error, value } = schema.validate(merged, { allowUnknown: true });
  if (error) {
    throw error;
  }
  return value as AppConfig;
}
