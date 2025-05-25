export interface ServerConfig {
  port: number;
}

export interface LogConfig {
  level: string;
}

export interface EWSAuthConfig {
  type: string;
  username?: string;
  password?: string;
}

export interface EWSConfig {
  url: string;
  version: string;
  auth: EWSAuthConfig;
  requestTimeout: number;
  maxRetries: number;
}

export interface RedisConfig {
  url: string;
}

export interface AuditConfig {
  enabled: boolean;
}

export interface CacheConfig {
  enabled: boolean;
  defaultTtlSeconds: number;
}

export interface AppConfig {
  nodeEnv: string;
  server: ServerConfig;
  log: LogConfig;
  ews: EWSConfig;
  redis: RedisConfig;
  audit: AuditConfig;
  cache: CacheConfig;
}

export interface AppServices {
  config: AppConfig;
  // placeholders for services
  ewsClient: any;
  soapBuilder: any;
  cacheService: any;
  auditService: any;
}
