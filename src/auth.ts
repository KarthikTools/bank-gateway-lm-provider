/**
 * OAuth2 client-credentials token management.
 *
 * - The client secret (and optional gateway API key) live in VS Code
 *   SecretStorage, which is backed by the OS keychain. They are never written
 *   to settings.json and never logged.
 * - Tokens are cached in memory and refreshed 60 s before expiry.
 * - Concurrent callers share one in-flight token request.
 * - Credentials can be presented either as form fields (client_secret_post,
 *   the Entra ID default) or as an HTTP Basic header (client_secret_basic,
 *   what PingFederate's /as/token.oauth2, Apigee and Kong usually expect).
 */
import * as vscode from 'vscode';
import { request } from './http';

export const SECRET_CLIENT_SECRET = 'bankGateway.clientSecret';
export const SECRET_API_KEY = 'bankGateway.apiKey';

const REFRESH_MARGIN_MS = 60_000;

export type TokenAuthMethod = 'body' | 'basic';

interface CachedToken {
  value: string;
  expiresAt: number; // epoch ms
}

export class TokenManager {
  private cached: CachedToken | undefined;
  private inflight: Promise<string> | undefined;

  constructor(
    private readonly secrets: vscode.SecretStorage,
    private readonly log: (msg: string) => void
  ) {}

  /** Returns a valid bearer token, fetching a new one if needed. */
  async getToken(): Promise<string> {
    if (this.cached && Date.now() < this.cached.expiresAt - REFRESH_MARGIN_MS) {
      return this.cached.value;
    }
    if (!this.inflight) {
      this.inflight = this.fetchToken().finally(() => {
        this.inflight = undefined;
      });
    }
    return this.inflight;
  }

  /** Drop the cached token (e.g. after a 401 or a settings change). */
  invalidate(): void {
    this.cached = undefined;
  }

  private async fetchToken(): Promise<string> {
    const cfg = vscode.workspace.getConfiguration('bankGateway');
    const tokenUrl = cfg.get<string>('tokenUrl', '').trim();
    const clientId = cfg.get<string>('clientId', '').trim();
    const scope = cfg.get<string>('scope', '').trim();
    const resource = cfg.get<string>('resource', '').trim();
    const tokenAuth = cfg.get<TokenAuthMethod>('tokenAuth', 'body');

    if (!tokenUrl || !clientId) {
      throw new Error('Set bankGateway.tokenUrl and bankGateway.clientId in settings.');
    }
    const clientSecret = await this.secrets.get(SECRET_CLIENT_SECRET);
    if (!clientSecret) {
      throw new Error('Client secret not set. Run "Bank AI Gateway: Set OAuth Client Secret".');
    }

    const form = new URLSearchParams({ grant_type: 'client_credentials' });
    const headers: Record<string, string> = {
      'Content-Type': 'application/x-www-form-urlencoded',
      Accept: 'application/json',
    };
    if (tokenAuth === 'basic') {
      // client_secret_basic: same as `curl -u client_id:client_secret`.
      headers.Authorization = 'Basic ' + Buffer.from(`${clientId}:${clientSecret}`).toString('base64');
    } else {
      // client_secret_post
      form.set('client_id', clientId);
      form.set('client_secret', clientSecret);
    }
    if (scope) {
      form.set('scope', scope);
    }
    if (resource) {
      form.set('resource', resource);
    }

    this.log(`Requesting token from ${tokenUrl} (auth=${tokenAuth}${scope ? `, scope=${scope}` : ''})`);
    const res = await request(tokenUrl, {
      method: 'POST',
      headers,
      body: form.toString(),
    });

    if (res.status !== 200) {
      throw new Error(`Token endpoint returned HTTP ${res.status}: ${res.body.slice(0, 300)}`);
    }

    let json: { access_token?: string; expires_in?: number | string };
    try {
      json = JSON.parse(res.body);
    } catch {
      throw new Error('Token endpoint returned a non-JSON body.');
    }
    if (!json.access_token) {
      throw new Error('Token endpoint response had no access_token.');
    }

    const expiresIn = Number(json.expires_in ?? 3600);
    this.cached = {
      value: json.access_token,
      expiresAt: Date.now() + (Number.isFinite(expiresIn) ? expiresIn : 3600) * 1000,
    };
    this.log(`Token acquired; expires in ${Math.round(expiresIn)} s`);
    return this.cached.value;
  }
}

export async function promptAndStoreSecret(
  secrets: vscode.SecretStorage,
  key: string,
  prompt: string
): Promise<boolean> {
  const value = await vscode.window.showInputBox({
    prompt,
    password: true,
    ignoreFocusOut: true,
  });
  if (value === undefined) {
    return false;
  }
  if (value.trim() === '') {
    await secrets.delete(key);
    return true;
  }
  await secrets.store(key, value.trim());
  return true;
}
