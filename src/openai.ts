/**
 * Translation layer: VS Code LanguageModelChat* types <-> OpenAI chat
 * completions wire format (what the gateway's /chat/completions speaks).
 *
 * The parts that matter for agent mode are tool calls: VS Code hands us
 * LanguageModelToolCallPart (assistant asked for a tool) and
 * LanguageModelToolResultPart (result of running it); OpenAI expects
 * `tool_calls` on the assistant message and a `role: "tool"` message per
 * result. Order must be preserved: assistant(tool_calls) -> tool -> tool ...
 */
import * as vscode from 'vscode';

// ---------- OpenAI wire types (only what we use) ----------

export interface OAIToolCall {
  id: string;
  type: 'function';
  function: { name: string; arguments: string };
}

export type OAIContentPart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } };

export interface OAIMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | OAIContentPart[] | null;
  tool_calls?: OAIToolCall[];
  tool_call_id?: string;
  name?: string;
}

export interface OAITool {
  type: 'function';
  function: { name: string; description?: string; parameters: object };
}

// ---------- Messages ----------

function textOfToolResult(part: vscode.LanguageModelToolResultPart): string {
  const pieces: string[] = [];
  for (const item of part.content) {
    if (item instanceof vscode.LanguageModelTextPart) {
      pieces.push(item.value);
    } else if (item instanceof vscode.LanguageModelPromptTsxPart) {
      pieces.push(JSON.stringify(item.value));
    } else if (item instanceof vscode.LanguageModelDataPart) {
      pieces.push(`[binary ${item.mimeType}, ${item.data.byteLength} bytes omitted]`);
    } else if (typeof item === 'string') {
      pieces.push(item);
    } else {
      pieces.push(JSON.stringify(item));
    }
  }
  return pieces.join('\n');
}

function base64(data: Uint8Array): string {
  return Buffer.from(data).toString('base64');
}

/**
 * Convert VS Code request messages to OpenAI messages.
 * Roles: 1 = User, 2 = Assistant. Copilot Chat may also send a system role
 * (value 3 in VS Code's proposed API); anything unknown is treated as system.
 */
export function toOpenAIMessages(
  messages: readonly vscode.LanguageModelChatRequestMessage[],
  allowImages: boolean
): OAIMessage[] {
  const out: OAIMessage[] = [];

  for (const msg of messages) {
    const role = msg.role as number;

    if (role === vscode.LanguageModelChatMessageRole.Assistant) {
      let text = '';
      const toolCalls: OAIToolCall[] = [];
      for (const part of msg.content) {
        if (part instanceof vscode.LanguageModelTextPart) {
          text += part.value;
        } else if (part instanceof vscode.LanguageModelToolCallPart) {
          toolCalls.push({
            id: part.callId,
            type: 'function',
            function: { name: part.name, arguments: JSON.stringify(part.input ?? {}) },
          });
        }
        // Data parts in assistant turns are not sent back.
      }
      const m: OAIMessage = { role: 'assistant', content: text.length ? text : null };
      if (toolCalls.length) {
        m.tool_calls = toolCalls;
      }
      if (text.length || toolCalls.length) {
        out.push(m);
      }
      continue;
    }

    if (role === vscode.LanguageModelChatMessageRole.User) {
      // Tool results first (they must directly follow the assistant tool_calls),
      // then any user text/images as a single user message.
      const userParts: OAIContentPart[] = [];
      for (const part of msg.content) {
        if (part instanceof vscode.LanguageModelToolResultPart) {
          out.push({ role: 'tool', tool_call_id: part.callId, content: textOfToolResult(part) });
        } else if (part instanceof vscode.LanguageModelTextPart) {
          userParts.push({ type: 'text', text: part.value });
        } else if (part instanceof vscode.LanguageModelDataPart) {
          if (allowImages && part.mimeType.startsWith('image/')) {
            userParts.push({
              type: 'image_url',
              image_url: { url: `data:${part.mimeType};base64,${base64(part.data)}` },
            });
          } else if (part.mimeType.startsWith('text/') || part.mimeType.includes('json')) {
            userParts.push({ type: 'text', text: Buffer.from(part.data).toString('utf8') });
          }
          // Other binary data is dropped.
        }
      }
      if (userParts.length) {
        const onlyText = userParts.every((p) => p.type === 'text');
        out.push({
          role: 'user',
          content: onlyText
            ? userParts.map((p) => (p.type === 'text' ? p.text : '')).join('')
            : userParts,
          ...(msg.name ? { name: msg.name } : {}),
        });
      }
      continue;
    }

    // System (or unknown) role: text only.
    const sys = msg.content
      .filter((p): p is vscode.LanguageModelTextPart => p instanceof vscode.LanguageModelTextPart)
      .map((p) => p.value)
      .join('');
    if (sys.length) {
      out.push({ role: 'system', content: sys });
    }
  }

  return out;
}

// ---------- Tools ----------

export function toOpenAITools(tools: readonly vscode.LanguageModelChatTool[] | undefined): OAITool[] | undefined {
  if (!tools || tools.length === 0) {
    return undefined;
  }
  return tools.map((t) => ({
    type: 'function',
    function: {
      name: t.name,
      description: t.description,
      parameters: (t.inputSchema as object | undefined) ?? { type: 'object', properties: {} },
    },
  }));
}

export function toToolChoice(mode: vscode.LanguageModelChatToolMode, hasTools: boolean): 'auto' | 'required' | undefined {
  if (!hasTools) {
    return undefined;
  }
  return mode === vscode.LanguageModelChatToolMode.Required ? 'required' : 'auto';
}

// ---------- Response handling ----------

interface PendingToolCall {
  id: string;
  name: string;
  args: string;
}

/**
 * Accumulates streamed deltas and reports VS Code response parts. Text is
 * reported as it arrives; tool calls are buffered until their JSON arguments
 * are complete (finish_reason or end of stream) and then reported once.
 */
export class ResponseAccumulator {
  private readonly pending = new Map<number, PendingToolCall>();
  private flushed = false;

  constructor(private readonly progress: vscode.Progress<vscode.LanguageModelResponsePart>) {}

  /** Handle one streamed chunk (a chat.completion.chunk object). */
  handleStreamChunk(chunk: unknown): void {
    const choice = (chunk as { choices?: Array<{ delta?: Record<string, unknown>; finish_reason?: string | null }> })
      ?.choices?.[0];
    if (!choice) {
      return;
    }
    const delta = choice.delta ?? {};

    const content = delta['content'];
    if (typeof content === 'string' && content.length > 0) {
      this.progress.report(new vscode.LanguageModelTextPart(content));
    }

    const toolCalls = delta['tool_calls'];
    if (Array.isArray(toolCalls)) {
      for (const tc of toolCalls as Array<{ index?: number; id?: string; function?: { name?: string; arguments?: string } }>) {
        const index = tc.index ?? 0;
        let entry = this.pending.get(index);
        if (!entry) {
          entry = { id: '', name: '', args: '' };
          this.pending.set(index, entry);
        }
        if (tc.id) {
          entry.id = tc.id;
        }
        if (tc.function?.name) {
          entry.name = tc.function.name;
        }
        if (tc.function?.arguments) {
          entry.args += tc.function.arguments;
        }
      }
    }

    if (choice.finish_reason) {
      this.flush();
    }
  }

  /** Handle a complete (non-streaming) chat.completion object. */
  handleFullResponse(body: unknown): void {
    const message = (body as { choices?: Array<{ message?: { content?: string | null; tool_calls?: OAIToolCall[] } }> })
      ?.choices?.[0]?.message;
    if (!message) {
      return;
    }
    if (typeof message.content === 'string' && message.content.length > 0) {
      this.progress.report(new vscode.LanguageModelTextPart(message.content));
    }
    for (const [i, tc] of (message.tool_calls ?? []).entries()) {
      this.pending.set(i, { id: tc.id, name: tc.function.name, args: tc.function.arguments });
    }
    this.flush();
  }

  /** Emit any buffered tool calls. Safe to call more than once. */
  flush(): void {
    if (this.flushed && this.pending.size === 0) {
      return;
    }
    const ordered = [...this.pending.entries()].sort((a, b) => a[0] - b[0]);
    for (const [index, tc] of ordered) {
      let input: object = {};
      if (tc.args.trim().length > 0) {
        try {
          input = JSON.parse(tc.args);
        } catch {
          input = { _unparsedArguments: tc.args };
        }
      }
      this.progress.report(
        new vscode.LanguageModelToolCallPart(tc.id || `call_${index}_${Date.now()}`, tc.name, input)
      );
    }
    this.pending.clear();
    this.flushed = true;
  }
}
