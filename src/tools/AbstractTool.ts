import { ZodSchema } from 'zod';
import { ToolV2, ToolHandler } from '@anthropic/mcp';
import { AppServices } from '../config/config.types';
import { InvalidToolInputError } from '../shared/errors';
import { getLogger } from '../shared/logger';

export abstract class AbstractTool<TInput, TOutput> implements ToolV2<TInput, TOutput> {
  abstract name: string;
  abstract description: string;
  abstract version: string;
  abstract inputSchema: ZodSchema<TInput>;

  protected services: AppServices;
  protected logger = getLogger();

  constructor(services: AppServices) {
    this.services = services;
  }

  handler: ToolHandler<TInput, TOutput> = async (input) => {
    const parsed = this.inputSchema.safeParse(input);
    if (!parsed.success) {
      throw new InvalidToolInputError(parsed.error);
    }
    const result = await this.execute(parsed.data);
    this.services.auditService?.logToolExecution(this.name, parsed.data, result);
    return result;
  };

  protected abstract execute(params: TInput): Promise<TOutput>;
}
