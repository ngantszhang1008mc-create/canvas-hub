import type { Env, ModelPolicy, Policy } from "./config";
import type { GenerationRequest } from "./validation";
import type { MeteredUsage } from "./pricing";
import { OpenAIResponsesAdapter } from "./openai-responses";

export type Delta =
  | { type: "text.delta"; delta: string }
  | { type: "tool_call.delta"; index: number; id?: string; name?: string; arguments_delta?: string };

export interface Completion {
  // Only a complete provider stream with terminal authoritative usage may
  // release any reservation. Intermediate usage estimates are not accepted.
  usage: MeteredUsage | null;
  finish_reason: "stop" | "tool_calls" | "length";
  bound_violation?: boolean;
}

export interface PreparedCall {
  stream(signal: AbortSignal): AsyncGenerator<Delta, Completion, void>;
}

export interface ProviderAdapter {
  // MUST be synchronous, local-only, and perform no provider/network dispatch.
  // Any exception here is provably before dispatch. It may inspect prompt data
  // in memory, but must not store it or include it in an exception/log.
  prepare(input: GenerationRequest, model: ModelPolicy, policy: Policy): PreparedCall;
}

export class PreflightError extends Error {
  constructor(public code: "input_too_large" | "provider_unavailable") { super(code); }
}

export class MockAdapter implements ProviderAdapter {
  prepare(input: GenerationRequest, model: ModelPolicy, _policy: Policy): PreparedCall {
    // This adapter defines one mock input token per UTF-8 byte. Its accepted
    // input bound is exact, and includes serialized messages/tool names.
    const inputTokens = new TextEncoder().encode(JSON.stringify({ messages: input.messages, tools: input.tools })).length;
    if (inputTokens > model.max_input_tokens) throw new PreflightError("input_too_large");
    const output = "Offline mock response. No paid provider was contacted.".slice(0, input.max_output_tokens);
    const toolName = input.messages.at(-1)?.role === "tool" ? undefined : input.tools[0];
    const toolId = `mock_${crypto.randomUUID().replaceAll("-", "")}`;
    return {
      async *stream(signal) {
        signal.throwIfAborted();
        // Mock output token is one emitted text/argument character. Tool calls
        // are only returned when the server-enforced output bound permits them.
        if (toolName && input.max_output_tokens >= 2) {
          yield { type: "tool_call.delta", index: 0, id: toolId, name: toolName, arguments_delta: "{" };
          signal.throwIfAborted();
          yield { type: "tool_call.delta", index: 0, arguments_delta: "}" };
          return { usage: { input_tokens: inputTokens, output_tokens: 2 }, finish_reason: "tool_calls" };
        }
        const midpoint = Math.max(1, Math.floor(output.length / 2));
        yield { type: "text.delta", delta: output.slice(0, midpoint) };
        signal.throwIfAborted();
        if (output.length > midpoint) yield { type: "text.delta", delta: output.slice(midpoint) };
        return { usage: { input_tokens: inputTokens, output_tokens: output.length }, finish_reason: output.length < 53 ? "length" : "stop" };
      },
    };
  }
}

export function adapterFor(model: ModelPolicy, env: Env): ProviderAdapter {
  if (model.provider === "mock") return new MockAdapter();
  return new OpenAIResponsesAdapter(env.OPENAI_API_KEY);
}
