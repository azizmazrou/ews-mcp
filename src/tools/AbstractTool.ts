export abstract class AbstractTool {
  abstract run(params: any): Promise<any>;
}
