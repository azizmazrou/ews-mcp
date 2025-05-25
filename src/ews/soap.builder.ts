import { create } from 'xmlbuilder2';
import { EWSConfig } from '../config/config.types';

export class SOAPBuilder {
  constructor(private config: EWSConfig) {}

  private createEnvelope() {
    return create({ version: '1.0', encoding: 'UTF-8' })
      .ele('soap:Envelope', {
        xmlns: 'http://schemas.xmlsoap.org/soap/envelope/',
        'xmlns:t': 'http://schemas.microsoft.com/exchange/services/2006/types',
        'xmlns:m': 'http://schemas.microsoft.com/exchange/services/2006/messages',
      })
      .ele('soap:Header')
      .ele('t:RequestServerVersion', { Version: this.config.version })
      .up()
      .up()
      .ele('soap:Body');
  }

  buildFindItemRequest(params: {
    folderId: string;
    queryString?: string;
    pageSize: number;
    offset: number;
  }): string {
    const doc = this.createEnvelope();
    const body = doc
      .ele('m:FindItem', {
        Traversal: 'Shallow',
      })
      .ele('m:ItemShape')
      .ele('t:BaseShape')
      .txt('Default')
      .up()
      .up();

    body
      .ele('m:IndexedPageItemView', {
        MaxEntriesReturned: params.pageSize,
        Offset: params.offset,
        BasePoint: 'Beginning',
      })
      .up();

    if (params.queryString) {
      body.ele('m:QueryString').txt(params.queryString).up();
    }

    body
      .ele('m:ParentFolderIds')
      .ele('t:DistinguishedFolderId', { Id: params.folderId })
      .up()
      .up();

    return doc.end({ headless: true });
  }

  buildGetItemRequest(params: { itemIds: string[] }): string {
    const doc = this.createEnvelope();
    const body = doc
      .ele('m:GetItem')
      .ele('m:ItemShape')
      .ele('t:BaseShape')
      .txt('AllProperties')
      .up()
      .ele('t:BodyType')
      .txt('HTML')
      .up()
      .up();

    const ids = body.ele('m:ItemIds');
    params.itemIds.forEach((id) => {
      ids.ele('t:ItemId', { Id: id });
    });

    return doc.end({ headless: true });
  }
}
