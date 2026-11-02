import { describe, expect, it } from "vitest";
import { allowance, api, controlled, doc, enroll, fixture, inside, INPUT, respond } from "./harness";

describe("mode consent at reservation time", () => {
  it("rejects a mock consent after the operator changes mode, before any reservation or dispatch", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const before = await allowance(stub, user.token);
    expect(before.capabilities.mode_header).toBe("X-Gateway-Mode");
    const provider = await controlled(stub);
    await inside(stub, i => { i.policy.mode = "production"; });
    const response = await respond(stub, user.token);
    expect(response.status).toBe(409);
    expect(await doc(response)).toEqual({ error: "mode_mismatch" });
    expect(provider.calls()).toBe(0);
    const after = await allowance(stub, user.token);
    expect(after.daily.held_microusd).toBe(before.daily.held_microusd);
    expect(after.daily.spent_microusd).toBe(before.daily.spent_microusd);
    provider.finish();
  });

  it("requires an explicit mode header", async () => {
    const stub = await fixture();
    const user = await enroll(stub);
    const response = await api(stub, "/v1/responses", INPUT, user.token,
      { "Idempotency-Key": crypto.randomUUID(), "X-Gateway-Mode": "" });
    expect(response.status).toBe(409);
    expect((await allowance(stub, user.token)).daily.held_microusd).toBe("0");
  });
});
