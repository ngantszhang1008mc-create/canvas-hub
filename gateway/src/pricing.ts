import type { ModelPolicy } from "./config";

export interface Rates {
  input: number;
  cached_input: number;
  cache_write_30m: number;
  cache_write_12h: number;
  output: number;
}

export interface Pricing {
  unit: "microusd_per_million_tokens";
  long_context_threshold_tokens: number;
  short: Rates;
  long: Rates;
}

export interface MeteredUsage {
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens?: number;
  cache_write_input_tokens?: number;
  cache_write_30m_input_tokens?: number;
  cache_write_12h_input_tokens?: number;
  reasoning_tokens?: number;
}

// Reviewed 2026-11-02 against the official model + prompt caching references.
// Global Standard only; default service tier is explicitly sent. No regional
// endpoint, Fast/Flex/Batch, pro mode, multi-agent, or hosted tools are enabled.
export const LUNA_CONTEXT_TOKENS = 1_050_000;
export const LUNA_OUTPUT_TOKENS = 128_000;
export const LUNA_PRICE_FLOORS: Pricing = {
  unit: "microusd_per_million_tokens",
  long_context_threshold_tokens: 272_000,
  short: { input: 100_000, cached_input: 10_000, cache_write_30m: 125_000, cache_write_12h: 250_000, output: 500_000 },
  long: { input: 200_000, cached_input: 20_000, cache_write_30m: 250_000, cache_write_12h: 500_000, output: 750_000 },
};

export function snapshotPricing(model: ModelPolicy): Pricing {
  if (model.provider === "openai_responses") return structuredClone(model.pricing);
  const rates: Rates = { input: model.input_microusd_per_million_tokens, cached_input: model.input_microusd_per_million_tokens,
    cache_write_30m: model.input_microusd_per_million_tokens, cache_write_12h: model.input_microusd_per_million_tokens, output: model.output_microusd_per_million_tokens };
  return { unit: "microusd_per_million_tokens", long_context_threshold_tokens: Number.MAX_SAFE_INTEGER, short: rates, long: rates };
}

export function reservePrice(model: ModelPolicy, maxOutput: number): bigint {
  const p = snapshotPricing(model);
  // Even though the real adapter explicitly requests 30m cache writes, reserve
  // the most expensive 12h category as well. This protects the owner if a
  // provider reports a different cache lifetime; settlement also trips a breach.
  const maxInput = Math.max(...[p.short, p.long].flatMap(r => [r.input, r.cached_input, r.cache_write_30m, r.cache_write_12h]));
  const maxOutputRate = Math.max(p.short.output, p.long.output);
  return (BigInt(model.max_input_tokens) * BigInt(maxInput) + BigInt(maxOutput) * BigInt(maxOutputRate) + 999_999n) / 1_000_000n;
}

export function settlementPrice(p: Pricing, usage: MeteredUsage): bigint {
  const r = usage.input_tokens > p.long_context_threshold_tokens ? p.long : p.short;
  const cached = BigInt(usage.cached_input_tokens ?? 0);
  const write30 = BigInt(usage.cache_write_30m_input_tokens ?? 0);
  const write12 = BigInt(usage.cache_write_12h_input_tokens ?? 0);
  const ordinary = BigInt(usage.input_tokens) - cached - write30 - write12;
  // Cache reads/writes are disjoint subsets of input; reasoning is already a
  // subset of output_tokens. Never add either category to the token totals.
  const amount = ordinary * BigInt(r.input) + cached * BigInt(r.cached_input)
    + write30 * BigInt(r.cache_write_30m) + write12 * BigInt(r.cache_write_12h)
    + BigInt(usage.output_tokens) * BigInt(r.output);
  return (amount + 999_999n) / 1_000_000n;
}
