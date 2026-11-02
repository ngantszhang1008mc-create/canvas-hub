import { describe, expect, it } from "vitest";
import example from "../policy.example.json";
import { parsePolicy, price } from "../src/config";
import { bodyFingerprint } from "../src/crypto";
import { errorResponse, GatewayError } from "../src/errors";
import worker from "../src/index";
import { SINGLETON } from "../src/config";
import { generation, readJson } from "../src/validation";
import { bindings, inside } from "./harness";

const policy = () => parsePolicy(structuredClone(example));
const requestBody = () => ({
  model: "mock-text",
  messages: [{ role: "user", content: "Synthetic test text." }],
  max_output_tokens: 32,
});

describe("independent protocol boundary review", () => {
  it.each(["usage", "api_key", "base_url"])(
    "does not accept client-controlled %s alongside a valid generation",
    field => {
      expect(() => generation({ ...requestBody(), [field]: "untrusted" }, policy()))
        .toThrowError("invalid_fields");
    },
  );

  it.each(["__proto__", "unapproved-model"])(
    "rejects model aliases outside own catalog entries: %s",
    model => {
      expect(() => generation({ ...requestBody(), model }, policy())).toThrowError("model_not_allowed");
    },
  );

  it("only accepts approved tool names and rejects uploaded/builtin definitions", () => {
    for (const tools of [["constructor"], ["web_search"], [{ type: "function", name: "demo_read_only" }], ["demo_read_only", "demo_read_only"]]) {
      expect(() => generation({ ...requestBody(), tools }, policy())).toThrowError("tool_not_allowed");
    }
    expect(generation({ ...requestBody(), tools: ["demo_read_only"] }, policy()).input.tools)
      .toEqual(["demo_read_only"]);
  });

  it("does not allow metadata or credentials to be serialized as message fields", () => {
    expect(() => generation({ ...requestBody(), messages: [{ role: "user", content: "text", canvas_cookie: "synthetic" }] }, policy()))
      .toThrowError("invalid_fields");
    expect(() => generation({ ...requestBody(), messages: [{ role: "user", content: [{ type: "image_url", image_url: "https://example.invalid" }] }] }, policy()))
      .toThrowError("invalid_messages");
  });

  it("requires complete matched function history with unique call IDs", () => {
    const assistant = { role: "assistant", content: "", tool_calls: [{ id: "call_123", name: "demo_read_only", arguments: "{}" }] };
    const result = { role: "tool", tool_call_id: "call_123", content: "Synthetic result." };
    const body = { ...requestBody(), tools: ["demo_read_only"], messages: [assistant, result] };
    expect(generation(body, policy()).input.messages).toHaveLength(2);
    expect(() => generation({ ...body, messages: [assistant] }, policy())).toThrowError("missing_tool_result");
    expect(() => generation({ ...body, messages: [result] }, policy())).toThrowError("invalid_tool_result");
    expect(() => generation({ ...body, messages: [assistant, result, result] }, policy())).toThrowError("invalid_tool_result");
    expect(() => generation({ ...body, messages: [assistant, result, assistant, result] }, policy())).toThrowError("invalid_tool_calls");
  });

  it.each([0, 1.5, 1025])("rejects invalid output bound %s", max_output_tokens => {
    expect(() => generation({ ...requestBody(), max_output_tokens }, policy())).toThrowError("invalid_output_limit");
  });

  it("bounds streamed JSON by bytes without trusting Content-Length", async () => {
    const bytes = new TextEncoder().encode(JSON.stringify({ text: "學".repeat(120) }));
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(bytes); },
      cancel() { cancelled = true; },
    });
    const request = new Request("https://gateway.invalid/test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    });
    await expect(readJson(request, 256)).rejects.toMatchObject({ status: 413, code: "body_too_large" });
    expect(cancelled).toBe(true);
  });

  it("rejects invalid UTF-8 instead of interpreting replacement characters", async () => {
    const request = new Request("https://gateway.invalid/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: new Uint8Array([123, 34, 120, 34, 58, 34, 255, 34, 125]),
    });
    await expect(readJson(request, 256)).rejects.toMatchObject({ code: "invalid_json" });
  });

  it("does not leak unknown provider exception bodies to callers", async () => {
    const response = errorResponse(new Error("provider key: synthetic-secret; body: synthetic-course-text"));
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "internal_error" });
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(await errorResponse(new GatewayError(429, "user_daily_limit")).json()).toEqual({ error: "user_daily_limit" });
  });

  it("fails closed for a nonmock provider even if its shape otherwise matches", () => {
    const invalid = structuredClone(example);
    invalid.models["mock-text"].provider = "openai";
    expect(() => parsePolicy(invalid)).toThrowError("configuration_error");
  });

  it("rejects fractional and unsafe monetary limits", () => {
    for (const limit of [-1, 0.5, 1_000_000_000_001, Number.MAX_SAFE_INTEGER + 1]) {
      expect(() => parsePolicy({ ...example, global_lifetime_microusd: limit })).toThrowError("configuration_error");
    }
  });

  it("prices integer token usage exactly and rounds partial micro-USD up", () => {
    const rates = { input_microusd_per_million_tokens: 1, output_microusd_per_million_tokens: 1 };
    expect(price(rates, 1, 0)).toBe(1n);
    expect(price(rates, 500000, 500000)).toBe(1n);
    expect(price(rates, 500000, 500001)).toBe(2n);
    expect(price({ input_microusd_per_million_tokens: 1000000000, output_microusd_per_million_tokens: 1000000000 }, 1000000000, 1000000000))
      .toBe(2000000000000n);
  });

  it("binds a request fingerprint to both body and server secret", async () => {
    const body = JSON.stringify(requestBody());
    const first = await bodyFingerprint(body, "test_only_fingerprint_secret_aaaaaaaaaaaaaaaa");
    expect(first).toMatch(/^[0-9a-f]{64}$/);
    expect(await bodyFingerprint(body, "test_only_fingerprint_secret_aaaaaaaaaaaaaaaa")).toBe(first);
    expect(await bodyFingerprint(body + " ", "test_only_fingerprint_secret_aaaaaaaaaaaaaaaa")).not.toBe(first);
    expect(await bodyFingerprint(body, "test_only_fingerprint_secret_bbbbbbbbbbbbbbbb")).not.toBe(first);
  });

  it.each([
    { Origin: "https://untrusted.invalid" },
    { Cookie: "synthetic_canvas_cookie=private" },
  ] as Record<string, string>[])("rejects browser origin/cookie requests at the public Worker", async headers => {
    const response = await worker.fetch(new Request("https://gateway.test/v1/allowance", { headers }), bindings);
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ error: "local_process_only" });
  });

  it("does not accept credentials or context in query strings", async () => {
    const response = await worker.fetch(new Request("https://gateway.test/v1/allowance?token=synthetic"), bindings);
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "query_not_allowed" });
  });

  it("replaces a caller-supplied peer key before enforcing ingress limits", async () => {
    const singleton = bindings.GATEWAY.get(bindings.GATEWAY.idFromName(SINGLETON));
    await inside(singleton, i => { i.policy.ingress_per_minute = 1; });
    const first = await worker.fetch(new Request("https://gateway.test/v1/allowance", {
      headers: { "X-Gateway-Peer": "a".repeat(64), "CF-Connecting-IP": "192.0.2.10" },
    }), bindings);
    expect(first.status).toBe(401);
    await first.arrayBuffer();
    const second = await worker.fetch(new Request("https://gateway.test/v1/allowance", {
      headers: { "X-Gateway-Peer": "b".repeat(64), "CF-Connecting-IP": "192.0.2.10" },
    }), bindings);
    expect(second.status).toBe(429);
    expect(await second.json()).toEqual({ error: "rate_limited" });
  });
});
