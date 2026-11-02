import { describe, expect, it } from "vitest";
import { digest } from "../src/crypto";
import { ADMIN, allowance, api, doc, enroll, fixture, frozenClock, inside, invite, respond, events, status, INPUT } from "./harness";

describe("invitation and session lifecycle in SQLite DO", () => {
  it("requires admin authentication and an unexpired one-use invitation", async () => {
    const stub = await fixture();
    expect((await api(stub, "/admin/invites", { expires_in_seconds: 60 }, "")).status).toBe(401);
    expect((await api(stub, "/admin/status", undefined, "wrong_admin_credential_0000000000000000")).status).toBe(401);
    expect((await api(stub, "/v1/allowance", undefined, "")).status).toBe(401);
    const invitation = await invite(stub);
    const responses = await Promise.all(Array.from({ length: 12 }, () => api(stub, "/v1/enroll", { invite: invitation.invite }, "")));
    expect(responses.filter(r => r.status === 201)).toHaveLength(1);
    expect(responses.filter(r => r.status === 401)).toHaveLength(11);
    expect((await status(stub)).users).toBe(1);
  });

  it("stores only random-token hashes, never raw invitations or sessions", async () => {
    const stub = await fixture();
    const invitation = await invite(stub);
    const user = await doc(await api(stub, "/v1/enroll", { invite: invitation.invite }, ""));
    expect(invitation.invite).toMatch(/^inv_[A-Za-z0-9_-]{43}$/);
    expect(user.token).toMatch(/^ses_[A-Za-z0-9_-]{43}$/);
    const rows = await inside(stub, (_, state) => ({ invites: state.storage.sql.exec("SELECT * FROM invites").toArray(), sessions: state.storage.sql.exec("SELECT * FROM sessions").toArray() }));
    const serialized = JSON.stringify(rows);
    expect(serialized).not.toContain(invitation.invite);
    expect(serialized).not.toContain(user.token);
    expect(serialized).toContain(await digest(invitation.invite));
    expect(serialized).toContain(await digest(user.token));
    expect((await allowance(stub, user.token)).user_id).toBe(user.user_id);
  });

  it("enforces exact invite/session expiry boundaries", async () => {
    const stub = await fixture({ session_ttl_seconds: 60 });
    const now = Date.UTC(2032, 0, 1);
    await frozenClock(stub, now);
    const invitation = await invite(stub, undefined, 2);
    await frozenClock(stub, now + 2000);
    expect((await api(stub, "/v1/enroll", { invite: invitation.invite }, "")).status).toBe(401);
    const user = await enroll(stub);
    await frozenClock(stub, user.expires_at - 1);
    expect((await api(stub, "/v1/allowance", undefined, user.token)).status).toBe(200);
    await frozenClock(stub, user.expires_at);
    expect((await api(stub, "/v1/allowance", undefined, user.token)).status).toBe(401);
  });

  it("recovers the same user without erasing spend or idempotency history", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const key = crypto.randomUUID();
    const done = (await events(await respond(stub, user.token, key))).at(-1);
    const recovery = await enroll(stub, user.user_id);
    expect(recovery.user_id).toBe(user.user_id);
    expect((await api(stub, "/v1/allowance", undefined, user.token)).status).toBe(401);
    expect((await allowance(stub, recovery.token)).daily.spent_microusd).toBe(done.charged_microusd);
    const replay = await doc(await respond(stub, recovery.token, key, INPUT));
    expect(replay.replay).toBe(true);
    expect(replay.request_id).toBe(done.request_id);
    expect((await status(stub)).users).toBe(1);
  });

  it("revokes invites and sessions and disables accounts including pending recovery invites", async () => {
    const stub = await fixture();
    const invitation = await invite(stub);
    expect((await api(stub, `/admin/invites/${invitation.invite_id}/revoke`, {})).status).toBe(200);
    expect((await api(stub, "/v1/enroll", { invite: invitation.invite }, "")).status).toBe(401);
    const first = await enroll(stub);
    await api(stub, `/admin/sessions/${first.session_id}/revoke`, {});
    expect((await api(stub, "/v1/allowance", undefined, first.token)).status).toBe(401);
    const recovery = await invite(stub, first.user_id);
    await api(stub, `/admin/users/${first.user_id}/revoke`, {});
    expect((await api(stub, "/v1/enroll", { invite: recovery.invite }, "")).status).toBe(401);
    expect((await api(stub, "/admin/invites", { expires_in_seconds: 60, user_id: first.user_id })).status).toBe(409);
  });

  it("prevents enrollment beyond configured user count and never accepts caller identity", async () => {
    const stub = await fixture({ max_users: 1 });
    const first = await enroll(stub);
    const invitation = await invite(stub);
    expect((await api(stub, "/v1/enroll", { invite: invitation.invite, user_id: first.user_id }, "")).status).toBe(400);
    expect((await api(stub, "/v1/enroll", { invite: invitation.invite }, "")).status).toBe(429);
    expect((await enroll(stub, first.user_id)).user_id).toBe(first.user_id);
  });

  it("requires explicit authenticated limit/reset adjustments without freeform audit fields", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    expect((await api(stub, `/admin/users/${user.user_id}/limits`, { daily_microusd: 500, monthly_microusd: 600 }, user.token)).status).toBe(401);
    expect((await api(stub, `/admin/users/${user.user_id}/reset`, { daily: false, monthly: false })).status).toBe(400);
    expect((await api(stub, `/admin/users/${user.user_id}/reset`, { daily: true })).status).toBe(400);
    expect((await api(stub, `/admin/users/${user.user_id}/limits`, { daily_microusd: 500, monthly_microusd: 600, reason: "secret" })).status).toBe(400);
    expect((await api(stub, `/admin/users/${user.user_id}/limits`, { daily_microusd: 500, monthly_microusd: 600 })).status).toBe(200);
    expect((await allowance(stub, user.token)).daily.limit_microusd).toBe("500");
    expect((await status(stub)).audit.find((a: any) => a.action === "limits_changed").values).toEqual({ daily_microusd: 500, monthly_microusd: 600 });
  });

  it("isolates request status and user balances", async () => {
    const stub = await fixture();
    const a = await enroll(stub), b = await enroll(stub);
    const result = (await events(await respond(stub, a.token))).at(-1);
    expect((await api(stub, `/v1/requests/${result.request_id}`, undefined, b.token)).status).toBe(404);
    expect((await allowance(stub, b.token)).daily.spent_microusd).toBe("0");
    expect((await api(stub, `/v1/requests/${result.request_id}`, undefined, ADMIN)).status).toBe(401);
  });
});
