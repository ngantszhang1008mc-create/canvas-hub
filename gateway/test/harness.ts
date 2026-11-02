import { env } from "cloudflare:workers";
import { runInDurableObject } from "cloudflare:test";
import { expect } from "vitest";
import type { Env, Policy } from "../src/config";
import type { AllowanceGateway } from "../src/index";
import type { Ledger } from "../src/ledger";
import type { ProviderAdapter } from "../src/providers";

export const bindings = env as unknown as Env;
export const ADMIN = "test_only_admin_credential_000000000000000000000000";
export const INPUT = { model: "mock-text", messages: [{ role: "user", content: "A synthetic test prompt." }], max_output_tokens: 100 };
export const RESERVE = 8392n; // 8192 mock input bound + 100 output * 2.

interface Internals { policy: Policy; ledger: Ledger; clock: () => number; adapter: (model: unknown) => ProviderAdapter }

export function inside<T>(stub: DurableObjectStub, fn: (i: Internals, state: DurableObjectState) => T | Promise<T>): Promise<T> {
  return runInDurableObject<AllowanceGateway, T>(stub, (i, state) => fn(i as unknown as Internals, state));
}

export async function fixture(patch: Partial<Policy> = {}) {
  const stub = bindings.GATEWAY.get(bindings.GATEWAY.newUniqueId());
  await inside(stub, i => { Object.assign(i.policy, patch); });
  return stub;
}

export function api(stub: DurableObjectStub, path: string, body?: unknown, token = ADMIN, headers: Record<string, string> = {}) {
  return stub.fetch(`https://gateway.test${path}`, { method: body === undefined ? "GET" : "POST", headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body === undefined ? {} : { "Content-Type": "application/json" }), "X-Gateway-Mode": "mock", ...headers }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
}

// Deliberately untyped JSON only in tests, to keep contract assertions readable.
export async function doc(response: Response): Promise<any> { return response.json(); }

export async function invite(stub: DurableObjectStub, userId?: string, ttl = 3600) {
  const response = await api(stub, "/admin/invites", { expires_in_seconds: ttl, ...(userId ? { user_id: userId } : {}) });
  expect(response.status).toBe(201);
  return doc(response);
}

export async function enroll(stub: DurableObjectStub, userId?: string) {
  const created = await invite(stub, userId);
  const response = await api(stub, "/v1/enroll", { invite: created.invite }, "");
  expect(response.status).toBe(201);
  return doc(response);
}

export function respond(stub: DurableObjectStub, token: string, key = crypto.randomUUID(), body: unknown = INPUT, mode = "mock") {
  return api(stub, "/v1/responses", body, token, { "Idempotency-Key": key, "X-Gateway-Mode": mode });
}

export async function events(response: Response): Promise<any[]> {
  expect(response.status).toBe(200);
  expect(response.headers.get("Content-Type")).toBe("application/x-ndjson");
  return new TextDecoder().decode(await response.arrayBuffer()).trim().split("\n").map(line => JSON.parse(line));
}

export async function allowance(stub: DurableObjectStub, token: string) { return doc(await api(stub, "/v1/allowance", undefined, token)); }
export async function status(stub: DurableObjectStub) { return doc(await api(stub, "/admin/status")); }

export function gate() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

export async function controlled(stub: DurableObjectStub, options: { input?: number; output?: number; fail?: boolean; missingUsage?: boolean } = {}) {
  const started = gate();
  const finish = gate();
  let calls = 0;
  await inside(stub, i => {
    i.adapter = () => ({ prepare() { return { async *stream() {
      calls++;
      started.release();
      yield { type: "text.delta" as const, delta: "synthetic" };
      await finish.promise;
      if (options.fail) throw new Error("DO_NOT_LEAK_PROVIDER_SECRET_OR_PROMPT");
      return { usage: options.missingUsage ? null : { input_tokens: options.input ?? 10, output_tokens: options.output ?? 5 }, finish_reason: "stop" as const };
    } }; } });
  });
  return { started: started.promise, finish: finish.release, calls: () => calls };
}

export async function frozenClock(stub: DurableObjectStub, now: number) {
  await inside(stub, i => { i.clock = () => now; });
}
