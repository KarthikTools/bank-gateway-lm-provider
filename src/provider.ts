import * as vscode from 'vscode';
import { SECRET_API_KEY, TokenManager } from './auth';
import { request, requestStream, SseParser } from './http';
import { ResponseAccumulator, toOpenAIMessages, toOpenAITools, toToolChoice } from './openai';

export const VENDOR = 'bank-gateway';

export interface ModelConfig {
  id: string;
  gatewayModel?: string;
  name: string;
  family?: string;
  version?: string;
  maxInputTokens: number;
  maxOutputTokens: number;
  toolCalling?: boolean;
  imageInput?: boolean;
}

interface GatewayConfig {
  baseUrl: string;
  chatPath: string;
  apiKeyHeader: string;
  extraHeaders: Record<string, string>;
  stream: boolean;
  sendMaxTokens: boolean;
  debugLogBodies: boolean;
  models: ModelConfig[];
}

export function readGatewayConfig(): GatewayConfig {
  const cfg = vscode.workspace.getConfiguration('bankGateway');
  return {
    baseUrl: cfg.get<string>('baseUrl', '').trim().replace(/\/+$/, ''),
    chatPath: cfg.get<string>('chatPath', '/chat/completions').trim(),
    apiKeyHeader: cfg.get<string>('apiKeyHeader', '').trim(),
    extraHeaders: cfg.get<Record<string, string>>('extraHeaders', {}),
    stream: cfg.get<boolean>('stream', true),
    sendMaxTokens: cfg.get<boolean>('sendMaxTokens', true),
    debugLogBodies: cfg.get<boolean>('debugLogBodies', false),
    models: cfg.get<ModelConfig[]>('models', []),
  };
}

export class BankGatewayProvider implements vscode.LanguageModelChatProvider {
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeLanguageModelChatInformation = this.changeEmitter.event;

  constructor(
    private readonly tokens: TokenManager,
    private readonly secrets: vscode.SecretStorage,
    private readonly log: (msg: string) => void
  ) {}

  /** Tell VS Code the model list may have changed (after a settings edit). */
  refresh(): void {
    this.changeEmitter.fire();
  }

  dispose(): void {
    this.changeEmitter.dispose();
  }

  async provideLanguageModelChatInformation(
    _options: vscode.PrepareLanguageModelChatModelOptions,
    _token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelChatInformation[]> {
    const { models } = readGatewayConfig();
    return models
      .filter((m) => m && m.id && m.name)
      .map((m) => ({
        id: m.id,
        name: m.name,
        family: m.family ?? 'claude',
        version: m.version ?? '1.0.0',
        maxInputTokens: m.maxInputTokens,
        maxOutputTokens: m.maxOutputTokens,
        detail: 'Bank AI Gateway',
        tooltip: `Served through the internal AI gateway as "${m.gatewayModel ?? m.id}"`,
        capabilities: {
          toolCalling: m.toolCalling !== false,
          imageInput: m.imageInput === true,
        },
      }));
  }

  async provideLanguageModelChatResponse(
    model: vscode.LanguageModelChatInformation,
    messages: readonly vscode.LanguageModelChatRequestMessage[],
    options: vscode.ProvideLanguageModelChatResponseOptions,
    progress: vscode.Progress<vscode.LanguageModelResponsePart>,
    token: vscode.CancellationToken
  ): Promise<void> {
    const cfg = readGatewayConfig();
    if (!cfg.baseUrl) {
      throw new Error('Set bankGateway.baseUrl in settings.');
    }
    const modelCfg = cfg.models.find((m) => m.id === model.id);
    const url = cfg.baseUrl + cfg.chatPath;

    const tools = toOpenAITools(options.tools);
    const body: Record<string, unknown> = {
      model: modelCfg?.gatewayModel ?? model.id,
      messages: toOpenAIMessages(messages, model.capabilities?.imageInput === true),
      stream: cfg.stream,
    };
    if (tools) {
      body.tools = tools;
      body.tool_choice = toToolChoice(options.toolMode, true);
    }
    if (cfg.sendMaxTokens) {
      body.max_tokens = model.maxOutputTokens;
    }
    // Pass through common sampling options if VS Code supplied them.
    for (const key of ['temperature', 'top_p', 'max_tokens', 'stop']) {
      const v = options.modelOptions?.[key];
      if (v !== undefined) {
        body[key] = v;
      }
    }

    const bodyText = JSON.stringify(body);
    this.log(
      `POST ${url} model=${body.model} messages=${(body.messages as unknown[]).length} tools=${tools?.length ?? 0} stream=${cfg.stream}`
    );
    if (cfg.debugLogBodies) {
      this.log(`Request body: ${bodyText}`);
    }

    // One retry on 401 with a freshly minted token.
    for (let attempt = 0; attempt < 2; attempt++) {
      const headers = await this.buildHeaders(cfg);
      const accumulator = new ResponseAccumulator(progress);

      if (cfg.stream) {
        const parser = new SseParser(
          (event) => {
            if (cfg.debugLogBodies) {
              this.log(`chunk: ${JSON.stringify(event)}`);
            }
            accumulator.handleStreamChunk(event);
          },
          () => accumulator.flush()
        );
        const res = await requestStream(url, { method: 'POST', headers, body: bodyText }, (t) => parser.feed(t), token);
        if (res.status === 200) {
          parser.end();
          accumulator.flush();
          return;
        }
        if (res.status === 401 && attempt === 0) {
          this.log('401 from gateway; refreshing token and retrying once');
          this.tokens.invalidate();
          continue;
        }
        throw new Error(this.describeError(res.status, res.body));
      }

      const res = await request(url, { method: 'POST', headers, body: bodyText }, token);
      if (res.status === 200) {
        if (cfg.debugLogBodies) {
          this.log(`Response body: ${res.body}`);
        }
        accumulator.handleFullResponse(JSON.parse(res.body));
        return;
      }
      if (res.status === 401 && attempt === 0) {
        this.log('401 from gateway; refreshing token and retrying once');
        this.tokens.invalidate();
        continue;
      }
      throw new Error(this.describeError(res.status, res.body));
    }
  }

  async provideTokenCount(
    _model: vscode.LanguageModelChatInformation,
    text: string | vscode.LanguageModelChatRequestMessage,
    _token: vscode.CancellationToken
  ): Promise<number> {
    // Heuristic (~4 chars/token). Good enough for VS Code's context budgeting;
    // replace with a real tokenizer if you need precision.
    const s =
      typeof text === 'string'
        ? text
        : text.content
            .map((p) => {
              if (p instanceof vscode.LanguageModelTextPart) {
                return p.value;
              }
              if (p instanceof vscode.LanguageModelToolCallPart) {
                return p.name + JSON.stringify(p.input ?? {});
              }
              if (p instanceof vscode.LanguageModelToolResultPart) {
                return JSON.stringify(p.content);
              }
              return '';
            })
            .join('');
    return Math.ceil(s.length / 4);
  }

  /** Build headers for a gateway call: bearer token + optional API-key header + extras. */
  async buildHeaders(cfg: GatewayConfig): Promise<Record<string, string>> {
    const bearer = await this.tokens.getToken();
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: cfg.stream ? 'text/event-stream' : 'application/json',
      Authorization: `Bearer ${bearer}`,
      ...cfg.extraHeaders,
    };
    if (cfg.apiKeyHeader) {
      const apiKey = await this.secrets.get(SECRET_API_KEY);
      if (apiKey) {
        headers[cfg.apiKeyHeader] = apiKey;
      }
    }
    return headers;
  }

  private describeError(status: number, body: string): string {
    const snippet = body.replace(/\s+/g, ' ').slice(0, 400);
    this.log(`Gateway error HTTP ${status}: ${snippet}`);
    return `Bank AI Gateway returned HTTP ${status}: ${snippet}`;
  }
}
