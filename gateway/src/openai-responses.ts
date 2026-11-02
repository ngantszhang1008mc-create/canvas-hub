import { integer, MAX_TOOL_CALLS, MAX_USAGE_TOKENS, record, validProviderSecret, type ModelPolicy, type Policy } from "./config";
import { GatewayError } from "./errors";
import { PreflightError, type Completion, type Delta, type PreparedCall, type ProviderAdapter } from "./providers";
import type { MeteredUsage } from "./pricing";
import { protocolError, readSse } from "./sse";
import type { GenerationRequest } from "./validation";

const ENDPOINT = "https://api.openai.com/v1/responses";
const identifier = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(v);

function inputItems(input: GenerationRequest): Record<string, unknown>[] {
  return input.messages.flatMap(message => {
    if (message.role === "tool") return [{ type: "function_call_output", call_id: message.tool_call_id, output: message.content }];
    if (message.role !== "assistant") return [{ role: message.role, content: message.content }];
    const items: Record<string, unknown>[] = [];
    if (message.content) items.push({ role: "assistant", content: message.content });
    for (const call of message.tool_calls ?? []) items.push({ type: "function_call", call_id: call.id, name: call.name, arguments: call.arguments });
    return items;
  });
}

function authoritativeUsage(value: unknown): MeteredUsage {
  if (!record(value) || !integer(value.input_tokens, 0, MAX_USAGE_TOKENS) || !integer(value.output_tokens, 0, MAX_USAGE_TOKENS)
    || !integer(value.total_tokens, 0, MAX_USAGE_TOKENS)
    || BigInt(value.total_tokens) !== BigInt(value.input_tokens) + BigInt(value.output_tokens)
    || !record(value.input_tokens_details) || !record(value.output_tokens_details)) protocolError();
  const details = value.input_tokens_details;
  const cached = details.cached_tokens, writes = details.cache_write_tokens;
  const reasoning = value.output_tokens_details.reasoning_tokens;
  if (!integer(cached, 0, MAX_USAGE_TOKENS) || !integer(writes, 0, MAX_USAGE_TOKENS)
    || !integer(reasoning, 0, value.output_tokens) || BigInt(cached) + BigInt(writes) > BigInt(value.input_tokens)) protocolError();
  let write30 = writes, write12 = 0;
  if (details.cache_write_tokens_details !== undefined) {
    const byLifetime = details.cache_write_tokens_details;
    if (!record(byLifetime) || !integer(byLifetime.ttl_30m_tokens, 0, MAX_USAGE_TOKENS)
      || !integer(byLifetime.ttl_12h_tokens, 0, MAX_USAGE_TOKENS)
      || BigInt(byLifetime.ttl_30m_tokens) + BigInt(byLifetime.ttl_12h_tokens) !== BigInt(writes)) protocolError();
    write30 = byLifetime.ttl_30m_tokens;
    write12 = byLifetime.ttl_12h_tokens;
  }
  // Older schema examples omit per-TTL details. The authoritative total can be
  // assigned to 30m because this adapter unconditionally requests that lifetime.
  // The total write count itself is mandatory; missing never means zero.
  return { input_tokens: value.input_tokens, output_tokens: value.output_tokens, cached_input_tokens: cached,
    cache_write_input_tokens: writes, cache_write_30m_input_tokens: write30, cache_write_12h_input_tokens: write12, reasoning_tokens: reasoning };
}

type OutputItem =
  | { type: "message"; id: string; parts: Map<number, { type: "output_text" | "refusal"; text: string }> }
  | { type: "function_call"; id: string; callId: string; name: string; arguments: string; index: number };

function matchFinalItem(value: unknown, expected: OutputItem | undefined): void {
  if (!record(value) || !expected || value.id !== expected.id || value.type !== expected.type) protocolError();
  if (expected.type === "function_call") {
    if (value.call_id !== expected.callId || value.name !== expected.name || value.arguments !== expected.arguments) protocolError();
  } else {
    if (value.role !== "assistant" || !Array.isArray(value.content) || value.content.length > 64) protocolError();
    for (const [index, part] of value.content.entries()) {
      if (!record(part) || (part.type !== "output_text" && part.type !== "refusal")) protocolError();
      const text = part.type === "output_text" ? part.text : part.refusal;
      if (typeof text !== "string") protocolError();
      const prior = expected.parts.get(index);
      if (prior ? prior.type !== part.type || prior.text !== text : text !== "") protocolError();
    }
    if ([...expected.parts.keys()].some(index => index >= (value.content as unknown[]).length)) protocolError();
  }
}

export class OpenAIResponsesAdapter implements ProviderAdapter {
  constructor(private apiKey: string | undefined, private transport: typeof fetch = fetch) {}

  prepare(input: GenerationRequest, model: ModelPolicy, policy: Policy): PreparedCall {
    if (model.provider !== "openai_responses" || policy.mode !== "production" || model.native_model !== "gpt-6-luna"
      || !validProviderSecret(this.apiKey)) throw new PreflightError("provider_unavailable");
    // No tokenizer estimate is used. Reservation covers the verified full
    // provider context (including server tool definitions and hidden framing).
    const body = JSON.stringify({
      model: model.native_model,
      input: inputItems(input),
      tools: input.tools.map(name => ({ type: "function", name, description: policy.tools[name].description, parameters: policy.tools[name].parameters, strict: false })),
      tool_choice: input.tools.length ? "auto" : "none",
      parallel_tool_calls: false,
      reasoning: { effort: "none", mode: "standard", context: "current_turn" },
      prompt_cache_options: { mode: "implicit", ttl: "30m" },
      service_tier: "default",
      max_output_tokens: input.max_output_tokens,
      store: false,
      background: false,
      stream: true,
      truncation: "disabled",
    });
    const apiKey = this.apiKey;
    const transport = this.transport;
    const nativeModel = model.native_model;
    const maxTotalBytes = Math.min(8_388_608, policy.max_stream_bytes * 4 + 65_536);
    const maxEventBytes = Math.min(2_097_152, policy.max_stream_bytes * 2 + 65_536);
    return {
      async *stream(signal): AsyncGenerator<Delta, Completion, void> {
        // Exactly one native HTTP dispatch. No SDK retries, redirects, failover,
        // client-supplied URL, auth headers, or continuation identifiers.
        const response = await transport(ENDPOINT, { method: "POST", redirect: "error", signal,
          headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json", Accept: "text/event-stream" }, body });
        if (response.status !== 200) {
          void response.body?.cancel().catch(() => {});
          throw new GatewayError(502, "provider_http_error");
        }
        if (!response.body || !/^text\/event-stream(?:\s*;|$)/i.test(response.headers.get("Content-Type") ?? "")) {
          void response.body?.cancel().catch(() => {});
          protocolError();
        }
        const length = response.headers.get("Content-Length");
        if (length !== null && (!/^\d+$/.test(length) || Number(length) > maxTotalBytes)) {
          void response.body.cancel().catch(() => {});
          protocolError();
        }
        const items = new Map<number, OutputItem>();
        let completion: Completion | undefined, responseId: string | undefined;
        let calls = 0, emittedBytes = 0, transportDone = false;
        const countOutput = (text: string) => {
          emittedBytes += new TextEncoder().encode(text).byteLength;
          if (emittedBytes > policy.max_stream_bytes) protocolError();
        };
        const checkResponse = (r: unknown, terminal: boolean): Record<string, unknown> => {
          if (!record(r) || !identifier(r.id)) protocolError();
          if (responseId !== undefined && responseId !== r.id) protocolError();
          responseId = r.id;
          if ((terminal || r.model !== undefined) && r.model !== nativeModel) protocolError();
          if ((terminal || r.service_tier !== undefined) && r.service_tier !== "default") protocolError();
          if (r.reasoning !== undefined && r.reasoning !== null && !record(r.reasoning)) protocolError();
          if (record(r.reasoning) && ((r.reasoning.effort !== undefined && r.reasoning.effort !== "none")
            || (r.reasoning.mode !== undefined && r.reasoning.mode !== "standard")
            || (r.reasoning.context !== undefined && r.reasoning.context !== "current_turn"))) protocolError();
          return r;
        };
        for await (const event of readSse(response.body, signal, maxTotalBytes, maxEventBytes)) {
          if (event.type === "transport.done") {
            if (!completion || transportDone) protocolError();
            transportDone = true;
            continue;
          }
          if (completion || transportDone) protocolError();
          if (event.type === "response.created" || event.type === "response.in_progress") {
            checkResponse(event.response, false);
          } else if (event.type === "response.output_item.added") {
            if (!integer(event.output_index, 0, 255) || items.has(event.output_index) || !record(event.item) || !identifier(event.item.id)) protocolError();
            const item = event.item;
            if (!identifier(item.id)) protocolError();
            if ([...items.values()].some(prior => prior.id === item.id)) protocolError();
            if (item.type === "function_call") {
              if (++calls > MAX_TOOL_CALLS || !identifier(item.call_id) || typeof item.name !== "string" || !input.tools.includes(item.name)
                || typeof item.arguments !== "string" || [...items.values()].some(prior => prior.type === "function_call" && prior.callId === item.call_id)) protocolError();
              countOutput(item.arguments);
              items.set(event.output_index, { type: "function_call", id: item.id, callId: item.call_id, name: item.name, arguments: item.arguments, index: calls - 1 });
              yield { type: "tool_call.delta", index: calls - 1, id: item.call_id, name: item.name, arguments_delta: item.arguments };
            } else if (item.type === "message" && item.role === "assistant") {
              items.set(event.output_index, { type: "message", id: item.id, parts: new Map() });
            } else {
              // No hosted tools, programs, images, or opaque reasoning items.
              // Reasoning effort none makes this stateless text/function-only
              // contract usable without storing/replaying encrypted reasoning.
              protocolError();
            }
          } else if (event.type === "response.output_text.delta" || event.type === "response.refusal.delta") {
            if (!integer(event.output_index, 0, 255) || !integer(event.content_index, 0, 63) || typeof event.delta !== "string") protocolError();
            const item = items.get(event.output_index);
            if (!item || item.type !== "message" || item.id !== event.item_id) protocolError();
            const type = event.type === "response.output_text.delta" ? "output_text" : "refusal";
            const part = item.parts.get(event.content_index) ?? { type, text: "" };
            if (part.type !== type) protocolError();
            countOutput(event.delta);
            part.text += event.delta;
            item.parts.set(event.content_index, part);
            yield { type: "text.delta", delta: event.delta };
          } else if (event.type === "response.function_call_arguments.delta" || event.type === "response.function_call_arguments.done") {
            if (!integer(event.output_index, 0, 255)) protocolError();
            const item = items.get(event.output_index);
            if (!item || item.type !== "function_call" || item.id !== event.item_id) protocolError();
            if (event.type.endsWith(".done")) {
              if (event.arguments !== item.arguments) protocolError();
            } else {
              if (typeof event.delta !== "string") protocolError();
              countOutput(event.delta);
              item.arguments += event.delta;
              yield { type: "tool_call.delta", index: item.index, arguments_delta: event.delta };
            }
          } else if (event.type === "response.output_item.done") {
            if (!integer(event.output_index, 0, 255)) protocolError();
            matchFinalItem(event.item, items.get(event.output_index));
          } else if (["response.content_part.added", "response.content_part.done", "response.output_text.done", "response.refusal.done", "response.output_text.annotation.added"].includes(event.type as string)) {
            if (!integer(event.output_index, 0, 255) || !integer(event.content_index, 0, 63)) protocolError();
            const item = items.get(event.output_index);
            if (!item || item.type !== "message" || item.id !== event.item_id) protocolError();
            if (event.type === "response.content_part.added" || event.type === "response.content_part.done") {
              if (!record(event.part) || !["output_text", "refusal"].includes(event.part.type as string)) protocolError();
            }
            if (event.type === "response.output_text.done" || event.type === "response.refusal.done") {
              const finalText = event.type === "response.output_text.done" ? event.text : event.refusal;
              if (typeof finalText !== "string" || finalText !== (item.parts.get(event.content_index)?.text ?? "")) protocolError();
            }
          } else if (event.type === "response.completed" || event.type === "response.incomplete") {
            const result = checkResponse(event.response, true);
            const incomplete = event.type === "response.incomplete";
            if (result.status !== (incomplete ? "incomplete" : "completed") || (incomplete && (!record(result.incomplete_details) || result.incomplete_details.reason !== "max_output_tokens"))
              || !Array.isArray(result.output) || result.output.length !== items.size) protocolError();
            result.output.forEach((item, index) => matchFinalItem(item, items.get(index)));
            const usage = authoritativeUsage(result.usage);
            completion = { usage, finish_reason: incomplete ? "length" : calls > 0 ? "tool_calls" : "stop",
              bound_violation: (usage.cache_write_12h_input_tokens ?? 0) > 0 };
            // Read to clean EOF. A malformed suffix or disconnect after an
            // apparently complete event remains conservatively unresolved.
          } else if (event.type === "response.failed" || event.type === "error") {
            throw new GatewayError(502, "provider_failed");
          } else {
            protocolError();
          }
        }
        if (!completion) protocolError();
        return completion;
      },
    };
  }
}
