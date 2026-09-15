// End-to-end smoke test: stub the `vscode` module, run a fake PingFederate +
// gateway on localhost, and drive the compiled provider through a streaming
// agent-mode turn (text + tool call), a non-streaming turn, and a 401 retry.
const http = require('http');
const Module = require('module');
const assert = require('assert');
const path = require('path');

// ---------- vscode stub ----------
let config = {};
class Part { constructor() {} }
class LanguageModelTextPart extends Part { constructor(value) { super(); this.value = value; } }
class LanguageModelToolCallPart extends Part { constructor(callId, name, input) { super(); this.callId = callId; this.name = name; this.input = input; } }
class LanguageModelToolResultPart extends Part { constructor(callId, content) { super(); this.callId = callId; this.content = content; } }
class LanguageModelDataPart extends Part { constructor(data, mimeType) { super(); this.data = data; this.mimeType = mimeType; } }
class LanguageModelPromptTsxPart extends Part { constructor(value) { super(); this.value = value; } }
class EventEmitter { constructor() { this.event = () => ({ dispose() {} }); } fire() {} dispose() {} }
const vscodeStub = {
  workspace: {
    getConfiguration: (section) => ({ get: (k, d) => (section === 'bankGateway' && k in config ? config[k] : d) }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
  },
  LanguageModelChatMessageRole: { User: 1, Assistant: 2 },
  LanguageModelChatToolMode: { Auto: 1, Required: 2 },
  LanguageModelTextPart, LanguageModelToolCallPart, LanguageModelToolResultPart, LanguageModelDataPart, LanguageModelPromptTsxPart,
  EventEmitter,
};
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (req, ...rest) { return req === 'vscode' ? 'vscode' : origResolve.call(this, req, ...rest); };
require.cache['vscode'] = { id: 'vscode', filename: 'vscode', loaded: true, exports: vscodeStub };

const out = path.resolve(process.argv[2] || '.', 'out');
const { TokenManager, SECRET_CLIENT_SECRET } = require(path.join(out, 'auth.js'));
const { BankGatewayProvider } = require(path.join(out, 'provider.js'));

// ---------- fake PingFederate + gateway ----------
const CLIENT_ID = 'rbc-client', CLIENT_SECRET = 'sup3r:secret/=', MODEL = 'us.anthropic.claude-sonnet-4-5-20250929-v1:0';
let tokenCalls = 0, chatCalls = 0, lastTokenReq = null, lastChatReq = null, failNextChatWith401 = false;
const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    if (req.method === 'POST' && req.url === '/as/token.oauth2') {
      tokenCalls++;
      lastTokenReq = { headers: req.headers, body };
      const auth = req.headers.authorization || '';
      const ok = auth.startsWith('Basic ') && Buffer.from(auth.slice(6), 'base64').toString() === `${CLIENT_ID}:${CLIENT_SECRET}`;
      if (!ok) { res.writeHead(401, { 'content-type': 'application/json' }); return res.end('{"error":"invalid_client"}'); }
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ access_token: `tok-${tokenCalls}`, token_type: 'Bearer', expires_in: 3600 }));
    }
    if (req.method === 'POST' && req.url === '/v1/chat/completion') {
      chatCalls++;
      lastChatReq = { headers: req.headers, body: JSON.parse(body) };
      if (failNextChatWith401) { failNextChatWith401 = false; res.writeHead(401); return res.end('{"message":"expired"}'); }
      if (req.headers.authorization !== `Bearer tok-${tokenCalls}`) { res.writeHead(403); return res.end('bad bearer'); }
      const b = lastChatReq.body;
      if (b.stream) {
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        const chunk = (o) => res.write(`data: ${JSON.stringify({ id: 'x', object: 'chat.completion.chunk', choices: [o] })}\n\n`);
        chunk({ index: 0, delta: { role: 'assistant', content: 'Hel' } });
        chunk({ index: 0, delta: { content: 'lo' } });
        chunk({ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_1', type: 'function', function: { name: 'read_file', arguments: '{"pa' } }] } });
        chunk({ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: 'th":"a.ts"}' } }] } });
        chunk({ index: 0, delta: {}, finish_reason: 'tool_calls' });
        res.write('data: [DONE]\n\n');
        return res.end();
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ choices: [{ message: { role: 'assistant', content: 'pong', tool_calls: [{ id: 'call_9', type: 'function', function: { name: 'run', arguments: '{"cmd":"ls"}' } }] }, finish_reason: 'tool_calls' }] }));
    }
    res.writeHead(404); res.end('not found ' + req.url);
  });
});

(async () => {
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  config = {
    baseUrl: base, chatPath: '/v1/chat/completion', tokenUrl: `${base}/as/token.oauth2`,
    clientId: CLIENT_ID, tokenAuth: 'basic', scope: 'ai.gateway', stream: true, sendMaxTokens: true,
    models: [{ id: 'claude-sonnet-4-5', gatewayModel: MODEL, name: 'Sonnet', maxInputTokens: 200000, maxOutputTokens: 16000, toolCalling: true }],
  };
  const store = new Map([[SECRET_CLIENT_SECRET, CLIENT_SECRET]]);
  const secrets = { get: async (k) => store.get(k), store: async (k, v) => store.set(k, v), delete: async (k) => store.delete(k) };
  const logs = []; const log = (m) => logs.push(m);
  const tokens = new TokenManager(secrets, log);
  const provider = new BankGatewayProvider(tokens, secrets, log);
  const modelInfo = { id: 'claude-sonnet-4-5', maxOutputTokens: 16000, capabilities: { toolCalling: true, imageInput: false } };
  const cancel = { onCancellationRequested: () => ({ dispose() {} }), isCancellationRequested: false };
  const messages = [
    { role: 3, content: [new LanguageModelTextPart('You are an agent.')] },
    { role: 1, content: [new LanguageModelTextPart('read a.ts')] },
    { role: 2, content: [new LanguageModelToolCallPart('call_0', 'list_dir', { path: '.' })] },
    { role: 1, content: [new LanguageModelToolResultPart('call_0', [new LanguageModelTextPart('a.ts\nb.ts')]), new LanguageModelTextPart('go on')] },
  ];
  const options = { tools: [{ name: 'read_file', description: 'read', inputSchema: { type: 'object', properties: { path: { type: 'string' } } } }], toolMode: 1, modelOptions: {} };

  // 1) streaming agent turn
  let parts = [];
  await provider.provideLanguageModelChatResponse(modelInfo, messages, options, { report: (p) => parts.push(p) }, cancel);
  assert.strictEqual(tokenCalls, 1, 'one token fetch');
  assert.strictEqual(lastTokenReq.body, 'grant_type=client_credentials&scope=ai.gateway', 'form body has no client_id/secret in basic mode');
  assert.strictEqual(lastChatReq.body.model, MODEL, 'gatewayModel sent');
  assert.strictEqual(lastChatReq.body.max_tokens, 16000);
  assert.deepStrictEqual(lastChatReq.body.messages.map((m) => m.role), ['system', 'user', 'assistant', 'tool', 'user'], 'message roles in order');
  assert.strictEqual(lastChatReq.body.messages[3].tool_call_id, 'call_0');
  assert.strictEqual(lastChatReq.body.tool_choice, 'auto');
  assert.strictEqual(lastChatReq.body.tools[0].function.name, 'read_file');
  const text = parts.filter((p) => p instanceof LanguageModelTextPart).map((p) => p.value).join('');
  const calls = parts.filter((p) => p instanceof LanguageModelToolCallPart);
  assert.strictEqual(text, 'Hello');
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].callId, 'call_1'); assert.strictEqual(calls[0].name, 'read_file'); assert.deepStrictEqual(calls[0].input, { path: 'a.ts' });
  console.log('PASS streaming turn: text + tool call, Basic auth, custom chat path, cached token');

  // 2) second call reuses token
  parts = [];
  await provider.provideLanguageModelChatResponse(modelInfo, messages, options, { report: (p) => parts.push(p) }, cancel);
  assert.strictEqual(tokenCalls, 1, 'token cached');
  console.log('PASS token cache reused');

  // 3) 401 -> refresh -> retry
  failNextChatWith401 = true; parts = [];
  await provider.provideLanguageModelChatResponse(modelInfo, messages, options, { report: (p) => parts.push(p) }, cancel);
  assert.strictEqual(tokenCalls, 2, 'token refreshed after 401');
  assert.strictEqual(lastChatReq.headers.authorization, 'Bearer tok-2');
  assert.strictEqual(parts.filter((p) => p instanceof LanguageModelToolCallPart).length, 1);
  console.log('PASS 401 -> token refresh -> retry');

  // 4) non-streaming path
  config.stream = false; parts = [];
  await provider.provideLanguageModelChatResponse(modelInfo, messages, options, { report: (p) => parts.push(p) }, cancel);
  assert.strictEqual(parts.filter((p) => p instanceof LanguageModelTextPart).map((p) => p.value).join(''), 'pong');
  assert.deepStrictEqual(parts.find((p) => p instanceof LanguageModelToolCallPart).input, { cmd: 'ls' });
  console.log('PASS non-streaming turn');

  // 5) body auth mode sends client_id/secret in the form (fake IdP rejects it, proving the header is absent)
  config.tokenAuth = 'body'; tokens.invalidate();
  await assert.rejects(() => tokens.getToken(), /HTTP 401/);
  assert.match(lastTokenReq.body, /client_id=rbc-client&client_secret=sup3r%3Asecret%2F%3D/);
  assert.strictEqual(lastTokenReq.headers.authorization, undefined);
  console.log('PASS body auth mode puts credentials in the form, no Basic header');

  assert.ok(!logs.some((l) => l.includes(CLIENT_SECRET)), 'secret never logged');
  console.log('PASS secret never appears in logs');
  server.close();
  console.log('\nALL E2E CHECKS PASSED');
})().catch((e) => { console.error('FAIL', e); server.close(); process.exit(1); });
