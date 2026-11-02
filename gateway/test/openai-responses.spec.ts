import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import productionExample from "../policy.production.example.json";
import { assertConfigured, parsePolicy, type Policy } from "../src/config";
import { OpenAIResponsesAdapter } from "../src/openai-responses";
import { generation } from "../src/validation";
import { allowance, bindings, doc, enroll, events, fixture, inside, respond as gatewayRespond, status } from "./harness";

const KEY = "test_only_never_valid_openai_credential_00000000000";
const INPUT = { model: "luna", messages: [{ role: "user", content: "Synthetic text only." }], max_output_tokens: 128, tools: [] as string[] };
const RESERVE = "525096"; // Full context at worst 12h long rate + 128 output.
const respond = (stub: DurableObjectStub, token: string, key = crypto.randomUUID(), body: unknown = INPUT) =>
  gatewayRespond(stub, token, key, body, "production");

beforeEach(() => {
  // Any forgotten fixture is a hard test failure, never a live API request.
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("Network forbidden in offline adapter tests"))));
});
afterEach(() => { vi.unstubAllGlobals(); });

function usage(input = 1000, output = 10, cached = 200, writes = 100, write12 = 0) {
  return { input_tokens: input, output_tokens: output, total_tokens: input + output,
    input_tokens_details: { cached_tokens: cached, cache_write_tokens: writes,
      cache_write_tokens_details: { ttl_30m_tokens: writes - write12, ttl_12h_tokens: write12 } },
    output_tokens_details: { reasoning_tokens: Math.min(4, output) } };
}

function streamEvents(options: { tool?: string; incomplete?: boolean; usage?: ReturnType<typeof usage>; model?: string; tier?: string } = {}) {
  const response = { id: "resp_synthetic", model: options.model ?? "gpt-6-luna", service_tier: options.tier ?? "default", reasoning: { effort: "none" } };
  const text = "Synthetic 學 response.";
  const item = options.tool
    ? { type: "function_call", id: "fc_synthetic", call_id: "call_synthetic", name: options.tool, arguments: options.incomplete ? "{" : "{}", status: "completed" }
    : { type: "message", id: "msg_synthetic", role: "assistant", content: [{ type: "output_text", text, annotations: [] }], status: "completed" };
  return [
    { type: "response.created", response: { ...response, status: "in_progress", output: [] } },
    { type: "response.output_item.added", output_index: 0, item: options.tool ? { ...item, arguments: "", status: "in_progress" } : { ...item, content: [], status: "in_progress" } },
    ...(options.tool ? [{ type: "response.function_call_arguments.delta", output_index: 0, item_id: "fc_synthetic", delta: item.arguments }]
      : [{ type: "response.output_text.delta", output_index: 0, content_index: 0, item_id: "msg_synthetic", delta: text }]),
    { type: "response.output_item.done", output_index: 0, item },
    { type: options.incomplete ? "response.incomplete" : "response.completed", response: { ...response,
      status: options.incomplete ? "incomplete" : "completed", incomplete_details: options.incomplete ? { reason: "max_output_tokens" } : null,
      output: [item], usage: options.usage ?? usage() } },
  ];
}

function sse(values: unknown[]): Response {
  const bytes = new TextEncoder().encode(values.map(value => `data: ${JSON.stringify(value)}\r\n\r\n`).join(""));
  // Deliberately split UTF-8 sequences and CRLF delimiters across reads.
  return new Response(new ReadableStream<Uint8Array>({ start(controller) {
    for (let offset = 0; offset < bytes.length; offset += 7) controller.enqueue(bytes.slice(offset, offset + 7));
    controller.close();
  } }), { headers: { "Content-Type": "text/event-stream" } });
}

async function realFixture(upstream: typeof fetch, patch: Partial<Policy> = {}) {
  const p = parsePolicy(productionExample);
  const stub = await fixture({ ...p, user_daily_microusd: 2_000_000, user_monthly_microusd: 4_000_000,
    global_monthly_microusd: 10_000_000, global_lifetime_microusd: 20_000_000, ...patch });
  await inside(stub, i => { i.adapter = () => new OpenAIResponsesAdapter(KEY, upstream); });
  return { stub, user: await enroll(stub) };
}

describe("real OpenAI HTTP adapter with a hard-blocked network", () => {
  it("fails closed without production config/key and rejects insufficient bounds, rate floors and output1–15", () => {
    const p = parsePolicy(productionExample);
    expect(() => assertConfigured(p, { ...bindings, OPENAI_API_KEY: undefined })).toThrow("configuration_error");
    expect(() => parsePolicy({ ...productionExample, mode: "mock" })).toThrow("configuration_error");
    const smaller = structuredClone(productionExample);
    smaller.models.luna.max_input_tokens--;
    expect(() => parsePolicy(smaller)).toThrow("configuration_error");
    const underpriced = structuredClone(productionExample);
    underpriced.models.luna.pricing.long.cache_write_12h--;
    expect(() => parsePolicy(underpriced)).toThrow("configuration_error");
    expect(() => generation({ ...INPUT, max_output_tokens: 15 }, p)).toThrow("invalid_output_limit");
    expect(() => new OpenAIResponsesAdapter(undefined).prepare({ ...INPUT, messages: [{ role: "user", content: "synthetic" }] }, p.models.luna, p)).toThrow("provider_unavailable");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("uses one fixed safe HTTP request, decodes split SSE and settles cache/reasoning usage exactly once", async () => {
    let sent: Record<string, any> | undefined;
    const upstream = vi.fn<typeof fetch>(async (url, init) => {
      expect(url).toBe("https://api.openai.com/v1/responses");
      expect(init).toMatchObject({ method: "POST", redirect: "error" });
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      expect(new Headers(init?.headers).get("Authorization")).toBe(`Bearer ${KEY}`);
      sent = JSON.parse(init!.body as string);
      const counts = usage();
      // Older authoritative payload shape: ttl details absent, fixed30m applies.
      delete (counts.input_tokens_details as Partial<typeof counts.input_tokens_details>).cache_write_tokens_details;
      return sse(streamEvents({ usage: counts }));
    });
    const { stub, user } = await realFixture(upstream);
    const all = await events(await respond(stub, user.token, crypto.randomUUID(), INPUT));
    expect(sent).toMatchObject({ model: "gpt-6-luna", service_tier: "default", max_output_tokens: 128, store: false, stream: true, background: false,
      reasoning: { effort: "none", mode: "standard", context: "current_turn" }, prompt_cache_options: { mode: "implicit", ttl: "30m" }, truncation: "disabled", tool_choice: "none" });
    expect(sent).not.toHaveProperty("previous_response_id");
    expect(all[0].reserved_microusd).toBe(RESERVE);
    expect(all.filter(e => e.type === "text.delta").map(e => e.delta).join("")).toBe("Synthetic 學 response.");
    expect(all.at(-1)).toMatchObject({ status: "completed", charged_microusd: "90", usage: { input_tokens: 1000, output_tokens: 10,
      cached_input_tokens: 200, cache_write_input_tokens: 100, cache_write_30m_input_tokens: 100, cache_write_12h_input_tokens: 0, reasoning_tokens: 4 } });
    expect((await status(stub)).lifetime).toMatchObject({ spent_microusd: "90", held_microusd: "0" });
    expect(upstream).toHaveBeenCalledTimes(1);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("supports78 approved tools and stateless function history when tools=[] disables new calls", async () => {
    const tools = Object.fromEntries(Array.from({ length: 78 }, (_, n) => [`tool_${n}`, { description: "Synthetic local tool.", parameters: { type: "object", properties: {}, additionalProperties: false } }]));
    let round = 0;
    const bodies: any[] = [];
    const upstream = vi.fn<typeof fetch>(async (_url, init) => {
      bodies.push(JSON.parse(init!.body as string));
      return sse(streamEvents(++round === 1 ? { tool: "tool_0" } : {}));
    });
    const { stub, user } = await realFixture(upstream, { tools });
    const info = await allowance(stub, user.token);
    expect(info).toMatchObject({ mode: "production", capabilities: { protocol_version: 1, max_tools: 128, max_tool_calls_per_response: 32 }, models: [{ id: "luna", min_output_tokens: 16, max_output_tokens: 1024 }] });
    expect(info.tools).toHaveLength(78);
    const firstBody = { ...INPUT, tools: Object.keys(tools) };
    const first = await events(await respond(stub, user.token, crypto.randomUUID(), firstBody));
    expect(first.at(-1).finish_reason).toBe("tool_calls");
    expect(bodies[0].tools).toHaveLength(78);
    expect(bodies[0].tools.every((tool: any) => tool.type === "function" && tool.strict === false)).toBe(true);
    const continuation = { ...INPUT, tools: [], messages: [...INPUT.messages,
      { role: "assistant", content: "", tool_calls: [{ id: "call_synthetic", name: "tool_0", arguments: "{}" }] },
      { role: "tool", tool_call_id: "call_synthetic", content: "Synthetic tool result." }] };
    const second = await events(await respond(stub, user.token, crypto.randomUUID(), continuation));
    expect(second.at(-1).finish_reason).toBe("stop");
    expect(bodies[1]).toMatchObject({ tools: [], tool_choice: "none", input: [
      { role: "user", content: "Synthetic text only." },
      { type: "function_call", call_id: "call_synthetic", name: "tool_0", arguments: "{}" },
      { type: "function_call_output", call_id: "call_synthetic", output: "Synthetic tool result." },
    ] });
    expect(upstream).toHaveBeenCalledTimes(2);
  });

  it("prices the full long-context request and max-output terminal, and records unexpected12h writes before freezing", async () => {
    const first = await realFixture(vi.fn<typeof fetch>(async () => sse(streamEvents({ incomplete: true, tool: "demo", usage: usage(300_000, 20, 20_000, 10_000) }))), {
      tools: { demo: { description: "Synthetic function.", parameters: { type: "object", properties: {}, additionalProperties: false } } },
    });
    const result = (await events(await respond(first.stub, first.user.token, crypto.randomUUID(), { ...INPUT, tools: ["demo"] }))).at(-1);
    expect(result).toMatchObject({ status: "completed", finish_reason: "length", charged_microusd: "56915" });
    const second = await realFixture(vi.fn<typeof fetch>(async () => sse(streamEvents({ usage: usage(200, 10, 0, 100, 100) }))));
    const overrun = (await events(await respond(second.stub, second.user.token, crypto.randomUUID(), INPUT))).at(-1);
    expect(overrun).toMatchObject({ status: "overrun", error: "provider_bound_exceeded" });
    expect((await status(second.stub))).toMatchObject({ paused: true, breached: true, lifetime: { spent_microusd: "40", held_microusd: "0" } });
  });

  it("never retries or refunds HTTP failure, truncated/ambiguous SSE, tier mismatch, or upstream timeout", async () => {
    for (const scenario of ["http", "truncated", "missing_usage", "wrong_tier", "wrong_mode", "timeout"]) {
      let cancelled = false;
      const upstream = vi.fn<typeof fetch>(async () => {
        if (scenario === "http") return new Response("sensitive upstream diagnostic must not leak", { status: 429 });
        const values = streamEvents(scenario === "wrong_tier" ? { tier: "priority" } : {});
        if (scenario === "wrong_mode") (values[0].response!.reasoning as Record<string, unknown>).mode = "pro";
        if (scenario === "truncated") return sse(values.slice(0, -1));
        if (scenario === "missing_usage") {
          const last = values.at(-1) as { response: { usage: ReturnType<typeof usage> } };
          delete (last.response.usage.input_tokens_details as Partial<ReturnType<typeof usage>["input_tokens_details"]>).cache_write_tokens;
        }
        if (scenario === "timeout") return new Response(new ReadableStream<Uint8Array>({
          start(controller) { controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(values[0])}\n\n`)); },
          cancel() { cancelled = true; },
        }), { headers: { "Content-Type": "text/event-stream" } });
        return sse(values);
      });
      const { stub, user } = await realFixture(upstream, { request_timeout_ms: scenario === "timeout" ? 40 : 30_000 });
      const all = await events(await respond(stub, user.token, crypto.randomUUID(), INPUT));
      expect(all.at(-1).status).toBe("uncertain");
      expect(JSON.stringify(all)).not.toContain("sensitive upstream");
      expect((await status(stub)).lifetime.held_microusd).toBe(RESERVE);
      expect(upstream).toHaveBeenCalledTimes(1);
      if (scenario === "timeout") expect(cancelled).toBe(true);
      if (["missing_usage", "wrong_tier", "wrong_mode", "truncated"].includes(scenario)) expect((await status(stub)).breached).toBe(true);
    }
    expect(fetch).not.toHaveBeenCalled();
  });
});
