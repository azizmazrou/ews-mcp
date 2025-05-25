import { AuditConfig } from '../config/config.types';
import { getLogger } from '../shared/logger';

export class AuditService {
  private logger = getLogger();
  constructor(private config: AuditConfig) {}

  logToolExecution(toolName: string, params: any, result: any, userId?: string) {
    if (!this.config.enabled) return;
    const entry = {
      timestamp: new Date().toISOString(),
      event_type: 'TOOL_EXECUTION',
      tool_name: toolName,
      user_id: userId,
      params: this.sanitize(params),
      resultSummary: result,
    };
    this.logger.info('audit', entry);
  }

  private sanitize(obj: any) {
    const clone = { ...obj };
    if ('password' in clone) clone.password = 'REDACTED';
    if ('body' in clone) clone.body = 'REDACTED';
    return clone;
  }
}
