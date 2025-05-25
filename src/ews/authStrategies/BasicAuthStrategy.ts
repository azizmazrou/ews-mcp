import { IAuthStrategy } from './IAuthStrategy';
import { AppConfig } from '../../config/config.types';

export class BasicAuthStrategy implements IAuthStrategy {
  constructor(private config: AppConfig) {}
  async getAuthHeader(): Promise<string> {
    const { ewsUsername, ewsPassword } = this.config;
    const token = Buffer.from(`${ewsUsername}:${ewsPassword}`).toString('base64');
    return `Basic ${token}`;
  }
}
