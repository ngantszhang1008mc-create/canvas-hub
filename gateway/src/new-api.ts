import { integer, MAX_TOOL_CALLS, MAX_USAGE_TOKENS, newApiEndpoint, record, validProviderSecret, type ModelPolicy, type Policy } from "./config";
import { GatewayError } from "./errors";
import { PreflightError, type Completion, type Delta, type PreparedCall, type ProviderAdapter } from "./providers";
import { protocolError, readSse } from "./sse";
import type { GenerationRequest } from "./validation";

const identifier = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);

function fields(value: Record<string, unknown>, allowed: string[]): void {
  if (Object.keys(value).some(key => !allowed.includes(key))) protocolError();
}

function messages(input: GenerationRequest): Record<string, unknown>[] {
  return input.messages.map(message => {
    if (message.role !== "assistant") return { ...message };
    return { role: "assistant", content: message.content,
      ...(message.tool_calls?.length ? { tool_calls: message.tool_calls.map(call => ({
        id: call.id, type: "function", function: { name: call.name, arguments: call.arguments },
      })) } : {}) };
  });
}

// Relay usage is neither an invoice nor evidence that only one upstream attempt
// occurred. Validate it for obvious bound violations, but never use it to refund
// or reduce the full-reservation charge, and never fabricate provider usage.
function reportedBoundViolation(value: unknown, maxInput: number, maxOutput: number): boolean {
  if (!record(value) || !integer(value.prompt_tokens, 0, MAX_USAGE_TOKENS)
    || !integer(value.completion_tokens, 0, MAX_USAGE_TOKENS) || !integer(value.total_tokens, 0, MAX_USAGE_TOKENS)
    || BigInt(value.total_tokens) !== BigInt(value.prompt_tokens) + BigInt(value.completion_tokens)) protocolError();
  let violation = value.prompt_tokens > maxInput || value.completion_tokens > maxOutput;
  if (value.completion_tokens_details !== undefined && value.completion_tokens_details !== null) {
    if (!record(value.completion_tokens_details)) protocolError();
    const reasoning = value.completion_tokens_details.reasoning_tokens;
    if (reasoning !== undefined && (!integer(reasoning, 0, value.completion_tokens))) protocolError();
    if (typeof reasoning === "number" && reasoning > 0) violation = true;
  }
  return violation;
}

/** A dedicated, server-owned relay key. No New API control-plane credentials. */
export class NewApiAdapter implements ProviderAdapter {
  constructor(private apiKey: string | undefined, private baseUrl: string | undefined, private transport: typeof fetch = fetch) {}

  prepare(input: GenerationRequest, model: ModelPolicy, policy: Policy): PreparedCall {
    if (model.provider !== "new_api" || policy.mode !== "production" || model.native_model !== "deepseek-flash"
      || !validProviderSecret(this.apiKey)) throw new PreflightError("provider_unavailable");
    let endpoint: string;
    try { endpoint = newApiEndpoint(this.baseUrl); } catch { throw new PreflightError("provider_unavailable"); }
    const body = JSON.stringify({
      model: model.native_model,
      messages: messages(input),
      ...(input.tools.length ? { tools: input.tools.map(name => ({ type: "function", function: {
        name, description: policy.tools[name].description, parameters: policy.tools[name].parameters,
      } })), tool_choice: "auto" } : {}),
      thinking: { type: "disabled" },
      reasoning_effort: "none",
      max_tokens: input.max_output_tokens,
      stream: true,
      stream_options: { include_usage: true },
    });
    const apiKey = this.apiKey;
    const transport = this.transport;
    const nativeModel = model.native_model;
    const maxInput = model.max_input_tokens;
    const maxOutput = input.max_output_tokens;
    const maxTotalBytes = Math.min(8_388_608, policy.max_stream_bytes * 4 + 65_536);
    const maxEventBytes = Math.min(2_097_152, policy.max_stream_bytes * 2 + 65_536);
    return {
      async *stream(signal): AsyncGenerator<Delta, Completion, void> {
        signal.throwIfAborted();
        // One HTTP dispatch, no SDK retry, redirect or paid fallback. New API
        // itself must also have retries/failover disabled by its operator.
        const response = await transport(endpoint, { method: "POST", redirect: "error", signal,
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
        let responseId: string | undefined, finish: Completion["finish_reason"] | undefined;
        let transportDone = false, usageSeen = false, boundViolation = false, emittedBytes = 0;
        const calls = new Map<number, { id: string; name: string; arguments: string }>();
        const countOutput = (text: string) => {
          emittedBytes += new TextEncoder().encode(text).byteLength;
          if (emittedBytes > policy.max_stream_bytes) protocolError();
        };
        for await (const event of readSse(response.body, signal, maxTotalBytes, maxEventBytes, "chat")) {
          if (event.type === "transport.done") {
            if (!finish || transportDone) protocolError();
            transportDone = true;
            continue;
          }
          if (transportDone || event.object !== "chat.completion.chunk" || !identifier(event.id)
            || (responseId !== undefined && event.id !== responseId) || event.model !== nativeModel
            || !Array.isArray(event.choices) || event.choices.length > 1
            || (event.service_tier !== undefined && event.service_tier !== null && event.service_tier !== "default")) protocolError();
          responseId = event.id;
          if (event.error !== undefined) throw new GatewayError(502, "provider_failed");
          if (event.choices.length === 0) {
            if (!finish || event.usage === undefined || event.usage === null) protocolError();
          } else {
            if (finish) protocolError();
            const choice = event.choices[0];
            if (!record(choice) || choice.index !== 0 || !record(choice.delta)) protocolError();
            const delta = choice.delta;
            fields(delta, ["role", "content", "tool_calls", "reasoning_content"]);
            if ((delta.role !== undefined && delta.role !== null && delta.role !== "assistant")
              || (delta.reasoning_content !== undefined && delta.reasoning_content !== null && delta.reasoning_content !== "")) protocolError();
            if (delta.content !== undefined && delta.content !== null) {
              if (typeof delta.content !== "string") protocolError();
              countOutput(delta.content);
              if (delta.content) yield { type: "text.delta", delta: delta.content };
            }
            if (delta.tool_calls !== undefined && delta.tool_calls !== null) {
              if (!Array.isArray(delta.tool_calls) || delta.tool_calls.length > MAX_TOOL_CALLS) protocolError();
              const seen = new Set<number>();
              for (const fragment of delta.tool_calls) {
                if (!record(fragment) || !integer(fragment.index, 0, MAX_TOOL_CALLS - 1) || seen.has(fragment.index)
                  || !record(fragment.function)) protocolError();
                fields(fragment, ["index", "id", "type", "function"]);
                fields(fragment.function, ["name", "arguments"]);
                seen.add(fragment.index);
                const fn = fragment.function;
                let call = calls.get(fragment.index);
                const first = !call;
                if (!call) {
                  if (fragment.index !== calls.size || !identifier(fragment.id) || fragment.type !== "function"
                    || typeof fn.name !== "string" || !input.tools.includes(fn.name)
                    || [...calls.values()].some(prior => prior.id === fragment.id)) protocolError();
                  call = { id: fragment.id, name: fn.name, arguments: "" };
                  calls.set(fragment.index, call);
                } else if ((fragment.id !== undefined && fragment.id !== call.id)
                  || (fragment.type !== undefined && fragment.type !== "function")
                  || (fn.name !== undefined && fn.name !== call.name)) protocolError();
                if (fn.arguments !== undefined && typeof fn.arguments !== "string") protocolError();
                const args = typeof fn.arguments === "string" ? fn.arguments : "";
                countOutput(args);
                call.arguments += args;
                yield { type: "tool_call.delta", index: fragment.index,
                  ...(first ? { id: call.id, name: call.name } : {}), arguments_delta: args };
              }
            }
            if (choice.finish_reason !== undefined && choice.finish_reason !== null) {
              if (!["stop", "tool_calls", "length"].includes(choice.finish_reason as string)) protocolError();
              finish = choice.finish_reason as Completion["finish_reason"];
              if ((finish === "stop" && calls.size > 0) || (finish === "tool_calls" && calls.size === 0)) protocolError();
              if (finish === "tool_calls") {
                for (const call of calls.values()) {
                  let parsed: unknown;
                  try { parsed = JSON.parse(call.arguments); } catch { protocolError(); }
                  if (!record(parsed)) protocolError();
                }
              }
            }
          }
          if (event.usage !== undefined && event.usage !== null) {
            if (!finish || usageSeen) protocolError();
            usageSeen = true;
            boundViolation = reportedBoundViolation(event.usage, maxInput, maxOutput);
          }
        }
        if (!finish || !transportDone) protocolError();
        return { usage: null, accounting: "full_reservation", finish_reason: finish, bound_violation: boundViolation };
      },
    };
  }
}
