import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import newApiExample from "../policy.new-api.example.json";
import openAIExample from "../policy.production.example.json";
import { assertConfigured, newApiEndpoint, parsePolicy, type Env, type Policy } from "../src/config";
import { NewApiAdapter } from "../src/new-api";
import { generation, type GenerationRequest } from "../src/validation";
import { allowance, api, bindings, doc, enroll, events, fixture, gate, inside, INPUT as MOCK_INPUT, RESERVE as MOCK_RESERVE, respond as gatewayRespond, status } from "./harness";

const KEY = "test_only_never_valid_new_api_credential_00000000000";
const BASE_URL = "https://new-api.test";
const MODEL = Object.keys(newApiExample.models)[0];
const INPUT: GenerationRequest = { model: MODEL, messages: [{ role: "user", content: "Synthetic text only." }], max_output_tokens: 128, tools: [] };
// Peak cache-miss rate for the full reviewed context + requested output cap.
const RESERVE = "314727";
const respond = (stub: DurableObjectStub, token: string, key = crypto.randomUUID(), body: unknown = INPUT) =>
  api(stub, "/v1/responses", body, token, { "Idempotency-Key": key, "X-Gateway-Mode": "production", "X-Gateway-Consent-Version": "2" });

beforeEach(() => {
  // No test can silently fall back to a real provider request.
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("Network forbidden in offline New API tests"))));
});
afterEach(() => { vi.unstubAllGlobals(); });

function chunk(delta: Record<string, unknown>, finish: string | null = null) {
  return { id: "chatcmpl_synthetic", object: "chat.completion.chunk", model: "deepseek-flash",
    choices: [{ index: 0, delta, finish_reason: finish }] };
}

function streamValues(tool?: string): unknown[] {
  return [
    chunk({ role: "assistant", content: "" }),
    ...(tool ? [
      chunk({ tool_calls: [{ index: 0, id: "call_synthetic", type: "function", function: { name: tool, arguments: "{" } }] }),
      chunk({ tool_calls: [{ index: 0, function: { arguments: "}" } }] }),
    ] : [chunk({ content: "Synthetic 學 response." })]),
    chunk({}, tool ? "tool_calls" : "stop"),
    // This relay-reported count is deliberately tiny. It must never refund the
    // conservative reservation, even if New API estimated it itself.
    { id: "chatcmpl_synthetic", object: "chat.completion.chunk", model: "deepseek-flash", choices: [],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
    "[DONE]",
  ];
}

function sse(values: unknown[], suffix = ""): Response {
  const bytes = new TextEncoder().encode(values.map(value => `data: ${value === "[DONE]" ? value : JSON.stringify(value)}\r\n\r\n`).join("") + suffix);
  // Exercise Unicode and CRLF split across arbitrary transport boundaries.
  return new Response(new ReadableStream<Uint8Array>({ start(controller) {
    for (let offset = 0; offset < bytes.length; offset += 7) controller.enqueue(bytes.slice(offset, offset + 7));
    controller.close();
  } }), { headers: { "Content-Type": "text/event-stream" } });
}

async function relayFixture(upstream: typeof fetch, patch: Partial<Policy> = {}) {
  const policy = parsePolicy(newApiExample);
  const stub = await fixture({ ...policy, user_daily_microusd: 2_000_000, user_monthly_microusd: 4_000_000,
    global_monthly_microusd: 10_000_000, global_lifetime_microusd: 20_000_000, ...patch });
  await inside(stub, i => { i.adapter = () => new NewApiAdapter(KEY, BASE_URL, upstream); });
  return { stub, user: await enroll(stub) };
}

async function perUserFixture() {
  const aliceKey = "test_only_never_valid_alice_relay_credential_00000000000";
  const policy = parsePolicy(newApiExample);
  const perUserEnv: Env = { ...bindings, NEW_API_BASE_URL: BASE_URL, NEW_API_RELAY_KEY: undefined,
    NEW_API_IDENTITY_MODE: "per_user", NEW_API_RELAY_KEYS: JSON.stringify({ alice: aliceKey }) };
  const stub = await fixture({ ...policy, user_daily_microusd: 2_000_000, user_monthly_microusd: 4_000_000,
    global_monthly_microusd: 10_000_000, global_lifetime_microusd: 20_000_000 });
  await inside(stub, i => { (i as unknown as { env: Env }).env = perUserEnv; });
  const observed: string[] = [];
  const upstream = vi.mocked(fetch).mockImplementation(async (_url, init) => {
    observed.push(new Headers(init?.headers).get("Authorization") ?? "");
    return sse(streamValues());
  });
  return { stub, policy, perUserEnv, aliceKey, observed, upstream };
}

describe("New API behind the existing allowance ledger, with network blocked", () => {
  it("fails closed for missing server configuration, unreviewed bounds/prices, and client overrides", () => {
    const policy = parsePolicy(newApiExample);
    const configured = { ...bindings, NEW_API_RELAY_KEY: KEY, NEW_API_BASE_URL: BASE_URL, OPENAI_API_KEY: undefined };
    expect(() => assertConfigured(policy, configured)).not.toThrow();
    expect(() => assertConfigured(policy, { ...configured, NEW_API_RELAY_KEY: undefined })).toThrow("configuration_error");
    expect(() => assertConfigured(policy, { ...configured, NEW_API_BASE_URL: undefined })).toThrow("configuration_error");
    for (const url of ["http://new-api.test", "https://name:secret@new-api.test", "https://new-api.test/v1", "https://new-api.test?key=secret", "https://new-api.test#fragment"]) {
      expect(() => newApiEndpoint(url)).toThrow();
    }
    expect(newApiEndpoint(BASE_URL)).toBe(`${BASE_URL}/v1/chat/completions`);
    expect(() => parsePolicy({ ...newApiExample, mode: "mock" })).toThrow("configuration_error");
    for (const patch of [{ max_input_tokens: 1048575 }, { input_microusd_per_million_tokens: 299999 },
      { output_microusd_per_million_tokens: 1199999 }, { native_model: "unreviewed-model" }]) {
      expect(() => parsePolicy({ ...newApiExample, models: { [MODEL]: { ...newApiExample.models[MODEL as keyof typeof newApiExample.models], ...patch } } })).toThrow("configuration_error");
    }
    for (const extra of [{ base_url: BASE_URL }, { api_key: KEY }, { accounting: "full_reservation" }, { user: "another-user" }]) {
      expect(() => generation({ ...INPUT, ...extra }, policy)).toThrow("invalid_fields");
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it("dispatches once, charges the full reservation without inventing usage, and replays without another charge", async () => {
    let sent: Record<string, any> | undefined;
    const upstream = vi.fn<typeof fetch>(async (url, init) => {
      expect(url).toBe(`${BASE_URL}/v1/chat/completions`);
      expect(init).toMatchObject({ method: "POST", redirect: "error" });
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      const headers = new Headers(init?.headers);
      expect(headers.get("Authorization")).toBe(`Bearer ${KEY}`);
      expect(headers.has("New-Api-User")).toBe(false);
      sent = JSON.parse(init!.body as string);
      return sse(streamValues());
    });
    const { stub, user } = await relayFixture(upstream);
    const key = crypto.randomUUID();
    const all = await events(await respond(stub, user.token, key));
    expect(sent).toMatchObject({ model: "deepseek-flash", max_tokens: 128, stream: true,
      stream_options: { include_usage: true }, thinking: { type: "disabled" }, reasoning_effort: "none" });
    expect(sent).not.toHaveProperty("n");
    expect(sent).not.toHaveProperty("parallel_tool_calls");
    expect(sent).not.toHaveProperty("user");
    expect(sent).not.toHaveProperty("base_url");
    expect(all.filter(event => event.type === "text.delta").map(event => event.delta).join("")).toBe("Synthetic 學 response.");
    expect(all[0].reserved_microusd).toBe(RESERVE);
    expect(all.at(-1)).toMatchObject({ type: "done", status: "completed", accounting: "full_reservation",
      charged_microusd: RESERVE, usage: null, finish_reason: "stop" });
    expect((await allowance(stub, user.token)).daily).toMatchObject({ spent_microusd: RESERVE, held_microusd: "0" });
    expect((await status(stub)).lifetime).toMatchObject({ spent_microusd: RESERVE, held_microusd: "0" });
    const replay = await doc(await respond(stub, user.token, key));
    expect(replay).toMatchObject({ replay: true, request_id: all[0].request_id, accounting: "full_reservation", charged_microusd: RESERVE, usage: null });
    expect(JSON.stringify(all)).not.toContain(KEY);
    expect(upstream).toHaveBeenCalledTimes(1);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("maps only approved local tools and keeps function history when new tools are disabled", async () => {
    const bodies: any[] = [];
    const upstream = vi.fn<typeof fetch>(async (_url, init) => {
      bodies.push(JSON.parse(init!.body as string));
      const values = streamValues(bodies.length === 1 ? "demo_read_only" : undefined);
      // Full-reservation completion needs a valid terminal stream, not a
      // possibly synthesized relay usage event.
      if (bodies.length === 2) values.splice(-2, 1);
      return sse(values);
    });
    const { stub, user } = await relayFixture(upstream, { tools: { demo_read_only: {
      description: "Synthetic local read-only function.", parameters: { type: "object", properties: {}, additionalProperties: false },
    } } });
    const first = await events(await respond(stub, user.token, crypto.randomUUID(), { ...INPUT, tools: ["demo_read_only"] }));
    expect(first.filter(event => event.type === "tool_call.delta")).toEqual([
      { type: "tool_call.delta", index: 0, id: "call_synthetic", name: "demo_read_only", arguments_delta: "{" },
      { type: "tool_call.delta", index: 0, arguments_delta: "}" },
    ]);
    expect(first.at(-1)).toMatchObject({ type: "done", finish_reason: "tool_calls", usage: null });
    expect(bodies[0].tools).toMatchObject([{ type: "function", function: { name: "demo_read_only", parameters: { additionalProperties: false } } }]);
    const messages = [...INPUT.messages,
      { role: "assistant", content: "", tool_calls: [{ id: "call_synthetic", name: "demo_read_only", arguments: "{}" }] },
      { role: "tool", tool_call_id: "call_synthetic", content: "Synthetic function output." }];
    const second = await events(await respond(stub, user.token, crypto.randomUUID(), { ...INPUT, messages, tools: [] }));
    expect(second.at(-1)).toMatchObject({ type: "done", finish_reason: "stop" });
    expect(bodies[1].tools ?? []).toEqual([]);
    expect(bodies[1].messages).toEqual([
      ...INPUT.messages,
      { role: "assistant", content: "", tool_calls: [{ id: "call_synthetic", type: "function", function: { name: "demo_read_only", arguments: "{}" } }] },
      { role: "tool", tool_call_id: "call_synthetic", content: "Synthetic function output." },
    ]);
    expect(upstream).toHaveBeenCalledTimes(2);
  });

  it("retains liability without retries for failed streams or reported model/output breaches", async () => {
    for (const scenario of ["http", "missing_done", "wrong_model", "suffix", "over_bound", "reasoning"]) {
      const upstream = vi.fn<typeof fetch>(async () => {
        if (scenario === "http") return new Response("DO_NOT_LEAK_UPSTREAM_DIAGNOSTICS", { status: 429 });
        const values = streamValues();
        if (scenario === "missing_done") values.pop();
        if (scenario === "wrong_model") (values[1] as Record<string, unknown>).model = "unapproved-native-model";
        if (scenario === "over_bound") (values.at(-2) as Record<string, unknown>).usage = { prompt_tokens: 1, completion_tokens: 129, total_tokens: 130 };
        if (scenario === "reasoning") (values.at(-2) as Record<string, unknown>).usage = { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2, completion_tokens_details: { reasoning_tokens: 1 } };
        return sse(values, scenario === "suffix" ? "data: malformed\n\n" : "");
      });
      const { stub, user } = await relayFixture(upstream);
      const all = await events(await respond(stub, user.token));
      expect(all.at(-1)).toMatchObject({ type: "error", status: "uncertain" });
      expect(JSON.stringify(all)).not.toContain("DO_NOT_LEAK_UPSTREAM_DIAGNOSTICS");
      expect((await status(stub)).lifetime).toMatchObject({ held_microusd: RESERVE, spent_microusd: "0" });
      if (scenario !== "http") expect((await status(stub)).breached).toBe(true);
      expect(upstream).toHaveBeenCalledTimes(1);
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it("snapshots the accounting mode at admission and rejects an adapter trying to switch a metered request", async () => {
    const finish = gate(), started = gate();
    const { stub, user } = await relayFixture(vi.fn<typeof fetch>(async () => {
      started.release();
      await finish.promise;
      return sse(streamValues());
    }));
    const response = await respond(stub, user.token);
    await started.promise;
    // A deployment can change this alias while the request is still running.
    // Its stored liability/accounting contract must remain the admitted one.
    await inside(stub, i => { i.policy.models[MODEL] = parsePolicy(openAIExample).models.luna; });
    finish.release();
    expect((await events(response)).at(-1)).toMatchObject({ status: "completed", charged_microusd: RESERVE, accounting: "full_reservation", usage: null });

    const metered = await fixture();
    const meteredUser = await enroll(metered);
    await inside(metered, i => {
      i.adapter = () => ({ prepare() { return { async *stream() {
        return { accounting: "full_reservation" as const, usage: null, finish_reason: "stop" as const };
      } }; } });
    });
    const switched = await events(await gatewayRespond(metered, meteredUser.token, crypto.randomUUID(), MOCK_INPUT));
    expect(switched.at(-1)).toMatchObject({ type: "error", status: "uncertain" });
    expect((await status(metered)).lifetime).toMatchObject({ held_microusd: String(MOCK_RESERVE), spent_microusd: "0" });
  });

  it("keeps every user/owner cap and gateway revocation in front of the relay", async () => {
    for (const [field, error] of [
      ["user_daily_microusd", "user_daily_limit"], ["user_monthly_microusd", "user_monthly_limit"],
      ["global_monthly_microusd", "global_monthly_limit"], ["global_lifetime_microusd", "global_lifetime_limit"],
    ] as const) {
      const upstream = vi.fn<typeof fetch>(async () => sse(streamValues()));
      const { stub, user } = await relayFixture(upstream, { [field]: Number(RESERVE) - 1 });
      const denied = await respond(stub, user.token);
      expect(denied.status).toBe(429);
      expect(await doc(denied)).toEqual({ error });
      expect(upstream).not.toHaveBeenCalled();
    }
    const upstream = vi.fn<typeof fetch>(async () => sse(streamValues()));
    const { stub, user } = await relayFixture(upstream);
    expect((await api(stub, `/admin/users/${user.user_id}/revoke`, {})).status).toBe(200);
    expect((await respond(stub, user.token)).status).toBe(401);
    expect((await respond(stub, KEY)).status).toBe(401);
    expect(upstream).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("requires renewed destination disclosure before a reservation or dispatch", async () => {
    const upstream = vi.fn<typeof fetch>(async () => sse(streamValues()));
    const { stub, user } = await relayFixture(upstream);
    for (const consent of [undefined, "1"]) {
      const denied = await api(stub, "/v1/responses", INPUT, user.token, { "Idempotency-Key": crypto.randomUUID(),
        "X-Gateway-Mode": "production", ...(consent ? { "X-Gateway-Consent-Version": consent } : {}) });
      expect(denied.status).toBe(409);
      expect(await doc(denied)).toEqual({ error: "disclosure_required" });
    }
    expect((await status(stub)).lifetime).toMatchObject({ spent_microusd: "0", held_microusd: "0" });
    expect((await status(stub)).requests).toEqual([]);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("resolves per-user relay keys only from authenticated server bindings, without shared fallback", async () => {
    const aliceKey = "test_only_never_valid_alice_relay_credential_00000000000";
    const bobKey = "test_only_never_valid_bob_relay_credential_0000000000000";
    const policy = parsePolicy(newApiExample);
    const perUserEnv: Env = { ...bindings, NEW_API_BASE_URL: BASE_URL, NEW_API_RELAY_KEY: undefined,
      NEW_API_IDENTITY_MODE: "per_user", NEW_API_RELAY_KEYS: JSON.stringify({ alice: aliceKey, bob: bobKey }) };
    expect(() => assertConfigured(policy, perUserEnv)).not.toThrow();
    expect(() => assertConfigured(policy, { ...perUserEnv, NEW_API_RELAY_KEY: KEY })).toThrow("configuration_error");
    const stub = await fixture({ ...policy, user_daily_microusd: 2_000_000, user_monthly_microusd: 4_000_000,
      global_monthly_microusd: 10_000_000, global_lifetime_microusd: 20_000_000 });
    // Keep the production adapter factory, replacing only this DO's synthetic
    // server bindings; no HTTP route allows the caller to replace env values.
    await inside(stub, i => { (i as unknown as { env: Env }).env = perUserEnv; });
    const observed: string[] = [];
    const upstream = vi.mocked(fetch).mockImplementation(async (_url, init) => {
      observed.push(new Headers(init?.headers).get("Authorization") ?? "");
      return sse(streamValues());
    });
    const alice = await enroll(stub), bob = await enroll(stub);
    const bindingPath = `/admin/users/${alice.user_id}/new-api`;
    expect((await api(stub, bindingPath, { relay_key_ref: "alice" }, alice.token)).status).toBe(401);
    expect((await api(stub, bindingPath, { relay_key_ref: "missing" })).status).toBe(400);
    const unbound = await respond(stub, alice.token);
    expect(unbound.status).toBe(502);
    expect((await doc(unbound)).error).toBe("provider_unavailable");
    expect((await status(stub)).lifetime).toMatchObject({ spent_microusd: "0", held_microusd: "0" });
    expect(upstream).not.toHaveBeenCalled();

    expect((await api(stub, bindingPath, { relay_key_ref: "alice" })).status).toBe(200);
    expect((await api(stub, `/admin/users/${bob.user_id}/new-api`, { relay_key_ref: "bob" })).status).toBe(200);
    const aliceEvents = await events(await respond(stub, alice.token));
    await events(await respond(stub, bob.token));
    expect(observed).toEqual([`Bearer ${aliceKey}`, `Bearer ${bobKey}`]);
    const publicData = JSON.stringify([aliceEvents, await allowance(stub, alice.token)]);
    expect(publicData).not.toContain(aliceKey);
    expect(publicData).not.toContain(bobKey);
    expect(publicData).not.toContain("relay_key_ref");

    await inside(stub, i => { (i as unknown as { env: Env }).env.NEW_API_RELAY_KEYS = JSON.stringify({ bob: bobKey }); });
    const removed = await respond(stub, alice.token);
    expect(removed.status).toBe(502);
    expect((await doc(removed)).error).toBe("provider_unavailable");
    expect((await status(stub)).lifetime.held_microusd).toBe("0");
    expect(upstream).toHaveBeenCalledTimes(2);
  });

  it("prebinds a new invitation atomically and preserves its binding through account recovery", async () => {
    const { stub, aliceKey, observed, upstream } = await perUserFixture();
    expect((await api(stub, "/admin/invites", { expires_in_seconds: 3600, relay_key_ref: "missing" })).status).toBe(400);
    const invited = await api(stub, "/admin/invites", { expires_in_seconds: 3600, relay_key_ref: "alice" });
    expect(invited.status).toBe(201);
    const invitation = await doc(invited);
    const enrollment = await api(stub, "/v1/enroll", { invite: invitation.invite }, "");
    expect(enrollment.status).toBe(201);
    const user = await doc(enrollment);
    const binding = await doc(await api(stub, `/admin/users/${user.user_id}/new-api`));
    expect(binding).toEqual({ user_id: user.user_id, identity_mode: "per_user", relay_key_ref: "alice", binding_status: "bound" });
    expect((await events(await respond(stub, user.token))).at(-1).status).toBe("completed");
    expect(observed).toEqual([`Bearer ${aliceKey}`]);

    // Recovery only replaces the session. It cannot silently change whose
    // New API key is charged or mint a fresh Canvas allowance account.
    expect((await api(stub, "/admin/invites", { expires_in_seconds: 3600,
      user_id: user.user_id, relay_key_ref: "alice" })).status).toBe(400);
    const recovered = await enroll(stub, user.user_id);
    expect(recovered.user_id).toBe(user.user_id);
    expect((await respond(stub, user.token)).status).toBe(401);
    expect((await allowance(stub, recovered.token)).daily.spent_microusd).toBe(RESERVE);
    expect((await doc(await api(stub, `/admin/users/${recovered.user_id}/new-api`))).relay_key_ref).toBe("alice");
    expect((await events(await respond(stub, recovered.token))).at(-1).status).toBe("completed");
    expect(observed).toEqual([`Bearer ${aliceKey}`, `Bearer ${aliceKey}`]);
    expect(upstream).toHaveBeenCalledTimes(2);
  });

  it("reports sanitized allowance and binding states while missing credentials or unbinding stop dispatch", async () => {
    const { stub, policy, perUserEnv, aliceKey, upstream } = await perUserFixture();
    const user = await enroll(stub);
    const path = `/admin/users/${user.user_id}/new-api`;
    expect((await api(stub, path, undefined, user.token)).status).toBe(401);
    expect(await doc(await api(stub, path))).toEqual({ user_id: user.user_id, identity_mode: "per_user", relay_key_ref: null, binding_status: "unbound" });
    const unbound = await allowance(stub, user.token);
    expect(unbound).toMatchObject({ user_id: user.user_id, allowance_source: "canvas_gateway", models: [{ id: MODEL,
      accounting: "full_reservation", reservation: { min_microusd: "314574", max_microusd: "315802" },
      new_api: { identity_mode: "per_user", binding_status: "unbound" } }] });
    expect((await api(stub, path, { relay_key_ref: "alice" })).status).toBe(200);
    expect((await doc(await api(stub, path))).binding_status).toBe("bound");
    const bound = await allowance(stub, user.token);
    expect(bound.models[0].new_api).toEqual({ identity_mode: "per_user", binding_status: "bound" });
    for (const response of [unbound, bound]) {
      const publicData = JSON.stringify(response);
      expect(publicData).not.toContain("relay_key_ref");
      expect(publicData).not.toContain("alice");
      expect(publicData).not.toContain(aliceKey);
    }

    // Removing the last relay key is a valid disabled-user state. Metadata
    // remains readable, but the request cannot fall back to another account.
    expect(() => assertConfigured(policy, { ...perUserEnv, NEW_API_RELAY_KEYS: "{}" })).not.toThrow();
    expect(() => assertConfigured(policy, { ...perUserEnv, NEW_API_RELAY_KEYS: undefined })).toThrow("configuration_error");
    expect(() => assertConfigured(policy, { ...perUserEnv, NEW_API_RELAY_KEYS: "malformed" })).toThrow("configuration_error");
    await inside(stub, i => { (i as unknown as { env: Env }).env.NEW_API_RELAY_KEYS = "{}"; });
    const missing = await doc(await api(stub, path));
    expect(missing).toEqual({ user_id: user.user_id, identity_mode: "per_user", relay_key_ref: "alice", binding_status: "credential_missing" });
    expect((await allowance(stub, user.token)).models[0].new_api.binding_status).toBe("credential_missing");
    const denied = await respond(stub, user.token);
    expect(denied.status).toBe(502);
    expect((await doc(denied)).error).toBe("provider_unavailable");

    expect((await api(stub, path, { relay_key_ref: null })).status).toBe(200);
    expect(await doc(await api(stub, path))).toEqual({ user_id: user.user_id, identity_mode: "per_user", relay_key_ref: null, binding_status: "unbound" });
    expect((await allowance(stub, user.token)).models[0].new_api.binding_status).toBe("unbound");
    expect((await respond(stub, user.token)).status).toBe(502);
    expect((await status(stub)).lifetime).toMatchObject({ spent_microusd: "0", held_microusd: "0" });
    expect(upstream).not.toHaveBeenCalled();
  });
});
