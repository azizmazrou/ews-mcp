import axios, { AxiosInstance } from 'axios';
import { EWSConfig } from '../config/config.types';
import { IAuthStrategy } from './authStrategies/IAuthStrategy';
import {
  EWSAuthenticationError,
  EWSServerBusyError,
  EWSRequestError,
} from '../shared/errors';
import { parseEWSResponseCode } from './soap.parser';
import { getLogger } from '../shared/logger';

export class EWSClient {
  private axios: AxiosInstance;
  private logger = getLogger();

  constructor(private config: EWSConfig, private authStrategy: IAuthStrategy) {
    this.axios = axios.create({
      baseURL: config.url,
      timeout: config.requestTimeout,
      headers: { 'Content-Type': 'text/xml' },
    });

    this.axios.interceptors.request.use(async (cfg) => {
      const token = await this.authStrategy.getAuthToken();
      cfg.headers = cfg.headers || {};
      cfg.headers['Authorization'] = token.header;
      return cfg;
    });
  }

  async request(soapBody: string, soapAction: string): Promise<string> {
    for (let attempt = 0; attempt <= this.config.maxRetries; attempt++) {
      try {
        const response = await this.axios.post('', soapBody, {
          headers: { SOAPAction: soapAction },
        });
        const responseCode = parseEWSResponseCode(response.data);
        if (responseCode === 'ErrorServerBusy') {
          throw new EWSServerBusyError('Server busy');
        }
        return response.data;
      } catch (err: any) {
        const status = err.response?.status;
        if (status === 401 || status === 403) {
          await this.authStrategy.handleAuthError?.(err);
          throw new EWSAuthenticationError('Authentication failed');
        }
        if (status === 503 || status === 429 || err instanceof EWSServerBusyError) {
          const delay = Math.pow(2, attempt) * 1000;
          await new Promise((r) => setTimeout(r, delay));
          continue;
        }
        this.logger.error('EWS request failed', { err });
        throw new EWSRequestError('EWS request failed', status, err);
      }
    }
    throw new EWSRequestError('Max retries exceeded');
  }
}
