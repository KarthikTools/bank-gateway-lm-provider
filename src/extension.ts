import * as vscode from 'vscode';
import { promptAndStoreSecret, SECRET_API_KEY, SECRET_CLIENT_SECRET, TokenManager } from './auth';
import { request } from './http';
import { BankGatewayProvider, readGatewayConfig, VENDOR } from './provider';

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel('Bank AI Gateway');
  const log = (msg: string) => output.appendLine(`[${new Date().toISOString()}] ${msg}`);

  const tokens = new TokenManager(context.secrets, log);
  const provider = new BankGatewayProvider(tokens, context.secrets, log);

  context.subscriptions.push(
    output,
    provider,
    vscode.lm.registerLanguageModelChatProvider(VENDOR, provider),

    vscode.commands.registerCommand('bankGateway.manage', () => manage(context, provider, tokens, log, output)),

    vscode.commands.registerCommand('bankGateway.setClientSecret', async () => {
      if (await promptAndStoreSecret(context.secrets, SECRET_CLIENT_SECRET, 'OAuth2 client secret for the AI gateway (stored in the OS keychain)')) {
        tokens.invalidate();
        vscode.window.showInformationMessage('Bank AI Gateway: client secret updated.');
      }
    }),

    vscode.commands.registerCommand('bankGateway.setApiKey', async () => {
      if (await promptAndStoreSecret(context.secrets, SECRET_API_KEY, 'Gateway API key / subscription key (sent in the header named by bankGateway.apiKeyHeader)')) {
        vscode.window.showInformationMessage('Bank AI Gateway: API key updated.');
      }
    }),

    vscode.commands.registerCommand('bankGateway.clearSecrets', async () => {
      await context.secrets.delete(SECRET_CLIENT_SECRET);
      await context.secrets.delete(SECRET_API_KEY);
      tokens.invalidate();
      vscode.window.showInformationMessage('Bank AI Gateway: stored secrets cleared.');
    }),

    vscode.commands.registerCommand('bankGateway.testConnection', () => testConnection(provider, tokens, log, output)),

    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('bankGateway')) {
        tokens.invalidate();
        provider.refresh();
      }
    })
  );

  log('Bank AI Gateway provider registered.');
}

export function deactivate(): void {
  // Nothing to do; disposables are handled by context.subscriptions.
}

async function manage(
  context: vscode.ExtensionContext,
  provider: BankGatewayProvider,
  tokens: TokenManager,
  log: (msg: string) => void,
  output: vscode.OutputChannel
): Promise<void> {
  const hasSecret = !!(await context.secrets.get(SECRET_CLIENT_SECRET));
  const pick = await vscode.window.showQuickPick(
    [
      { label: '$(key) Set OAuth client secret', detail: hasSecret ? 'A secret is stored' : 'No secret stored yet', action: 'secret' },
      { label: '$(key) Set gateway API key header value', detail: 'Optional, e.g. Ocp-Apim-Subscription-Key', action: 'apikey' },
      { label: '$(plug) Test connection', detail: 'Fetch a token and send a one-line chat completion', action: 'test' },
      { label: '$(settings-gear) Open settings', detail: 'bankGateway.* (URLs, client id, models)', action: 'settings' },
      { label: '$(output) Show log', action: 'log' },
      { label: '$(trash) Clear stored secrets', action: 'clear' },
    ],
    { title: 'Bank AI Gateway', placeHolder: 'Choose an action' }
  );
  switch (pick?.action) {
    case 'secret':
      await vscode.commands.executeCommand('bankGateway.setClientSecret');
      break;
    case 'apikey':
      await vscode.commands.executeCommand('bankGateway.setApiKey');
      break;
    case 'test':
      await testConnection(provider, tokens, log, output);
      break;
    case 'settings':
      await vscode.commands.executeCommand('workbench.action.openSettings', 'bankGateway');
      break;
    case 'log':
      output.show(true);
      break;
    case 'clear':
      await vscode.commands.executeCommand('bankGateway.clearSecrets');
      break;
  }
}

/** Fetch a token and send a minimal non-streaming completion to the first configured model. */
async function testConnection(
  provider: BankGatewayProvider,
  tokens: TokenManager,
  log: (msg: string) => void,
  output: vscode.OutputChannel
): Promise<void> {
  const cfg = readGatewayConfig();
  const model = cfg.models[0];
  if (!cfg.baseUrl || !model) {
    vscode.window.showErrorMessage('Bank AI Gateway: set bankGateway.baseUrl and at least one model first.');
    return;
  }
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'Bank AI Gateway: testing connection…' },
    async () => {
      try {
        tokens.invalidate();
        const headers = await provider.buildHeaders({ ...cfg, stream: false });
        const body = JSON.stringify({
          model: model.gatewayModel ?? model.id,
          messages: [{ role: 'user', content: 'Reply with the single word: pong' }],
          max_tokens: 20,
          stream: false,
        });
        const res = await request(cfg.baseUrl + cfg.chatPath, { method: 'POST', headers, body });
        log(`Test connection -> HTTP ${res.status}: ${res.body.slice(0, 500)}`);
        if (res.status === 200) {
          const text = JSON.parse(res.body)?.choices?.[0]?.message?.content ?? '(no content)';
          vscode.window.showInformationMessage(`Bank AI Gateway OK — model replied: ${String(text).slice(0, 80)}`);
        } else {
          vscode.window.showErrorMessage(`Bank AI Gateway: HTTP ${res.status} — see the "Bank AI Gateway" output channel.`);
          output.show(true);
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        log(`Test connection failed: ${msg}`);
        vscode.window.showErrorMessage(`Bank AI Gateway: ${msg}`);
        output.show(true);
      }
    }
  );
}
