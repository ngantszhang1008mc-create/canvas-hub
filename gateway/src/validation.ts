import { exactKeys, integer, MAX_TOOLS, MAX_TOOL_CALLS, record, type Policy, type ModelPolicy } from "./config";
import { GatewayError } from "./errors";

export interface ToolCall {
  id: string;
  name: string;
  arguments: string;
}
export type Message =
  | { role: "system" | "user"; content: string }
  | { role: "assistant"; content: string; tool_calls?: ToolCall[] }
  | { role: "tool"; tool_call_id: string; content: string };

export interface GenerationRequest {
  model: string;
  messages: Message[];
  max_output_tokens: number;
  tools: string[];
}

export async function readJson(request: Request, maxBytes: number): Promise<Record<string, unknown>> {
  const contentType = request.headers.get("Content-Type") ?? "";
  if (!/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(contentType)
    || request.headers.has("Content-Encoding")) throw new GatewayError(415, "json_required");
  const contentLength = request.headers.get("Content-Length");
  if (contentLength !== null && (!/^\d+$/.test(contentLength) || Number(contentLength) > maxBytes)) {
    throw new GatewayError(413, "body_too_large");
  }
  if (!request.body) throw new GatewayError(400, "invalid_json");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => { void reader.cancel().catch(() => {}); reject(new GatewayError(408, "body_timeout")); }, 10_000);
  });
  try {
    while (true) {
      const { value, done } = await Promise.race([reader.read(), timeout]);
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        void reader.cancel().catch(() => {});
        throw new GatewayError(413, "body_too_large");
      }
      chunks.push(value);
    }
    const body = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    let parsed: unknown;
    try { parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(body)); }
    catch { throw new GatewayError(400, "invalid_json"); }
    if (!record(parsed)) throw new GatewayError(400, "invalid_json");
    return parsed;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    reader.releaseLock();
  }
}

function text(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length <= max;
}

function callId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}

export function generation(body: Record<string, unknown>, policy: Policy): { input: GenerationRequest; model: ModelPolicy } {
  exactKeys(body, ["model", "messages", "max_output_tokens", "tools"], ["model", "messages", "max_output_tokens"]);
  if (typeof body.model !== "string" || !Object.hasOwn(policy.models, body.model)) throw new GatewayError(400, "model_not_allowed");
  const model = policy.models[body.model];
  if (!integer(body.max_output_tokens, model.provider === "openai_responses" ? 16 : 1, model.max_output_tokens)) throw new GatewayError(400, "invalid_output_limit");
  const tools = body.tools ?? [];
  if (!Array.isArray(tools) || tools.length > MAX_TOOLS || new Set(tools).size !== tools.length
    || tools.some(t => typeof t !== "string" || !Object.hasOwn(policy.tools, t))) throw new GatewayError(400, "tool_not_allowed");
  if (!Array.isArray(body.messages) || body.messages.length < 1 || body.messages.length > 128) throw new GatewayError(400, "invalid_messages");
  const pending = new Set<string>();
  const seenCalls = new Set<string>();
  const messages: Message[] = [];
  for (const m of body.messages) {
    if (!record(m) || !text(m.content, policy.max_body_bytes)) throw new GatewayError(400, "invalid_messages");
    if (m.role === "system" || m.role === "user") {
      exactKeys(m, ["role", "content"]);
      messages.push({ role: m.role, content: m.content });
    } else if (m.role === "assistant") {
      exactKeys(m, ["role", "content", "tool_calls"], ["role", "content"]);
      const calls = m.tool_calls ?? [];
      if (!Array.isArray(calls) || calls.length > MAX_TOOL_CALLS) throw new GatewayError(400, "invalid_tool_calls");
      for (const c of calls) {
        if (!record(c)) throw new GatewayError(400, "invalid_tool_calls");
        exactKeys(c, ["id", "name", "arguments"]);
        // History remains valid when current tools=[] disables new tool use.
        // These are inert function-call records; only newly generated calls
        // must belong to the current server-approved selection.
        if (!callId(c.id) || seenCalls.has(c.id) || typeof c.name !== "string" || !/^[a-z][a-z0-9_]{0,63}$/.test(c.name)
          || !text(c.arguments, 16_384)) throw new GatewayError(400, "invalid_tool_calls");
        let args: unknown;
        try { args = JSON.parse(c.arguments); } catch { throw new GatewayError(400, "invalid_tool_calls"); }
        if (!record(args)) throw new GatewayError(400, "invalid_tool_calls");
        pending.add(c.id);
        seenCalls.add(c.id);
      }
      messages.push({ role: "assistant", content: m.content, ...(calls.length ? { tool_calls: calls as unknown as ToolCall[] } : {}) });
    } else if (m.role === "tool") {
      exactKeys(m, ["role", "tool_call_id", "content"]);
      if (!callId(m.tool_call_id) || !pending.delete(m.tool_call_id)) throw new GatewayError(400, "invalid_tool_result");
      messages.push({ role: "tool", tool_call_id: m.tool_call_id, content: m.content });
    } else {
      throw new GatewayError(400, "invalid_messages");
    }
  }
  if (pending.size) throw new GatewayError(400, "missing_tool_result");
  return { input: { model: body.model, messages, max_output_tokens: body.max_output_tokens, tools: tools as string[] }, model };
}

export function idempotencyKey(request: Request): string {
  const key = request.headers.get("Idempotency-Key") ?? "";
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(key)) throw new GatewayError(400, "invalid_idempotency_key");
  return key;
}
