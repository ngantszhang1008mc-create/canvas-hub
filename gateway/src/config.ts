import { GatewayError } from "./errors";

export const MAX_AMOUNT = 1_000_000_000_000;
export const MAX_USAGE_TOKENS = Number.MAX_SAFE_INTEGER;
// Stable across deployment/model/user changes: changing this creates a new,
// unfunded ledger and abandons the owner's historical lifetime accounting.
export const SINGLETON = "singleton-v1";

export interface ModelPolicy {
  provider: "mock";
  max_input_tokens: number;
  max_output_tokens: number;
  input_microusd_per_million_tokens: number;
  output_microusd_per_million_tokens: number;
}

export interface ToolDefinition {
  description: string;
  parameters: Record<string, unknown>;
}

export interface Policy {
  version: 1;
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

export function price(model: Pick<ModelPolicy, "input_microusd_per_million_tokens" | "output_microusd_per_million_tokens">, input: number, output: number): bigint {
  // Integer arithmetic, rounding up once per request. No floating USD values.
  const numerator = BigInt(input) * BigInt(model.input_microusd_per_million_tokens)
    + BigInt(output) * BigInt(model.output_microusd_per_million_tokens);
  return (numerator + 999_999n) / 1_000_000n;
}

export function parsePolicy(value: unknown): Policy {
  try {
    const p = typeof value === "string" ? JSON.parse(value) : value;
    if (!record(p)) throw new Error();
    exactKeys(p, ["version", "user_daily_microusd", "user_monthly_microusd", "global_monthly_microusd", "global_lifetime_microusd", "session_ttl_seconds", "invite_ttl_max_seconds", "request_timeout_ms", "max_body_bytes", "max_stream_bytes", "user_concurrency", "global_concurrency", "user_requests_per_minute", "ingress_per_minute", "global_ingress_per_minute", "auth_attempts_per_minute", "admin_attempts_per_minute", "max_users", "models", "tools"]);
    if (p.version !== 1) throw new Error();
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
      exactKeys(model, ["provider", "max_input_tokens", "max_output_tokens", "input_microusd_per_million_tokens", "output_microusd_per_million_tokens"]);
      // Fail closed for any unimplemented provider. Adding a paid provider
      // requires a separately reviewed bound and terminal-usage adapter.
      if (model.provider !== "mock" || !integer(model.max_input_tokens, 1, 1_000_000)
        || !integer(model.max_output_tokens, 1, 100_000)) throw new Error();
      for (const rate of ["input_microusd_per_million_tokens", "output_microusd_per_million_tokens"]) {
        if (!integer(model[rate], 1, 1_000_000_000)) throw new Error();
      }
    }
    if (!record(p.tools) || Object.keys(p.tools).length > 32) throw new Error();
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
