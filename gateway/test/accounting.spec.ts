import { describe, expect, it } from "vitest";
import { allowance, api, controlled, doc, enroll, events, fixture, frozenClock, inside, INPUT, RESERVE, respond, status } from "./harness";
import type { Policy } from "../src/config";

describe("transactional monetary bounds in real SQLite DO storage", () => {
  it.each([
    ["user_daily_microusd", "user_daily_limit"],
    ["user_monthly_microusd", "user_monthly_limit"],
    ["global_monthly_microusd", "global_monthly_limit"],
    ["global_lifetime_microusd", "global_lifetime_limit"],
  ] as const)("checks %s before any provider dispatch", async (field, code) => {
    const stub = await fixture({ [field]: Number(RESERVE - 1n) } as Partial<Policy>);
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const response = await respond(stub, user.token);
    expect(response.status).toBe(429);
    expect(await doc(response)).toEqual({ error: code });
    expect(provider.calls()).toBe(0);
    expect((await status(stub)).lifetime.held_microusd).toBe("0");
  });

  it("reserves the full accepted input bound and requested output bound, then settles authoritative usage", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const provider = await controlled(stub, { input: 11, output: 7 });
    const response = await respond(stub, user.token);
    await provider.started;
    expect((await allowance(stub, user.token)).daily.held_microusd).toBe(String(RESERVE));
    expect((await status(stub)).lifetime.held_microusd).toBe(String(RESERVE));
    provider.finish();
    const done = (await events(response)).at(-1);
    expect(done.status).toBe("completed");
    expect(done.charged_microusd).toBe("25");
    expect(done.usage).toEqual({ input_tokens: 11, output_tokens: 7 });
    expect((await allowance(stub, user.token)).daily).toMatchObject({ spent_microusd: "25", held_microusd: "0", remaining_microusd: "99975" });
    expect((await status(stub)).lifetime.spent_microusd).toBe("25");
  });

  it("admits only one parallel request against one user's last reservation", async () => {
    const stub = await fixture({ user_daily_microusd: Number(RESERVE), user_concurrency: 32, global_concurrency: 32 });
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const responses = await Promise.all(Array.from({ length: 16 }, () => respond(stub, user.token)));
    expect(responses.filter(r => r.status === 200)).toHaveLength(1);
    expect(responses.filter(r => r.status === 429)).toHaveLength(15);
    expect(provider.calls()).toBe(1);
    provider.finish();
    await events(responses.find(r => r.status === 200)!);
    expect((await status(stub)).lifetime.spent_microusd).toBe("20");
  });

  it("atomically coordinates different users racing for the global budget", async () => {
    const stub = await fixture({ global_monthly_microusd: Number(RESERVE), global_concurrency: 32 });
    const users = await Promise.all(Array.from({ length: 8 }, () => enroll(stub)));
    const provider = await controlled(stub);
    const responses = await Promise.all(users.map(u => respond(stub, u.token)));
    expect(responses.filter(r => r.status === 200)).toHaveLength(1);
    expect(responses.filter(r => r.status === 429)).toHaveLength(7);
    expect(provider.calls()).toBe(1);
    expect((await status(stub)).monthly.remaining_microusd).toBe("0");
    provider.finish();
    await events(responses.find(r => r.status === 200)!);
  });

  it.each(["user", "global"])("enforces %s concurrency independently of money", async scope => {
    const stub = await fixture({ user_concurrency: scope === "user" ? 1 : 4, global_concurrency: scope === "global" ? 1 : 4 });
    const a = await enroll(stub), b = scope === "user" ? a : await enroll(stub);
    const provider = await controlled(stub);
    const first = await respond(stub, a.token);
    const denied = await respond(stub, b.token);
    expect(denied.status).toBe(429);
    expect((await doc(denied)).error).toBe("concurrency_limit");
    expect(provider.calls()).toBe(1);
    provider.finish();
    await events(first);
  });

  it("carries outstanding liability through UTC midnight and month boundary, settling only the captured buckets", async () => {
    const stub = await fixture({ user_daily_microusd: Number(RESERVE), user_monthly_microusd: Number(RESERVE), global_monthly_microusd: Number(RESERVE) });
    const before = Date.UTC(2032, 0, 31, 23, 59, 59);
    await frozenClock(stub, before);
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const response = await respond(stub, user.token);
    await frozenClock(stub, before + 2000);
    const pending = await allowance(stub, user.token);
    expect(pending.reset_timezone).toBe("UTC");
    expect(pending.daily).toMatchObject({ spent_microusd: "0", held_microusd: String(RESERVE), remaining_microusd: "0", resets_at: Date.UTC(2032, 1, 2) });
    expect(pending.monthly.resets_at).toBe(Date.UTC(2032, 2, 1));
    expect((await respond(stub, user.token)).status).toBe(429);
    provider.finish();
    await events(response);
    expect((await allowance(stub, user.token)).monthly).toMatchObject({ spent_microusd: "0", held_microusd: "0", remaining_microusd: String(RESERVE) });
    expect((await status(stub)).monthly.spent_microusd).toBe("0");
    expect((await status(stub)).lifetime.spent_microusd).toBe("20");
    const buckets = await inside(stub, (_, state) => state.storage.sql.exec("SELECT * FROM buckets").toArray());
    expect(buckets).toContainEqual({ scope: "global-month", bucket: "2032-01", spent: "20" });
  });

  it("manual reset leaves holds and owner spend intact, and a late settlement cannot credit the new generation", async () => {
    const stub = await fixture({ user_daily_microusd: Number(RESERVE), user_monthly_microusd: Number(RESERVE) });
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const first = await respond(stub, user.token);
    await api(stub, `/admin/users/${user.user_id}/reset`, { daily: true, monthly: true });
    expect((await allowance(stub, user.token)).daily.remaining_microusd).toBe("0");
    expect((await respond(stub, user.token)).status).toBe(429);
    provider.finish();
    await events(first);
    const fresh = await allowance(stub, user.token);
    expect(fresh.daily).toMatchObject({ spent_microusd: "0", held_microusd: "0", remaining_microusd: String(RESERVE) });
    expect((await status(stub)).lifetime.spent_microusd).toBe("20");
    await events(await respond(stub, user.token));
    expect((await allowance(stub, user.token)).daily.spent_microusd).toBe("20");
    expect((await status(stub)).lifetime.spent_microusd).toBe("40");
  });

  it("never resets the owner lifetime ceiling even after a user reset and month roll-over", async () => {
    const stub = await fixture({ global_lifetime_microusd: Number(RESERVE) });
    const now = Date.UTC(2032, 0, 31, 23, 59, 50);
    await frozenClock(stub, now);
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const first = await respond(stub, user.token);
    provider.finish();
    await events(first);
    await api(stub, `/admin/users/${user.user_id}/reset`, { daily: true, monthly: true });
    await frozenClock(stub, now + 20_000);
    const denied = await respond(stub, user.token);
    expect(denied.status).toBe(429);
    expect((await doc(denied)).error).toBe("global_lifetime_limit");
    expect((await status(stub)).lifetime.spent_microusd).toBe("20");
  });

  it("settles with captured rates even if the catalog changes during the request", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const provider = await controlled(stub);
    const first = await respond(stub, user.token);
    await inside(stub, i => {
      const model = i.policy.models["mock-text"];
      if (model.provider === "mock") model.input_microusd_per_million_tokens *= 100;
    });
    provider.finish();
    expect((await events(first)).at(-1).charged_microusd).toBe("20");
  });

  it("records the full authoritative overrun and commits a durable breaker that admin pause cannot clear", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const provider = await controlled(stub, { input: 9000, output: 101 });
    const first = await respond(stub, user.token);
    provider.finish();
    const result = (await events(first)).at(-1);
    expect(result).toMatchObject({ type: "error", status: "overrun", error: "provider_bound_exceeded" });
    const global = await status(stub);
    expect(global).toMatchObject({ paused: true, breached: true });
    expect(global.lifetime).toMatchObject({ spent_microusd: "9202", held_microusd: "0" });
    expect((await respond(stub, user.token)).status).toBe(503);
    expect((await api(stub, "/admin/pause", { paused: false })).status).toBe(409);
    expect((await doc(await api(stub, `/v1/requests/${result.request_id}`, undefined, user.token))).charged_microusd).toBe("9202");
  });

  it("retains exact integer cost even for enormous valid usage and freezes instead of overflowing", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const provider = await controlled(stub, { input: Number.MAX_SAFE_INTEGER, output: Number.MAX_SAFE_INTEGER });
    const first = await respond(stub, user.token);
    provider.finish();
    expect((await events(first)).at(-1).status).toBe("overrun");
    expect((await status(stub)).lifetime.spent_microusd).toBe(String(BigInt(Number.MAX_SAFE_INTEGER) * 3n));
  });

  it("uses caller output limits in the worst-case reserve and rejects client rates/usage", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const first = await respond(stub, user.token, crypto.randomUUID(), { ...INPUT, max_output_tokens: 1 });
    const all = await events(first);
    expect(all[0].reserved_microusd).toBe("8194");
    for (const extra of [{ usage: { input_tokens: 0 } }, { input_rate: 0 }, { remaining_microusd: 1000000000 }, { user_id: user.user_id }]) {
      expect((await respond(stub, user.token, crypto.randomUUID(), { ...INPUT, ...extra })).status).toBe(400);
    }
  });
});
