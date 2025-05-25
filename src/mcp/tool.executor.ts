import { AbstractTool } from '../tools/AbstractTool';

export class ToolExecutor {
  constructor(private tools: Record<string, AbstractTool>) {}

  async execute(name: string, params: any) {
    const tool = this.tools[name];
    if (!tool) throw new Error('Tool not found');
    return tool.run(params);
  }
}
