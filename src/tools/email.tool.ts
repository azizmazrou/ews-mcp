import { z } from 'zod';
import { AbstractTool } from './AbstractTool';
import {
  SearchEmailsParams,
  SearchEmailsResult,
  ReadEmailParams,
  ReadEmailResult,
} from './tools.types';
import { AppServices } from '../config/config.types';
import { parseSOAPResponse, FindItemResponseMessage, GetItemResponseMessage } from '../ews/soap.parser';
import { mapFindItemResponseToEmails, mapGetItemResponseToEmail } from '../ews/ews.mapper';

export class SearchEmailsTool extends AbstractTool<SearchEmailsParams, SearchEmailsResult> {
  name = 'search_emails';
  description = 'Search emails in a mailbox folder.';
  version = '1.0.0';
  inputSchema = z.object({
    folder: z.string().optional().default('inbox'),
    query: z.string().optional(),
    pageSize: z.number().min(1).max(100).optional().default(10),
    pageOffset: z.number().min(0).optional().default(0),
  });

  constructor(services: AppServices) {
    super(services);
  }

  protected async execute(params: SearchEmailsParams): Promise<SearchEmailsResult> {
    const soap = this.services.soapBuilder.buildFindItemRequest({
      folderId: params.folder || 'inbox',
      queryString: params.query,
      pageSize: params.pageSize || 10,
      offset: params.pageOffset || 0,
    });
    const xml = await this.services.ewsClient.request(soap, 'FindItem');
    const parsed = parseSOAPResponse<{ ResponseMessages: { FindItemResponseMessage: FindItemResponseMessage } }>(xml, 'FindItem');
    const msg = parsed.ResponseMessages.FindItemResponseMessage;
    return mapFindItemResponseToEmails(msg);
  }
}

export class ReadEmailTool extends AbstractTool<ReadEmailParams, ReadEmailResult> {
  name = 'read_email';
  description = 'Read a single email by id.';
  version = '1.0.0';
  inputSchema = z.object({
    emailId: z.string(),
  });

  constructor(services: AppServices) {
    super(services);
  }

  protected async execute(params: ReadEmailParams): Promise<ReadEmailResult> {
    const soap = this.services.soapBuilder.buildGetItemRequest({ itemIds: [params.emailId] });
    const xml = await this.services.ewsClient.request(soap, 'GetItem');
    const parsed = parseSOAPResponse<{ ResponseMessages: { GetItemResponseMessage: GetItemResponseMessage } }>(xml, 'GetItem');
    const msg = parsed.ResponseMessages.GetItemResponseMessage;
    return { email: mapGetItemResponseToEmail(msg) };
  }
}
