import axios from 'axios';
import { AppConfig } from '../config/config.types';

export class EWSClient {
  private http = axios.create();
  constructor(private config: AppConfig) {}

  async call(soapBody: string): Promise<string> {
    const response = await this.http.post(this.config.ewsUrl, soapBody, {
      auth: this.config.ewsUsername ? {
        username: this.config.ewsUsername,
        password: this.config.ewsPassword || ''
      } : undefined,
      headers: { 'Content-Type': 'text/xml' }
    });
    return response.data;
  }
}
