# Bank AI Gateway (Claude) — language model provider for Copilot Chat

A small VS Code extension that puts the Claude models served by the bank's internal API gateway (Bedrock-backed, OpenAI-compatible chat completions) into the Copilot Chat model picker, so they can be used in chat, plan and **agent mode** — billed through the gateway, not against Copilot quotas.

It solves the one thing Copilot's built-in "Custom Endpoint" provider cannot do: the gateway authenticates with an **OAuth2 client-credentials token that expires**. This extension gets the token itself, caches it, refreshes it before expiry, and retries once on a 401.

```
Copilot Chat (agent mode)
      │  VS Code LanguageModelChatProvider API  (messages, tools)
      ▼
this extension ── client-credentials token (cached, auto-refresh) ──▶ tokenUrl  (PingFederate /as/token.oauth2 or Entra ID)
      │  Authorization: Bearer <token>   +  optional API-key header
      ▼
<baseUrl><chatPath>  (OpenAI wire format, SSE streaming, tool_calls)  ──▶  Claude on Bedrock
```

No runtime dependencies. No telemetry. Secrets live in the OS keychain via VS Code SecretStorage.

## Requirements

- VS Code 1.105 or newer with GitHub Copilot Chat.
- The org's Copilot policy **"Bring Your Own Language Model Key in VS Code"** must be on (it is by default). If it has been disabled, provider extensions are not offered in the picker — that is a decision for the Copilot admins, not something to work around.
- Network path from the laptop to `tokenUrl` and `baseUrl` (through the corporate proxy is fine, see below).

## Build and run

```bash
npm install
npm run compile                                   # type-check + emit ./out
```

`npm test` compiles and runs `test/e2e.js`: it stubs the `vscode` module, starts a fake PingFederate token endpoint and a fake streaming gateway on localhost, and drives a full agent-mode turn (system/user/assistant/tool messages, streamed text + tool call, 401 refresh-and-retry, non-streaming path). No network needed.

Try it without installing anything: open the folder in VS Code and press **F5**. That launches an Extension Development Host with the extension loaded; configure it there (next section) and use Copilot Chat in that window.

Package for internal distribution:

```bash
npx vsce package --allow-missing-repository       # -> bank-gateway-lm-provider-0.0.1.vsix
code --install-extension bank-gateway-lm-provider-0.0.1.vsix
```

If the org restricts extensions with an allowlist (`extensions.allowed` policy), the `publisher` in `package.json` must be an allowed publisher id — change `your-bank` before packaging and go through the normal internal-extension approval.

## Configure

`settings.json` (user or workspace). Nothing secret goes here.

### Example: PingFederate SSO + Bedrock Claude gateway

```jsonc
{
  "bankGateway.baseUrl":   "https://<gateway-host>",              // no trailing slash
  "bankGateway.chatPath":  "/v1/chat/completions",                 // plural; the singular form is rejected with 400 "Unknown endpoint"
  "bankGateway.tokenUrl":  "https://<sso-host>/as/token.oauth2",   // PingFederate client-credentials endpoint
  "bankGateway.clientId":  "<client-id>",
  "bankGateway.tokenAuth": "basic",                                // PingFederate usually wants HTTP Basic (curl -u id:secret)
  "bankGateway.scope":     "",                                     // set if the gateway team issued a scope for the client
  "bankGateway.models": [
    {
      "id": "claude-sonnet-4-5",
      "gatewayModel": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",   // the id the gateway expects; copy it verbatim from the gateway catalogue
      "name": "Claude Sonnet 4.5 (Bank Gateway)",
      "maxInputTokens": 200000,
      "maxOutputTokens": 16000,
      "toolCalling": true
    },
    {
      "id": "claude-opus",
      "gatewayModel": "<opus id from the gateway catalogue>",
      "name": "Claude Opus (Bank Gateway)",
      "maxInputTokens": 200000,
      "maxOutputTokens": 16000,
      "toolCalling": true
    }
  ]
}
```

If the token call fails with 401, switch `tokenAuth` to `"body"` (or back) — PingFederate clients are configured for one of the two methods.

### Example: Entra ID + Azure APIM gateway

```jsonc
{
  "bankGateway.baseUrl":  "https://<gateway-host>/<route>",       // POSTs to <baseUrl>/chat/completions
  "bankGateway.tokenUrl": "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token",
  "bankGateway.clientId": "<client-id>",
  "bankGateway.scope":    "api://<gateway-app-id>/.default",      // v1 endpoints: leave empty, set bankGateway.resource
  "bankGateway.apiKeyHeader": "Ocp-Apim-Subscription-Key",         // only if the gateway also wants a subscription key
  "bankGateway.models": [
    {
      "id": "claude-sonnet-4-6",
      "name": "Claude Sonnet 4.6 (Bank Gateway)",
      "maxInputTokens": 200000,
      "maxOutputTokens": 16000,
      "toolCalling": true
    }
  ]
}
```

Then, from the Command Palette:

1. **Bank AI Gateway: Set OAuth Client Secret** (and **Set Gateway API Key** if `apiKeyHeader` is used). Stored in the OS keychain.
2. **Bank AI Gateway: Test Connection** — fetches a token and sends a one-line completion. Errors are shown with the gateway's response in the *Bank AI Gateway* output channel.
3. In Copilot Chat, open the model picker → **Manage Models…** → **Bank AI Gateway** → tick the model(s).
4. Pick the model in agent mode.

If Copilot's own models are unavailable (quota exhausted), also set **Chat: Utility Small Model** in Settings to this model so VS Code's helper flows (titles, intent detection) keep working.

### All settings

| Setting | Default | Purpose |
|---|---|---|
| `bankGateway.baseUrl` | `""` | Gateway base URL |
| `bankGateway.chatPath` | `/chat/completions` | Path appended to `baseUrl` |
| `bankGateway.tokenUrl` | `""` | OAuth2 token endpoint |
| `bankGateway.clientId` | `""` | OAuth2 client id (secret is set by command) |
| `bankGateway.tokenAuth` | `body` | `body` = client_secret_post form fields (Entra ID); `basic` = HTTP Basic header (PingFederate, Apigee, Kong) |
| `bankGateway.scope` / `resource` | `""` | `scope` for v2 endpoints / PingFederate, `resource` for Entra v1 |
| `bankGateway.apiKeyHeader` | `""` | Header name for an optional subscription key (value set by command) |
| `bankGateway.extraHeaders` | `{}` | Non-secret headers added to every request |
| `bankGateway.stream` | `true` | SSE streaming; set `false` if the gateway does not stream |
| `bankGateway.sendMaxTokens` | `true` | Send `max_tokens = maxOutputTokens` (Anthropic-backed gateways usually need it) |
| `bankGateway.debugLogBodies` | `false` | Log full prompts/responses locally to the output channel |
| `bankGateway.models[]` | one Claude entry | `id`, `name`, `maxInputTokens`, `maxOutputTokens`, `toolCalling`, `imageInput`, optional `gatewayModel` (name the gateway expects, if different from `id`) |

## Gateway compatibility checklist

These are the things that decide whether agent mode works; test them with the platform team if the extension misbehaves.

- **Streaming**: `stream: true` must return SSE (`data:` lines, `[DONE]` terminator). If the gateway buffers or rejects it, set `bankGateway.stream: false` — responses then arrive in one piece, which agent mode tolerates.
- **Tool calling**: the request carries `tools` and `tool_choice`; the response must return `tool_calls` (in `delta` chunks with an `index` when streaming). Without this, agent mode cannot edit files or run commands.
- **`role: "tool"` messages** with `tool_call_id` must be accepted on the next turn.
- **`tool_choice: "required"`**: some gateways only accept `auto`. If yours rejects it, change `toToolChoice` in `src/openai.ts` to always return `auto`.
- **`max_tokens`**: turn `sendMaxTokens` off if the gateway rejects it or enforces a lower cap.
- **Request size and timeouts**: agent turns can be 100k+ tokens and stream for minutes; APIM-style request-size and timeout policies may need raising.
- **Model name**: set `gatewayModel` to whatever the gateway calls the model. Bedrock inference-profile ids look like `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (hyphens, `v1:0`); some gateways rename them — copy the id from the gateway's model catalogue rather than typing it from memory.
- **Token endpoint auth**: PingFederate (`/as/token.oauth2`) clients are configured for either `client_secret_basic` or `client_secret_post`; `bankGateway.tokenAuth` selects which one the extension uses.

## Notes for security review

- **Secrets**: the client secret and optional API key are stored with VS Code `SecretStorage` (Windows Credential Manager / macOS Keychain / libsecret). They are never written to settings, never logged, and can be wiped with *Clear Stored Secrets*. Access tokens are held in memory only.
- **Network**: requests go through Node's `http`/`https` modules, which VS Code patches so the user's proxy settings (`http.proxy`, system proxy, PAC) and `http.systemCertificates` apply. The only destinations are `tokenUrl` and `baseUrl`.
- **Data**: whatever Copilot Chat decides to send to the model (prompt, selected code, tool results) goes to the gateway — the same data path as any other approved gateway client. The extension adds no other destinations and no telemetry.
- **Dependencies**: none at runtime; dev-only TypeScript, `@types/*` and `vsce`.
- **Logging**: metadata only (URL, model, message/tool counts, HTTP status) unless `debugLogBodies` is switched on locally.

## Known gaps

- Token counting is a 4-characters-per-token heuristic; VS Code uses it for context budgeting only.
- Image input is off by default; enable `imageInput` per model if the gateway maps `image_url` content to Claude.
- Only one automatic retry (on 401). Add backoff on 429/5xx if the gateway rate-limits.
- Extended thinking / reasoning content is not surfaced.
- Copilot Chat sends the system prompt with a role value outside VS Code's finalized `User`/`Assistant` enum; it is mapped to `role: "system"`. Revisit if the API changes.

## Layout

```
src/extension.ts   activation, commands, connection test
src/provider.ts    LanguageModelChatProvider implementation
src/openai.ts      VS Code <-> OpenAI translation, streaming accumulator
src/auth.ts        client-credentials token manager, SecretStorage helpers
src/http.ts        http/https helpers, SSE parser
test/e2e.js        offline smoke test against a mock token endpoint + gateway (npm test)
```
