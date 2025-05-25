import { Buffer } from 'buffer';
import { EWSAuthConfig } from '../../config/config.types';
import { AuthToken, IAuthStrategy } from './IAuthStrategy';

export class BasicAuthStrategy implements IAuthStrategy {
  constructor(private config: EWSAuthConfig) {}

  async getAuthToken(): Promise<AuthToken> {
    const token = Buffer.from(
      `${this.config.username}:${this.config.password}`
    ).toString('base64');
    return { header: `Basic ${token}` };
  }

  async handleAuthError(): Promise<void> {
    // Basic auth has no recovery
  }
}
