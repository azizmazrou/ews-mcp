import { GetItemResponseMessage, FindItemResponseMessage, EWSEmailItem } from './soap.parser';
import { Email } from '../tools/tools.types';

function mapEmail(item: EWSEmailItem): Email {
  return {
    id: item.ItemId.Id,
    changeKey: item.ItemId.ChangeKey,
    subject: item.Subject,
    receivedDate: item.DateTimeReceived,
    from: item.From?.Mailbox.EmailAddress || '',
    fromName: item.From?.Mailbox.Name || '',
    body: item.Body?.['#text'] || '',
    bodyType: item.Body?.['@_BodyType'] || 'Text',
    bodyPreview: item.BodyPreview || '',
    hasAttachments: item.HasAttachments === 'true',
    importance: item.Importance,
    isRead: item.IsRead === 'true',
  };
}

export function mapFindItemResponseToEmails(response: FindItemResponseMessage) {
  const messages = response.RootFolder.Items.Message;
  const items = Array.isArray(messages) ? messages : messages ? [messages] : [];
  return {
    emails: items.map(mapEmail),
    totalItems: response.RootFolder.TotalItemsInView,
    hasMore: response.RootFolder.IncludesLastItemInRange === 'false',
  };
}

export function mapGetItemResponseToEmail(response: GetItemResponseMessage): Email | null {
  const msg = response.Items?.Message;
  return msg ? mapEmail(msg) : null;
}
