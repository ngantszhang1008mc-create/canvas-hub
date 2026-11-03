import { GatewayError } from "./errors";
import { LUNA_CONTEXT_TOKENS, LUNA_OUTPUT_TOKENS, LUNA_PRICE_FLOORS, type Pricing } from "./pricing";

export const MAX_AMOUNT = 1_000_000_000_000;
export const MAX_USAGE_TOKENS = Number.MAX_SAFE_INTEGER;
export const MAX_TOOLS = 128;
export const MAX_TOOL_CALLS = 32;
// Stable across deployment/model/user changes: changing this creates a new,
// unfunded ledger and abandons the owner's historical lifetime accounting.
export const SINGLETON = "singleton-v1";

export interface MockModelPolicy {
  provider: "mock";
  max_input_tokens: number;
  max_output_tokens: number;
  input_microusd_per_million_tokens: number;
  output_microusd_per_million_tokens: number;
}

export interface OpenAIModelPolicy {
  provider: "openai_responses";
  native_model: "gpt-6-luna";
  max_input_tokens: number;
  max_output_tokens: number;
  pricing: Pricing;
}

// Peak, uncached rates and full provider context reviewed 2026-11-03.
export const DEEPSEEK_CONTEXT_TOKENS = 1_048_576;
// The provider allows 393,216; retain the existing Canvas wire limit.
export const DEEPSEEK_OUTPUT_TOKENS = 128_000;
export const DEEPSEEK_INPUT_RATE = 300_000;
export const DEEPSEEK_OUTPUT_RATE = 1_200_000;

export interface NewApiModelPolicy {
  provider: "new_api";
  native_model: "deepseek-flash";
  max_input_tokens: number;
  max_output_tokens: number;
  input_microusd_per_million_tokens: number;
  output_microusd_per_million_tokens: number;
}

export type ModelPolicy = MockModelPolicy | OpenAIModelPolicy | NewApiModelPolicy;

export interface ToolDefinition {
  description: string;
  parameters: Record<string, unknown>;
}

export interface Policy {
  version: 1;
  mode: "mock" | "production";
  user_daily_microusd: number;
  user_monthly_microusd: number;
  global_monthly_microusd: number;
  global_lifetime_microusd: number;
  session_ttl_seconds: number;
  invite_ttl_max_seconds: number;
  request_timeout_ms: number;
  max_body_bytes: number;
  max_stream_bytes: number;
  user_concurrency: number;
  global_concurrency: number;
  user_requests_per_minute: number;
  ingress_per_minute: number;
  global_ingress_per_minute: number;
  auth_attempts_per_minute: number;
  admin_attempts_per_minute: number;
  max_users: number;
  models: Record<string, ModelPolicy>;
  tools: Record<string, ToolDefinition>;
}

export interface Env {
  GATEWAY: DurableObjectNamespace;
  POLICY: unknown;
  ADMIN_SECRET: string;
  OPENAI_API_KEY?: string;
  NEW_API_BASE_URL?: string;
  NEW_API_RELAY_KEY?: string;
  NEW_API_IDENTITY_MODE?: "shared" | "per_user";
  NEW_API_RELAY_KEYS?: string;
}

export function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function exactKeys(value: Record<string, unknown>, allowed: string[], required = allowed): void {
  if (Object.keys(value).some(k => !allowed.includes(k)) || required.some(k => !(k in value))) {
    throw new GatewayError(400, "invalid_fields");
  }
}

export function integer(value: unknown, min: number, max: number): value is number {
  return Number.isSafeInteger(value) && (value as number) >= min && (value as number) <= max;
}

export function price(model: Pick<MockModelPolicy, "input_microusd_per_million_tokens" | "output_microusd_per_million_tokens">, input: number, output: number): bigint {
  // Integer arithmetic, rounding up once per request. No floating USD values.
  const numerator = BigInt(input) * BigInt(model.input_microusd_per_million_tokens)
    + BigInt(output) * BigInt(model.output_microusd_per_million_tokens);
  return (numerator + 999_999n) / 1_000_000n;
}

export function parsePolicy(value: unknown): Policy {
  try {
    const p = typeof value === "string" ? JSON.parse(value) : value;
    if (!record(p)) throw new Error();
    exactKeys(p, ["version", "mode", "user_daily_microusd", "user_monthly_microusd", "global_monthly_microusd", "global_lifetime_microusd", "session_ttl_seconds", "invite_ttl_max_seconds", "request_timeout_ms", "max_body_bytes", "max_stream_bytes", "user_concurrency", "global_concurrency", "user_requests_per_minute", "ingress_per_minute", "global_ingress_per_minute", "auth_attempts_per_minute", "admin_attempts_per_minute", "max_users", "models", "tools"]);
    if (p.version !== 1 || !["mock", "production"].includes(p.mode as string)) throw new Error();
    for (const field of ["user_daily_microusd", "user_monthly_microusd", "global_monthly_microusd", "global_lifetime_microusd"]) {
      if (!integer(p[field], 0, MAX_AMOUNT)) throw new Error();
    }
    for (const field of ["session_ttl_seconds", "invite_ttl_max_seconds"]) {
      if (!integer(p[field], 60, 31_536_000)) throw new Error();
    }
    if (!integer(p.request_timeout_ms, 10, 120_000)
      || !integer(p.max_body_bytes, 256, 1_048_576)
      || !integer(p.max_stream_bytes, 1024, 1_048_576)) throw new Error();
    for (const field of ["user_concurrency", "global_concurrency", "user_requests_per_minute", "ingress_per_minute", "global_ingress_per_minute", "auth_attempts_per_minute", "admin_attempts_per_minute", "max_users"]) {
      if (!integer(p[field], 1, 10_000)) throw new Error();
    }
    if (!record(p.models) || Object.keys(p.models).length < 1 || Object.keys(p.models).length > 16) throw new Error();
    for (const [name, model] of Object.entries(p.models)) {
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(name) || !record(model)) throw new Error();
      if (model.provider === "mock" && p.mode === "mock") {
        exactKeys(model, ["provider", "max_input_tokens", "max_output_tokens", "input_microusd_per_million_tokens", "output_microusd_per_million_tokens"]);
        if (!integer(model.max_input_tokens, 1, 1_000_000) || !integer(model.max_output_tokens, 1, 100_000)) throw new Error();
        for (const rate of ["input_microusd_per_million_tokens", "output_microusd_per_million_tokens"]) {
          if (!integer(model[rate], 1, 1_000_000_000)) throw new Error();
        }
      } else if (model.provider === "openai_responses" && p.mode === "production") {
        exactKeys(model, ["provider", "native_model", "max_input_tokens", "max_output_tokens", "pricing"]);
        if (model.native_model !== "gpt-6-luna" || model.max_input_tokens !== LUNA_CONTEXT_TOKENS
          || !integer(model.max_output_tokens, 16, LUNA_OUTPUT_TOKENS) || !record(model.pricing)) throw new Error();
        const pricing = model.pricing;
        exactKeys(pricing, ["unit", "long_context_threshold_tokens", "short", "long"]);
        if (pricing.unit !== "microusd_per_million_tokens" || pricing.long_context_threshold_tokens !== 272_000) throw new Error();
        for (const tier of ["short", "long"] as const) {
          const rates = pricing[tier];
          if (!record(rates)) throw new Error();
          exactKeys(rates, ["input", "cached_input", "cache_write_30m", "cache_write_12h", "output"]);
          for (const key of Object.keys(LUNA_PRICE_FLOORS[tier]) as (keyof typeof LUNA_PRICE_FLOORS.short)[]) {
            if (!integer(rates[key], LUNA_PRICE_FLOORS[tier][key], 1_000_000_000)) throw new Error();
          }
        }
      } else if (model.provider === "new_api" && p.mode === "production") {
        exactKeys(model, ["provider", "native_model", "max_input_tokens", "max_output_tokens", "input_microusd_per_million_tokens", "output_microusd_per_million_tokens"]);
        if (model.native_model !== "deepseek-flash" || model.max_input_tokens !== DEEPSEEK_CONTEXT_TOKENS
          || !integer(model.max_output_tokens, 1, DEEPSEEK_OUTPUT_TOKENS)
          || !integer(model.input_microusd_per_million_tokens, DEEPSEEK_INPUT_RATE, 1_000_000_000)
          || !integer(model.output_microusd_per_million_tokens, DEEPSEEK_OUTPUT_RATE, 1_000_000_000)) throw new Error();
      } else {
        throw new Error();
      }
    }
    if (!record(p.tools) || Object.keys(p.tools).length > MAX_TOOLS) throw new Error();
    for (const [name, tool] of Object.entries(p.tools)) {
      if (!/^[a-z][a-z0-9_]{0,63}$/.test(name) || !record(tool)) throw new Error();
      exactKeys(tool, ["description", "parameters"]);
      if (typeof tool.description !== "string" || tool.description.length > 2048 || !record(tool.parameters)
        || tool.parameters.type !== "object" || tool.parameters.additionalProperties !== false
        || JSON.stringify(tool.parameters).length > 8192) throw new Error();
    }
    // Clone rather than retaining mutable binding objects.
    return JSON.parse(JSON.stringify(p)) as Policy;
  } catch {
    throw new GatewayError(503, "configuration_error");
  }
}

export function validAdminSecret(secret: unknown): secret is string {
  return typeof secret === "string" && /^[A-Za-z0-9_-]{32,256}$/.test(secret);
}

export function validProviderSecret(secret: unknown): secret is string {
  return typeof secret === "string" && /^[\x21-\x7e]{20,512}$/.test(secret);
}

export function assertConfigured(policy: Policy, env: Env): void {
  if (!validAdminSecret(env.ADMIN_SECRET)) throw new GatewayError(503, "configuration_error");
  for (const model of Object.values(policy.models)) {
    if (model.provider === "openai_responses" && !validProviderSecret(env.OPENAI_API_KEY)) throw new GatewayError(503, "configuration_error");
    if (model.provider === "new_api") {
      newApiEndpoint(env.NEW_API_BASE_URL);
      if (env.NEW_API_IDENTITY_MODE === "per_user") {
        if (env.NEW_API_RELAY_KEY !== undefined) throw new GatewayError(503, "configuration_error");
        newApiRelayKeys(env);
      } else if ((env.NEW_API_IDENTITY_MODE !== undefined && env.NEW_API_IDENTITY_MODE !== "shared")
        || !validProviderSecret(env.NEW_API_RELAY_KEY) || env.NEW_API_RELAY_KEYS !== undefined) {
        throw new GatewayError(503, "configuration_error");
      }
    }
  }
}

export function newApiRelayKeys(env: Env): Record<string, string> {
  try {
    if (typeof env.NEW_API_RELAY_KEYS !== "string" || env.NEW_API_RELAY_KEYS.length > 524_288) throw new Error();
    const keys: unknown = JSON.parse(env.NEW_API_RELAY_KEYS);
    if (!record(keys) || Object.keys(keys).length < 1 || Object.keys(keys).length > 1000
      || Object.entries(keys).some(([ref, key]) => !/^[A-Za-z0-9_-]{1,64}$/.test(ref) || !validProviderSecret(key))) throw new Error();
    return keys as Record<string, string>;
  } catch { throw new GatewayError(503, "configuration_error"); }
}

export function newApiRelayKey(env: Env, reference?: string | null): string | undefined {
  if (env.NEW_API_IDENTITY_MODE !== "per_user") return env.NEW_API_RELAY_KEY;
  const keys = newApiRelayKeys(env);
  // Never fall back from a missing per-user binding to a shared credential.
  return reference && Object.hasOwn(keys, reference) ? keys[reference] : undefined;
}

export function newApiEndpoint(value: unknown): string {
  try {
    if (typeof value !== "string" || value.length > 2048 || value.trim() !== value) throw new Error();
    const url = new URL(value);
    // A server-owned HTTPS origin, never a client URL or an admin endpoint.
    // Private DNS names are allowed for operator-managed relay deployments.
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash
      || url.pathname !== "/" || !url.hostname || (value !== url.origin && value !== `${url.origin}/`)) throw new Error();
    return `${url.origin}/v1/chat/completions`;
  } catch { throw new GatewayError(503, "configuration_error"); }
}
