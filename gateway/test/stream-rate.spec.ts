import { describe, expect, it } from "vitest";
import { ADMIN, allowance, api, doc, enroll, events, fixture, frozenClock, inside, INPUT, RESERVE, respond, status } from "./harness";

describe("stream and admission boundaries", () => {
  it("streams function deltas through a complete local tool round trip with unique call IDs", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const body = { ...INPUT, tools: ["demo_read_only"] };
    const first = await events(await respond(stub, user.token, crypto.randomUUID(), body));
    const deltas = first.filter(e => e.type === "tool_call.delta");
    expect(deltas).toHaveLength(2);
    expect(first.at(-1)).toMatchObject({ type: "done", finish_reason: "tool_calls", status: "completed" });
    const call = { id: deltas[0].id, name: deltas[0].name, arguments: deltas.map(e => e.arguments_delta ?? "").join("") };
    expect(call.arguments).toBe("{}");
    const followup = { ...body, messages: [...body.messages,
      { role: "assistant", content: "", tool_calls: [call] },
      { role: "tool", tool_call_id: call.id, content: "Synthetic local result; no server execution." }] };
    const second = await events(await respond(stub, user.token, crypto.randomUUID(), followup));
    expect(second.some(e => e.type === "text.delta")).toBe(true);
    expect(second.some(e => e.type === "tool_call.delta")).toBe(false);
    expect(second.at(-1).finish_reason).toBe("stop");
    const third = await events(await respond(stub, user.token, crypto.randomUUID(), { ...followup, messages: [...followup.messages, { role: "user", content: "One more synthetic request." }] }));
    expect(third.find(e => e.type === "tool_call.delta").id).not.toBe(call.id);
  });

  it("rejects undeclared functions, malformed complete arguments and inconsistent terminal tool states", async () => {
    for (const scenario of ["undeclared", "malformed", "missing_call", "stop_with_call"]) {
      const stub = await fixture();
      const user = await enroll(stub);
      await inside(stub, i => {
        i.adapter = () => ({ prepare() { return { async *stream() {
          if (scenario !== "missing_call") yield { type: "tool_call.delta" as const, index: 0, id: "call_test", name: scenario === "undeclared" ? "execute_shell" : "demo_read_only", arguments_delta: scenario === "malformed" ? "{" : "{}" };
          return { usage: { input_tokens: 10, output_tokens: 5 }, finish_reason: scenario === "stop_with_call" ? "stop" as const : "tool_calls" as const };
        } }; } });
      });
      const all = await events(await respond(stub, user.token, crypto.randomUUID(), { ...INPUT, tools: ["demo_read_only"] }));
      expect(all.at(-1)).toMatchObject({ type: "error", error: "provider_protocol_error", status: "uncertain" });
      expect(all.some(e => e.name === "execute_shell")).toBe(false);
      expect((await status(stub))).toMatchObject({ paused: true, breached: true });
      expect((await allowance(stub, user.token)).daily.held_microusd).toBe(String(RESERVE));
    }
  });

  it("reports length truncation explicitly with incomplete tool arguments, while charging authoritative usage", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    await inside(stub, i => {
      i.adapter = () => ({ prepare() { return { async *stream() {
        yield { type: "tool_call.delta" as const, index: 0, id: "truncated_call", name: "demo_read_only", arguments_delta: "{" };
        return { usage: { input_tokens: 10, output_tokens: 100 }, finish_reason: "length" as const };
      } }; } });
    });
    const all = await events(await respond(stub, user.token, crypto.randomUUID(), { ...INPUT, tools: ["demo_read_only"] }));
    expect(all.at(-1)).toMatchObject({ type: "done", finish_reason: "length", charged_microusd: "210" });
    // Local clients execute tools only after finish_reason=tool_calls and schema
    // validation. A length finish leaves this partial call unusable.
    expect(all.filter(e => e.type === "tool_call.delta").map(e => e.arguments_delta).join("")).toBe("{");
  });

  it("bounds buffered output and retains the reserve after a stream-size abort", async () => {
    const stub = await fixture({ max_stream_bytes: 1024 });
    const user = await enroll(stub);
    await inside(stub, i => {
      i.adapter = () => ({ prepare() { return { async *stream() {
        yield { type: "text.delta" as const, delta: "x".repeat(2048) };
        return { usage: { input_tokens: 1, output_tokens: 1 }, finish_reason: "stop" as const };
      } }; } });
    });
    const response = await respond(stub, user.token);
    const id = response.headers.get("X-Request-ID");
    expect((await response.arrayBuffer()).byteLength).toBeLessThanOrEqual(1024);
    const record = await doc(await api(stub, `/v1/requests/${id}`, undefined, user.token));
    expect(record).toMatchObject({ status: "uncertain", error: "stream_too_large" });
    expect((await status(stub)).lifetime.held_microusd).toBe(String(RESERVE));
  });

  it("commits denied auth/admin attempts and resets rate windows only at the next UTC minute", async () => {
    const stub = await fixture({ auth_attempts_per_minute: 1, admin_attempts_per_minute: 2 });
    const now = Date.UTC(2032, 0, 1, 0, 0, 20);
    await frozenClock(stub, now);
    expect((await api(stub, "/v1/allowance", undefined, "")).status).toBe(401);
    expect((await api(stub, "/v1/allowance", undefined, "")).status).toBe(429);
    expect((await api(stub, "/admin/status", undefined, "invalid_admin_secret_0000000000000000")).status).toBe(401);
    expect((await api(stub, "/admin/status", undefined, "invalid_admin_secret_0000000000000000")).status).toBe(401);
    expect((await api(stub, "/admin/status", undefined, ADMIN)).status).toBe(429);
    const count = await inside(stub, (_, state) => state.storage.sql.exec("SELECT count FROM rate_limits WHERE scope='auth:unknown'").one().count);
    expect(count).toBe(2);
    await frozenClock(stub, now + 40_000);
    expect((await api(stub, "/v1/allowance", undefined, "")).status).toBe(401);
    expect((await api(stub, "/admin/status")).status).toBe(200);
  });

  it("stops a global-saturated ingress flood before creating arbitrary peer rate rows", async () => {
    const stub = await fixture({ global_ingress_per_minute: 2 });
    for (const n of [1, 2]) await api(stub, "/v1/allowance", undefined, "", { "X-Gateway-Peer": n.toString(16).padStart(64, "0") });
    const countRows = () => inside(stub, (_, state) => state.storage.sql.exec("SELECT COUNT(*) AS n FROM rate_limits").one().n);
    const before = await countRows();
    const denied = await Promise.all(Array.from({ length: 8 }, (_, n) => api(stub, "/v1/allowance", undefined, "", { "X-Gateway-Peer": (n + 3).toString(16).padStart(64, "0") })));
    expect(denied.every(r => r.status === 429)).toBe(true);
    expect(await countRows()).toBe(before);
  });
});
