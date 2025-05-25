export interface IAuthStrategy {
  getAuthHeader(): Promise<string>;
}
