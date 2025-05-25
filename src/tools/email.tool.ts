import { AbstractTool } from './AbstractTool';
import { EWSClient } from '../ews/ews.client';

export class EmailToolV1 extends AbstractTool {
  constructor(private ews: EWSClient) { super(); }
  async run(params: any) {
    const soap = '<FindItem></FindItem>'; // placeholder
    const res = await this.ews.call(soap);
    return { raw: res };
  }
}
