import Joi from 'joi';

export const configSchema = Joi.object({
  nodeEnv: Joi.string().default('development'),
  server: Joi.object({
    port: Joi.number().default(3000),
  }).required(),
  log: Joi.object({
    level: Joi.string().default('info'),
  }).required(),
  ews: Joi.object({
    url: Joi.string().uri().required(),
    version: Joi.string().default('Exchange2016'),
    auth: Joi.object({
      type: Joi.string().valid('Basic').default('Basic'),
      username: Joi.when('type', {
        is: 'Basic',
        then: Joi.string().required(),
        otherwise: Joi.string().optional(),
      }),
      password: Joi.when('type', {
        is: 'Basic',
        then: Joi.string().required(),
        otherwise: Joi.string().optional(),
      }),
    }).required(),
    requestTimeout: Joi.number().default(10000),
    maxRetries: Joi.number().default(3),
  }).required(),
  redis: Joi.object({
    url: Joi.string().uri().required(),
  }).required(),
  audit: Joi.object({
    enabled: Joi.boolean().default(true),
  }).required(),
  cache: Joi.object({
    enabled: Joi.boolean().default(true),
    defaultTtlSeconds: Joi.number().default(300),
  }).required(),
});

export type ConfigSchema = Joi.inferType<typeof configSchema>;
