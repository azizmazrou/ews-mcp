export interface AppConfig {
  port: number;
  ewsUrl: string;
  ewsUsername?: string;
  ewsPassword?: string;
  log?: { level?: string };
}
