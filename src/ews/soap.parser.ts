import { XMLParser } from 'fast-xml-parser';

const parser = new XMLParser({ ignoreAttributes: false, removeNSPrefix: true });

export function parseEWSResponseCode(xml: string): string | null {
  const obj = parser.parse(xml);
  const msg =
    obj?.Envelope?.Body?.FindItemResponse?.ResponseMessages?.FindItemResponseMessage ||
    obj?.Envelope?.Body?.GetItemResponse?.ResponseMessages?.GetItemResponseMessage;
  return msg?.ResponseCode || null;
}

export function parseSOAPResponse<T>(xml: string, operation: string): T {
  const obj = parser.parse(xml);
  const response = obj?.Envelope?.Body?.[`${operation}Response`];
  return response as T;
}

export interface EWSEmailItem {
  ItemId: { Id: string; ChangeKey: string };
  Subject: string;
  DateTimeReceived: string;
  From?: { Mailbox: { EmailAddress: string; Name: string } };
  Body?: { '#text': string; '@_BodyType': string };
  BodyPreview?: string;
  HasAttachments: string;
  Importance: string;
  IsRead: string;
}

export interface FindItemResponseMessage {
  RootFolder: {
    Items: { Message?: EWSEmailItem | EWSEmailItem[] };
    TotalItemsInView: number;
    IncludesLastItemInRange: string;
  };
}

export interface GetItemResponseMessage {
  Items: { Message: EWSEmailItem };
}
