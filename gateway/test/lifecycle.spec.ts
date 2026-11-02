import { abortAllDurableObjects, evictDurableObject, runDurableObjectAlarm } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { digest, bodyFingerprint } from "../src/crypto";
import { PreflightError } from "../src/providers";
import { ADMIN, allowance, api, bindings, controlled, doc, enroll, events, fixture, frozenClock, inside, INPUT, RESERVE, respond, status } from "./harness";

describe("request lifecycle, retries and irreversible liability", () => {
  it("persists an idempotency record before dispatch so parallel retries run exactly once", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const key = crypto.randomUUID();
    const responses = await Promise.all(Array.from({ length: 12 }, () => respond(stub, user.token, key)));
    const original = responses.filter(r => r.headers.get("Content-Type") === "application/x-ndjson");
    expect(original).toHaveLength(1);
    expect(responses.filter(r => r.status === 202)).toHaveLength(11);
    expect(provider.calls()).toBe(1);
    const firstReplay = await doc(responses.find(r => r.status === 202)!);
    expect(firstReplay).toMatchObject({ replay: true, output_replay_available: false });
    const collision = await respond(stub, user.token, key, { ...INPUT, max_output_tokens: 99 });
    expect(collision.status).toBe(409);
    provider.finish();
    const done = (await events(original[0])).at(-1);
    expect(done.request_id).toBe(firstReplay.request_id);
    const replay = await doc(await respond(stub, user.token, key));
    expect(replay).toMatchObject({ replay: true, status: "completed", request_id: done.request_id });
    expect(provider.calls()).toBe(1);
    expect((await status(stub)).lifetime.spent_microusd).toBe("20");
  });

  it("binds keys to users, retains them through reset and rejects collisions", async () => {
    const stub = await fixture();
    const a = await enroll(stub), b = await enroll(stub);
    const key = crypto.randomUUID();
    const first = (await events(await respond(stub, a.token, key))).at(-1);
    const second = (await events(await respond(stub, b.token, key))).at(-1);
    expect(first.request_id).not.toBe(second.request_id);
    await api(stub, `/admin/users/${a.user_id}/reset`, { daily: true, monthly: true });
    const replay = await doc(await respond(stub, a.token, key));
    expect(replay.request_id).toBe(first.request_id);
    expect((await allowance(stub, a.token)).daily.spent_microusd).toBe("0");
    expect((await respond(stub, a.token, key, { ...INPUT, messages: [{ role: "user", content: "Different synthetic prompt." }] })).status).toBe(409);
  });

  it("refunds only a proven local preflight failure and keeps its tombstone", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    await inside(stub, i => { i.adapter = () => ({ prepare() { throw new PreflightError("provider_unavailable"); } }); });
    const key = crypto.randomUUID();
    const failed = await respond(stub, user.token, key);
    expect(failed.status).toBe(502);
    const result = await doc(failed);
    expect(result.error).toBe("provider_unavailable");
    expect((await allowance(stub, user.token)).daily.held_microusd).toBe("0");
    const retry = await doc(await respond(stub, user.token, key));
    expect(retry).toMatchObject({ replay: true, status: "failed_before_dispatch", charged_microusd: "0" });
    expect((await status(stub)).lifetime.spent_microusd).toBe("0");
  });

  it("retains the complete hold after provider failure, including after a manual reset", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const provider = await controlled(stub, { fail: true });
    const key = crypto.randomUUID();
    const first = await respond(stub, user.token, key);
    provider.finish();
    const all = await events(first);
    expect(JSON.stringify(all)).not.toContain("DO_NOT_LEAK");
    expect(all.at(-1)).toMatchObject({ type: "error", status: "uncertain", error: "provider_failed" });
    await api(stub, `/admin/users/${user.user_id}/reset`, { daily: true, monthly: true });
    expect((await allowance(stub, user.token)).daily.held_microusd).toBe(String(RESERVE));
    expect((await status(stub)).lifetime.held_microusd).toBe(String(RESERVE));
    expect((await doc(await respond(stub, user.token, key))).status).toBe("uncertain");
    expect(provider.calls()).toBe(1);
  });

  it("treats a synchronous exception from stream() as ambiguous dispatch, never as safe preflight", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    await inside(stub, i => { i.adapter = () => ({ prepare() { return { stream() { throw new Error("synthetic upstream refusal"); } }; } }); });
    const all = await events(await respond(stub, user.token));
    expect(all.at(-1)).toMatchObject({ status: "uncertain", error: "provider_failed" });
    expect((await status(stub)).lifetime.held_microusd).toBe(String(RESERVE));
  });

  it("times out an uncooperative provider, releases concurrency but never money", async () => {
    const stub = await fixture({ request_timeout_ms: 40, user_concurrency: 1 });
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const first = await respond(stub, user.token);
    const all = await events(first);
    expect(all.at(-1)).toMatchObject({ status: "uncertain", error: "request_timeout" });
    expect((await allowance(stub, user.token)).daily.held_microusd).toBe(String(RESERVE));
    provider.finish();
    const second = await respond(stub, user.token);
    expect(second.status).toBe(200);
    await events(second);
    expect((await status(stub)).lifetime.held_microusd).toBe(String(RESERVE));
  });

  it("aborts on consumer disconnect and retains liability without waiting for upstream", async () => {
    const stub = await fixture({ request_timeout_ms: 1000 });
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const response = await respond(stub, user.token);
    await provider.started;
    const requestId = response.headers.get("X-Request-ID");
    await response.body!.cancel();
    // A DO event following the cancellation observes the pump's committed state.
    let found = await doc(await api(stub, `/v1/requests/${requestId}`, undefined, user.token));
    if (found.status === "dispatched") {
      await new Promise(resolve => setTimeout(resolve, 5));
      found = await doc(await api(stub, `/v1/requests/${requestId}`, undefined, user.token));
    }
    expect(found).toMatchObject({ status: "uncertain", error: "client_disconnected" });
    expect((await status(stub)).lifetime.held_microusd).toBe(String(RESERVE));
    provider.finish();
  });

  it("fails closed on missing terminal usage without refunding provisional estimates", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const provider = await controlled(stub, { missingUsage: true });
    const first = await respond(stub, user.token);
    provider.finish();
    expect((await events(first)).at(-1)).toMatchObject({ status: "uncertain", error: "usage_missing_or_invalid" });
    const global = await status(stub);
    expect(global).toMatchObject({ paused: true, breached: true });
    expect(global.lifetime.held_microusd).toBe(String(RESERVE));
  });

  it.each(["reserved", "dispatched"] as const)("recovers a stale %s record after eviction and processes duplicate alarms idempotently", async phase => {
    const stub = await fixture();
    const user = await enroll(stub);
    const now = Date.now();
    const id = crypto.randomUUID();
    const key = crypto.randomUUID();
    const [tokenHash, keyHash, bodyHash] = await Promise.all([digest(user.token), digest(key), bodyFingerprint(JSON.stringify({ ...INPUT, tools: [] }), ADMIN)]);
    const deadline = await inside(stub, async (i, state) => {
      const session = i.ledger.authenticate(tokenHash, now);
      const result = i.ledger.reserve(session, id, keyHash, bodyHash, "mock-text", i.policy.models["mock-text"], 100, i.policy, now);
      if (phase === "dispatched") i.ledger.markDispatched(id, session, now);
      await state.storage.setAlarm(result.row.deadline_at);
      await state.storage.sync();
      return result.row.deadline_at;
    });
    await evictDurableObject(stub);
    await frozenClock(stub, deadline + 1);
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    expect(await runDurableObjectAlarm(stub)).toBe(false);
    const record = await doc(await api(stub, `/v1/requests/${id}`, undefined, user.token));
    expect(record).toMatchObject({ status: "uncertain", error: "stale_inflight" });
    expect((await status(stub)).lifetime.held_microusd).toBe(String(RESERVE));
    const replay = await doc(await respond(stub, user.token, key));
    expect(replay).toMatchObject({ request_id: id, status: "uncertain", replay: true });
  });

  it("preserves dispatch liability through an actual forced workerd DO termination", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const key = crypto.randomUUID();
    const first = await respond(stub, user.token, key);
    await provider.started;
    const id = first.headers.get("X-Request-ID")!;
    const row = await inside(stub, i => i.ledger.request(id)!);
    const ended = first.arrayBuffer().then(() => "complete", () => "interrupted");
    // This destroys the live object and pump, while retaining SQLite storage.
    await abortAllDurableObjects();
    // A failed DO stub is poisoned by workerd; reconnect using the same ID.
    const freshStub = bindings.GATEWAY.get(stub.id);
    await frozenClock(freshStub, row.deadline_at + 1);
    await runDurableObjectAlarm(freshStub);
    const recovered = await doc(await respond(freshStub, user.token, key));
    expect(recovered).toMatchObject({ request_id: id, status: "uncertain", replay: true });
    expect((await status(freshStub)).lifetime.held_microusd).toBe(String(RESERVE));
    expect(provider.calls()).toBe(1);
    provider.finish();
    expect(await ended).toBe("interrupted");
  });

  it("does not store prompt text, tool-result text, output deltas or raw idempotency keys", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const key = "unique_idempotency_secret_0000000000000001";
    const prompt = "private_prompt_never_store_93f8c0";
    await events(await respond(stub, user.token, key, { ...INPUT, messages: [{ role: "user", content: prompt }] }));
    const saved = await inside(stub, (_, state) => {
      const names = ["control", "users", "sessions", "invites", "buckets", "requests", "rate_limits", "audit"];
      return JSON.stringify(names.map(name => state.storage.sql.exec(`SELECT * FROM ${name}`).toArray()));
    });
    for (const sensitive of [prompt, key, user.token, "Offline mock response."]) expect(saved).not.toContain(sensitive);
    expect(saved).toContain("body_hash");
  });
});
