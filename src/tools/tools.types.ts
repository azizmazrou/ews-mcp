export interface Email {
  id: string;
  changeKey: string;
  subject: string;
  receivedDate: string;
  from: string;
  fromName: string;
  body: string;
  bodyType: string;
  bodyPreview: string;
  hasAttachments: boolean;
  importance: string;
  isRead: boolean;
}

export interface SearchEmailsParams {
  folder?: string;
  query?: string;
  pageSize?: number;
  pageOffset?: number;
}

export interface SearchEmailsResult {
  emails: Email[];
  totalItems: number;
  hasMore: boolean;
}

export interface ReadEmailParams {
  emailId: string;
}

export interface ReadEmailResult {
  email: Email | null;
}
