import { DurableObject } from "cloudflare:workers";
import { adminMatches, bearer, bodyFingerprint, digest, opaqueToken, peerDigest } from "./crypto";
import { assertConfigured, exactKeys, integer, MAX_AMOUNT, newApiRelayKeys, parsePolicy, SINGLETON, type Env, type ModelPolicy, type Policy } from "./config";
import { errorResponse, GatewayError, json, responseHeaders } from "./errors";
import { Ledger, publicRequest, type RequestRow, type SessionRow } from "./ledger";
import { adapterFor, PreflightError, type Completion, type Delta, type PreparedCall } from "./providers";
import { generation, idempotencyKey, readJson, type GenerationRequest } from "./validation";

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      assertConfigured(parsePolicy(env.POLICY), env);
      if (request.headers.has("Origin") || request.headers.has("Cookie")) throw new GatewayError(403, "local_process_only");
      if (new URL(request.url).search) throw new GatewayError(400, "query_not_allowed");
      const headers = new Headers(request.headers);
      // Never accept a client-chosen rate scope or any credential cookie.
      headers.set("X-Gateway-Peer", await peerDigest(request, env.ADMIN_SECRET));
      return await env.GATEWAY.get(env.GATEWAY.idFromName(SINGLETON)).fetch(new Request(request, { headers }));
    } catch (e) {
      return errorResponse(e);
    }
  },
} satisfies ExportedHandler<Env>;

export class AllowanceGateway extends DurableObject<Env> {
  private ledger: Ledger;
  private policy: Policy;
  // These local seams are exercised inside workerd by runInDurableObject.
  // There are no HTTP endpoints to select time, adapter, or usage.
  private clock = () => Date.now();
  private adapter = (model: ModelPolicy, userId?: string) => adapterFor(model, this.env,
    model.provider === "new_api" && userId ? this.ledger.relayKeyRef(userId) : null);

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.policy = parsePolicy(env.POLICY);
    this.ledger = new Ledger(ctx.storage);
  }

  private scheduleAlarm(): Promise<void> {
    // The query and alarm write have no intervening await: overlapping fetches
    // cannot overwrite an earlier deadline with a stale later decision.
    const deadline = this.ledger.nextDeadline();
    return deadline === null ? this.ctx.storage.deleteAlarm()
      : this.ctx.storage.setAlarm(Math.max(this.clock() + 1, deadline));
  }

  async alarm(): Promise<void> {
    this.ledger.expireStale(this.clock());
    await this.scheduleAlarm();
  }

  async fetch(request: Request): Promise<Response> {
    try {
      assertConfigured(this.policy, this.env);
      if (request.headers.has("Origin") || request.headers.has("Cookie")) throw new GatewayError(403, "local_process_only");
      const url = new URL(request.url);
      if (url.search) throw new GatewayError(400, "query_not_allowed");
      const peer = request.headers.get("X-Gateway-Peer") ?? "unknown";
      if (peer !== "unknown" && !/^[0-9a-f]{64}$/.test(peer)) throw new GatewayError(400, "invalid_request");
      const now = this.clock();
      this.ledger.expireStale(now);
      const admin = url.pathname.startsWith("/admin/");
      this.ledger.rateLimit([
        { key: "ingress:global", limit: this.policy.global_ingress_per_minute },
        { key: `ingress:${peer}`, limit: this.policy.ingress_per_minute },
        ...(admin ? [{ key: "admin:global", limit: this.policy.admin_attempts_per_minute * 4 }, { key: `admin:${peer}`, limit: this.policy.admin_attempts_per_minute }] : []),
      ], now);

      if (admin) {
        if (!(await adminMatches(bearer(request), this.env.ADMIN_SECRET))) throw new GatewayError(401, "invalid_admin");
        return await this.adminRoute(request, url.pathname);
      }
      if (url.pathname === "/v1/enroll" && request.method === "POST") {
        this.authRate(peer);
        const body = await readJson(request, 1024);
        exactKeys(body, ["invite"]);
        if (typeof body.invite !== "string" || !/^inv_[A-Za-z0-9_-]{43}$/.test(body.invite)) throw new GatewayError(401, "invalid_invite");
        const token = opaqueToken("ses");
        const [inviteHash, sessionHash] = await Promise.all([digest(body.invite), digest(token)]);
        const enrolled = this.ledger.enroll(inviteHash, sessionHash, crypto.randomUUID(), crypto.randomUUID(), this.policy, this.clock());
        return json({ ...enrolled, token }, 201);
      }
      const token = bearer(request);
      if (!/^ses_[A-Za-z0-9_-]{43}$/.test(token)) {
        this.authRate(peer);
        throw new GatewayError(401, "invalid_session");
      }
      let session: SessionRow;
      try { session = this.ledger.authenticate(await digest(token), this.clock()); }
      catch (e) { this.authRate(peer); throw e; }
      this.ledger.rateLimit([{ key: `user:${session.user_id}`, limit: this.policy.user_requests_per_minute }], this.clock());

      if (url.pathname === "/v1/allowance" && request.method === "GET") {
        return json(this.ledger.allowance(session.user_id, this.policy, this.clock()));
      }
      const statusPath = /^\/v1\/requests\/([0-9a-f-]{36})$/.exec(url.pathname);
      if (statusPath && request.method === "GET") {
        const row = this.ledger.request(statusPath[1]);
        if (!row || row.user_id !== session.user_id) throw new GatewayError(404, "not_found");
        return json(publicRequest(row));
      }
      if (url.pathname === "/v1/responses" && request.method === "POST") {
        return await this.respond(request, session);
      }
      throw new GatewayError(404, "not_found");
    } catch (error) {
      // Unknown errors are deliberately not logged or reflected. Provider
      // exceptions can contain credentials or the full submitted prompt.
      return errorResponse(error);
    }
  }

  private authRate(peer: string): void {
    this.ledger.rateLimit([
      { key: "auth:global", limit: this.policy.auth_attempts_per_minute * 4 },
      { key: `auth:${peer}`, limit: this.policy.auth_attempts_per_minute },
    ], this.clock());
  }

  private async adminRoute(request: Request, path: string): Promise<Response> {
    if (path === "/admin/status" && request.method === "GET") return json(this.ledger.status(this.policy, this.clock()));
    if (request.method !== "POST") throw new GatewayError(404, "not_found");
    const body = await readJson(request, 2048);
    const now = this.clock();
    const bindingPath = /^\/admin\/users\/([0-9a-f-]{36})\/new-api$/.exec(path);
    if (bindingPath) {
      exactKeys(body, ["relay_key_ref"]);
      if (this.env.NEW_API_IDENTITY_MODE !== "per_user" || typeof body.relay_key_ref !== "string"
        || !/^[A-Za-z0-9_-]{1,64}$/.test(body.relay_key_ref)
        || !Object.hasOwn(newApiRelayKeys(this.env), body.relay_key_ref)) throw new GatewayError(400, "invalid_relay_binding");
      this.ledger.bindRelayKey(bindingPath[1], body.relay_key_ref, now);
      return json({ ok: true });
    }
    if (path === "/admin/invites") {
      exactKeys(body, ["expires_in_seconds", "user_id"], ["expires_in_seconds"]);
      if (!integer(body.expires_in_seconds, 1, this.policy.invite_ttl_max_seconds)
        || (body.user_id !== undefined && (typeof body.user_id !== "string" || !/^[0-9a-f-]{36}$/.test(body.user_id)))) {
        throw new GatewayError(400, "invalid_invite_options");
      }
      const invite = opaqueToken("inv");
      const id = crypto.randomUUID();
      const hash = await digest(invite);
      const expiresAt = this.clock() + body.expires_in_seconds * 1000;
      this.ledger.createInvite(id, hash, body.user_id as string | undefined ?? null, expiresAt, this.clock());
      return json({ invite_id: id, invite, expires_at: expiresAt }, 201);
    }
    const limitsPath = /^\/admin\/users\/([0-9a-f-]{36})\/limits$/.exec(path);
    if (limitsPath) {
      exactKeys(body, ["daily_microusd", "monthly_microusd"]);
      if (!integer(body.daily_microusd, 0, MAX_AMOUNT) || !integer(body.monthly_microusd, 0, MAX_AMOUNT)) throw new GatewayError(400, "invalid_limits");
      this.ledger.setLimits(limitsPath[1], body.daily_microusd, body.monthly_microusd, now);
      return json({ ok: true });
    }
    const resetPath = /^\/admin\/users\/([0-9a-f-]{36})\/reset$/.exec(path);
    if (resetPath) {
      exactKeys(body, ["daily", "monthly"]);
      if (typeof body.daily !== "boolean" || typeof body.monthly !== "boolean" || !(body.daily || body.monthly)) throw new GatewayError(400, "invalid_reset");
      this.ledger.resetUser(resetPath[1], body.daily, body.monthly, now);
      return json({ ok: true });
    }
    const revokePath = /^\/admin\/(users|sessions|invites)\/([0-9a-f-]{36})\/revoke$/.exec(path);
    if (revokePath) {
      exactKeys(body, []);
      this.ledger.revoke(revokePath[1] as "users" | "sessions" | "invites", revokePath[2], now);
      return json({ ok: true });
    }
    if (path === "/admin/pause") {
      exactKeys(body, ["paused"]);
      if (typeof body.paused !== "boolean") throw new GatewayError(400, "invalid_pause");
      this.ledger.setPaused(body.paused, now);
      return json({ ok: true });
    }
    throw new GatewayError(404, "not_found");
  }

  private async respond(request: Request, session: SessionRow): Promise<Response> {
    // Bind consent on EVERY round, including after a deployment/mode change.
    // This happens before any reservation or provider dispatch.
    if (request.headers.get("X-Gateway-Mode") !== this.policy.mode) throw new GatewayError(409, "mode_mismatch");
    const key = idempotencyKey(request);
    const body = await readJson(request, this.policy.max_body_bytes);
    const { input, model } = generation(body, this.policy);
    if (model.provider === "new_api" && request.headers.get("X-Gateway-Consent-Version") !== "2") {
      throw new GatewayError(409, "disclosure_required");
    }
    const [keyHash, bodyHash] = await Promise.all([digest(key), bodyFingerprint(JSON.stringify(input), this.env.ADMIN_SECRET)]);
    const reserved = this.ledger.reserve(session, crypto.randomUUID(), keyHash, bodyHash, input.model, model, input.max_output_tokens, this.policy, this.clock());
    if (reserved.replay) {
      return json({ ...publicRequest(reserved.row), replay: true, output_replay_available: false }, ["reserved", "dispatched"].includes(reserved.row.status) ? 202 : 200);
    }
    const row = reserved.row;
    let prepared: PreparedCall;
    try {
      prepared = this.adapter(model, session.user_id).prepare(input, model, this.policy);
      if (request.signal.aborted) throw new GatewayError(499, "client_disconnected");
      this.ledger.markDispatched(row.id, session, this.clock());
      await this.scheduleAlarm();
      // Explicitly flush the reservation and dispatch intent before any
      // provider adapter is invoked, in addition to DO output-gate protection.
      await this.ctx.storage.sync();
      if (request.signal.aborted) throw new GatewayError(499, "client_disconnected");
      if (this.clock() >= row.deadline_at) throw new GatewayError(408, "request_timeout");
    } catch (e) {
      const code = e instanceof PreflightError ? e.code : e instanceof GatewayError ? e.code : "preflight_failed";
      this.ledger.failBeforeDispatch(row.id, code, this.clock());
      await this.scheduleAlarm();
      throw new GatewayError(e instanceof GatewayError ? e.status : 502, code, row.id);
    }
    return this.stream(request, row, input, prepared);
  }

  private stream(request: Request, row: RequestRow, input: GenerationRequest, prepared: PreparedCall): Response {
    const abort = new AbortController();
    const encoder = new TextEncoder();
    let bytes = 0;
    let closed = false;
    let controller: ReadableStreamDefaultController<Uint8Array>;
    let stopReason = "client_disconnected";
    const stop = (reason: string) => { if (!abort.signal.aborted) { stopReason = reason; abort.abort(); } };
    const signalAbort = () => stop("client_disconnected");
    request.signal.addEventListener("abort", signalAbort, { once: true });
    const emit = (event: unknown) => {
      if (closed) return;
      const chunk = encoder.encode(`${JSON.stringify(event)}\n`);
      bytes += chunk.byteLength;
      if (bytes > this.policy.max_stream_bytes) throw new GatewayError(502, "stream_too_large");
      controller.enqueue(chunk);
    };
    const stream = new ReadableStream<Uint8Array>({
      start(c) { controller = c; },
      cancel() { closed = true; stop("client_disconnected"); },
    });
    const timer = setTimeout(() => stop("request_timeout"), Math.max(0, row.deadline_at - this.clock()));
    const aborted = new Promise<never>((_, reject) => {
      abort.signal.addEventListener("abort", () => reject(new GatewayError(502, stopReason)), { once: true });
    });
    // Install a handler even if abort happens just after the final iterator
    // result, so the cancellation promise never becomes an unhandled rejection.
    void aborted.catch(() => {});
    const pump = async () => {
      let iterator: AsyncGenerator<Delta, Completion, void> | undefined;
      try {
        emit({ type: "meta", request_id: row.id, reserved_microusd: row.reserve_microusd });
        abort.signal.throwIfAborted();
        iterator = prepared.stream(abort.signal);
        const calls = new Map<number, { id: string; name: string; arguments: string }>();
        while (true) {
          const next = await Promise.race([iterator.next(), aborted]);
          abort.signal.throwIfAborted();
          if (next.done) {
            const reason = next.value?.finish_reason;
            if ((reason === "tool_calls" && calls.size === 0) || (reason === "stop" && calls.size > 0)) {
              throw new GatewayError(502, "provider_protocol_error");
            }
            if (reason === "tool_calls") {
              for (const call of calls.values()) {
                let parsed: unknown;
                try { parsed = JSON.parse(call.arguments); } catch { throw new GatewayError(502, "provider_protocol_error"); }
                if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new GatewayError(502, "provider_protocol_error");
              }
            }
            const settled = this.ledger.settle(row.id, next.value, this.clock());
            if (settled.status === "completed") emit({ type: "done", ...publicRequest(settled), finish_reason: next.value.finish_reason });
            else emit({ type: "error", request_id: row.id, error: settled.error_code, status: settled.status });
            break;
          }
          const event = next.value;
          if (event.type === "text.delta" && typeof event.delta === "string") {
            emit({ type: "text.delta", delta: event.delta });
          } else if (event.type === "tool_call.delta" && integer(event.index, 0, 31)) {
            let call = calls.get(event.index);
            if (!call) {
              if (typeof event.id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(event.id)
                || typeof event.name !== "string" || !input.tools.includes(event.name)
                || [...calls.values()].some(c => c.id === event.id)) throw new GatewayError(502, "provider_protocol_error");
              call = { id: event.id, name: event.name, arguments: "" };
              calls.set(event.index, call);
            } else if ((event.id !== undefined && event.id !== call.id) || (event.name !== undefined && event.name !== call.name)) {
              throw new GatewayError(502, "provider_protocol_error");
            }
            if (event.arguments_delta !== undefined && typeof event.arguments_delta !== "string") throw new GatewayError(502, "provider_protocol_error");
            call.arguments += event.arguments_delta ?? "";
            emit({ type: "tool_call.delta", index: event.index, ...(event.id ? { id: event.id } : {}), ...(event.name ? { name: event.name } : {}), ...(event.arguments_delta !== undefined ? { arguments_delta: event.arguments_delta } : {}) });
          } else {
            throw new GatewayError(502, "provider_protocol_error");
          }
        }
      } catch (e) {
        const code = abort.signal.aborted ? stopReason : e instanceof GatewayError ? e.code : "provider_failed";
        this.ledger.uncertain(row.id, code, this.clock(), code === "provider_protocol_error");
        stop(code);
        try { emit({ type: "error", request_id: row.id, error: code, status: this.ledger.request(row.id)!.status }); } catch { /* disconnected or size bounded */ }
      } finally {
        clearTimeout(timer);
        request.signal.removeEventListener("abort", signalAbort);
        // Do not wait on an uncooperative iterator after timeout/disconnect.
        if (abort.signal.aborted && iterator) void iterator.return(undefined as never).catch(() => {});
        await this.scheduleAlarm();
        if (!closed) { closed = true; controller.close(); }
      }
    };
    this.ctx.waitUntil(pump().catch(() => {
      // Storage/worker termination can make the failure-settlement path itself
      // fail. The durable hold remains for alarm/next-request recovery. Close
      // the consumer with a fixed error rather than logging an internal error.
      if (!closed) { closed = true; controller.error(new Error("gateway_interrupted")); }
    }));
    return new Response(stream, { headers: { ...responseHeaders, "Content-Type": "application/x-ndjson", "X-Request-ID": row.id } });
  }
}
