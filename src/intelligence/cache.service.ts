import { createClient, RedisClientType } from 'redis';
import { CacheConfig, RedisConfig } from '../config/config.types';
import { getLogger } from '../shared/logger';

export class CacheService {
  private client?: RedisClientType;
  private connected = false;
  private logger = getLogger();

  constructor(private cacheConfig: CacheConfig, private redisConfig: RedisConfig) {}

  async connect() {
    if (!this.cacheConfig.enabled) {
      this.logger.info('Caching disabled');
      return;
    }
    this.client = createClient({ url: this.redisConfig.url });
    this.client.on('error', (err) => {
      this.logger.error('Redis error', err);
    });
    try {
      await this.client.connect();
      this.connected = true;
    } catch (err) {
      this.logger.error('Redis connection failed', err);
      this.cacheConfig.enabled = false;
    }
  }

  async get<T>(key: string): Promise<T | null> {
    if (!this.cacheConfig.enabled || !this.connected || !this.client) return null;
    const val = await this.client.get(key);
    return val ? (JSON.parse(val) as T) : null;
  }

  async set<T>(key: string, value: T, ttl?: number) {
    if (!this.cacheConfig.enabled || !this.connected || !this.client) return;
    await this.client.set(key, JSON.stringify(value), {
      EX: ttl || this.cacheConfig.defaultTtlSeconds,
    });
  }

  async disconnect() {
    if (this.client && this.connected) {
      await this.client.disconnect();
      this.connected = false;
    }
  }

  isHealthy() {
    return !this.cacheConfig.enabled || this.connected;
  }
}
