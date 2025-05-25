export interface AuthToken {
  header: string;
}

export interface IAuthStrategy {
  getAuthToken(): Promise<AuthToken>;
  handleAuthError?(error: any): Promise<void>;
}
