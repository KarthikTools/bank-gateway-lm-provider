/**
 * Minimal HTTP helpers on top of Node's http/https modules.
 *
 * Why not fetch(): VS Code patches Node's http/https modules inside the
 * extension host so that the user's proxy settings (http.proxy, PAC, system
 * proxy) and system certificates (http.systemCertificates) apply. That is
 * exactly what a corporate network with a proxy and TLS inspection needs.
 */
import * as http from 'http';
import * as https from 'https';
import * as vscode from 'vscode';

export interface HttpResponse {
  status: number;
  body: string;
}

export interface RequestOptions {
  method: 'GET' | 'POST';
  headers: Record<string, string>;
  body?: string;
}

function pickModule(url: URL): typeof http | typeof https {
  return url.protocol === 'http:' ? http : https;
}

/** Non-streaming request: resolves with status + full body (never rejects on HTTP errors). */
export function request(
  urlString: string,
  opts: RequestOptions,
  token?: vscode.CancellationToken
): Promise<HttpResponse> {
  return new Promise((resolve, reject) => {
    const url = new URL(urlString);
    const mod = pickModule(url);
    const headers: Record<string, string> = { ...opts.headers };
    if (opts.body !== undefined) {
      headers['Content-Length'] = String(Buffer.byteLength(opts.body));
    }

    const req = mod.request(url, { method: opts.method, headers }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c: Buffer) => chunks.push(c));
      res.on('end', () =>
        resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString('utf8') })
      );
      res.on('error', reject);
    });

    req.on('error', reject);
    token?.onCancellationRequested(() => req.destroy(new Error('Request cancelled')));

    if (opts.body !== undefined) {
      req.write(opts.body);
    }
    req.end();
  });
}

/**
 * Streaming request. On a 200 the body is delivered incrementally through
 * onChunk and the promise resolves with { status: 200 } when the stream ends.
 * On any other status the full body is collected and returned so the caller
 * can surface the gateway's error message.
 */
export function requestStream(
  urlString: string,
  opts: RequestOptions,
  onChunk: (text: string) => void,
  token?: vscode.CancellationToken
): Promise<HttpResponse> {
  return new Promise((resolve, reject) => {
    const url = new URL(urlString);
    const mod = pickModule(url);
    const headers: Record<string, string> = { ...opts.headers };
    if (opts.body !== undefined) {
      headers['Content-Length'] = String(Buffer.byteLength(opts.body));
    }

    const req = mod.request(url, { method: opts.method, headers }, (res) => {
      const status = res.statusCode ?? 0;
      if (status !== 200) {
        const chunks: Buffer[] = [];
        res.on('data', (c: Buffer) => chunks.push(c));
        res.on('end', () => resolve({ status, body: Buffer.concat(chunks).toString('utf8') }));
        res.on('error', reject);
        return;
      }
      res.setEncoding('utf8');
      res.on('data', (text: string) => onChunk(text));
      res.on('end', () => resolve({ status, body: '' }));
      res.on('error', reject);
    });

    req.on('error', reject);
    token?.onCancellationRequested(() => req.destroy(new Error('Request cancelled')));

    if (opts.body !== undefined) {
      req.write(opts.body);
    }
    req.end();
  });
}

/**
 * Incremental Server-Sent-Events parser. Feed raw text chunks; it calls
 * onEvent with the JSON payload of each `data:` line and onDone on `[DONE]`.
 */
export class SseParser {
  private buffer = '';
  private finished = false;

  constructor(
    private readonly onEvent: (payload: unknown) => void,
    private readonly onDone: () => void
  ) {}

  feed(text: string): void {
    if (this.finished) {
      return;
    }
    this.buffer += text;
    let newline = this.buffer.indexOf('\n');
    while (newline !== -1) {
      const line = this.buffer.slice(0, newline).replace(/\r$/, '');
      this.buffer = this.buffer.slice(newline + 1);
      this.handleLine(line);
      if (this.finished) {
        return;
      }
      newline = this.buffer.indexOf('\n');
    }
  }

  /** Flush a trailing line that arrived without a newline. */
  end(): void {
    if (!this.finished && this.buffer.trim().length > 0) {
      this.handleLine(this.buffer);
      this.buffer = '';
    }
  }

  private handleLine(line: string): void {
    if (!line.startsWith('data:')) {
      return; // ignore comments, event:, id:, blank lines
    }
    const payload = line.slice(5).trim();
    if (payload === '' ) {
      return;
    }
    if (payload === '[DONE]') {
      this.finished = true;
      this.onDone();
      return;
    }
    try {
      this.onEvent(JSON.parse(payload));
    } catch {
      // Malformed JSON in a data line: skip it rather than kill the stream.
    }
  }
}
