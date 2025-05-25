import { ToolExecutor } from './tool.executor';

export function createMcpHandler(executor: ToolExecutor) {
  return async (req: any, res: any) => {
    const { tool, params } = req.body;
    try {
      const result = await executor.execute(tool, params);
      res.json({ result });
    } catch (err: any) {
      res.status(400).json({ error: err.message });
    }
  };
}
