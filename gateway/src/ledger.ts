import { integer, MAX_TOOLS, MAX_TOOL_CALLS, MAX_USAGE_TOKENS, price, type ModelPolicy, type Policy } from "./config";
import { GatewayError } from "./errors";
import type { Completion } from "./providers";
import { reservePrice, settlementPrice, snapshotPricing, type Pricing } from "./pricing";

type Bind = string | number | null;
export type RequestStatus = "reserved" | "dispatched" | "completed" | "failed_before_dispatch" | "uncertain" | "overrun";
export interface UserRow {
  id: string; created_at: number; disabled: number; daily_limit: number; monthly_limit: number;
  day_generation: number; month_generation: number; held: string;
}
export interface SessionRow { id: string; user_id: string; token_hash: string; expires_at: number; revoked: number }
interface InviteRow { id: string; token_hash: string; user_id: string | null; expires_at: number; revoked: number; used_at: number | null }
interface Control { held: string; lifetime_spent: string; paused: number; breached: number }
export interface RequestRow {
  id: string; user_id: string; key_hash: string; body_hash: string; model: string;
  status: RequestStatus; created_at: number; deadline_at: number; completed_at: number | null;
  dispatched_at: number | null; reserve_microusd: string; charged_microusd: string | null;
  day_bucket: string; month_bucket: string; global_month: string;
  input_rate: number; output_rate: number; max_input: number; max_output: number;
  input_tokens: number | null; output_tokens: number | null; error_code: string | null;
  pricing_json: string | null; usage_details_json: string | null;
}

const day = (now: number) => new Date(now).toISOString().slice(0, 10);
const month = (now: number) => new Date(now).toISOString().slice(0, 7);
const active = (status: RequestStatus) => status === "reserved" || status === "dispatched";
const held = (status: RequestStatus) => active(status) || status === "uncertain";
const nonnegative = (amount: bigint) => amount > 0n ? amount : 0n;

export class Ledger {
  constructor(private storage: DurableObjectStorage) {
    storage.transactionSync(() => {
      this.exec(`
        CREATE TABLE IF NOT EXISTS control (
          id INTEGER PRIMARY KEY CHECK(id=1), held TEXT NOT NULL, lifetime_spent TEXT NOT NULL,
          paused INTEGER NOT NULL, breached INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO control VALUES (1,'0','0',0,0);
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
          daily_limit INTEGER NOT NULL, monthly_limit INTEGER NOT NULL,
          day_generation INTEGER NOT NULL DEFAULT 0, month_generation INTEGER NOT NULL DEFAULT 0,
          held TEXT NOT NULL DEFAULT '0'
        );
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), token_hash TEXT NOT NULL UNIQUE,
          expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
        CREATE TABLE IF NOT EXISTS invites (
          id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, user_id TEXT REFERENCES users(id),
          expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, used_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS buckets (
          scope TEXT NOT NULL, bucket TEXT NOT NULL, spent TEXT NOT NULL,
          PRIMARY KEY(scope,bucket)
        );
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), key_hash TEXT NOT NULL,
          body_hash TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
          created_at INTEGER NOT NULL, deadline_at INTEGER NOT NULL, completed_at INTEGER,
          dispatched_at INTEGER, reserve_microusd TEXT NOT NULL, charged_microusd TEXT,
          day_bucket TEXT NOT NULL, month_bucket TEXT NOT NULL, global_month TEXT NOT NULL,
          input_rate INTEGER NOT NULL, output_rate INTEGER NOT NULL, max_input INTEGER NOT NULL, max_output INTEGER NOT NULL,
          input_tokens INTEGER, output_tokens INTEGER, error_code TEXT,
          UNIQUE(user_id,key_hash)
        );
        CREATE INDEX IF NOT EXISTS requests_active ON requests(status,deadline_at);
        CREATE INDEX IF NOT EXISTS requests_user ON requests(user_id,status);
        CREATE TABLE IF NOT EXISTS rate_limits (
          scope TEXT PRIMARY KEY, minute INTEGER NOT NULL, count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, action TEXT NOT NULL,
          target_id TEXT, values_json TEXT NOT NULL
        );
      `);
      // Additive migration: retain old mock accounting/idempotency rows.
      const columns = this.all<{ name: string }>("PRAGMA table_info(requests)").map(r => r.name);
      if (!columns.includes("pricing_json")) this.exec("ALTER TABLE requests ADD COLUMN pricing_json TEXT");
      if (!columns.includes("usage_details_json")) this.exec("ALTER TABLE requests ADD COLUMN usage_details_json TEXT");
    });
  }

  private exec(query: string, ...bindings: Bind[]): void { this.storage.sql.exec(query, ...bindings).toArray(); }
  private one<T>(query: string, ...bindings: Bind[]): T | undefined {
    return this.storage.sql.exec(query, ...bindings).toArray()[0] as unknown as T | undefined;
  }
  private all<T>(query: string, ...bindings: Bind[]): T[] {
    return this.storage.sql.exec(query, ...bindings).toArray() as unknown as T[];
  }
  private tx<T>(fn: () => T): T { return this.storage.transactionSync(fn); }
  private control(): Control { return this.one<Control>("SELECT * FROM control WHERE id=1")!; }
  private user(id: string): UserRow {
    const user = this.one<UserRow>("SELECT * FROM users WHERE id=?", id);
    if (!user) throw new GatewayError(404, "not_found");
    return user;
  }
  private spent(scope: string, bucket: string): bigint {
    return BigInt(this.one<{ spent: string }>("SELECT spent FROM buckets WHERE scope=? AND bucket=?", scope, bucket)?.spent ?? "0");
  }
  private addSpent(scope: string, bucket: string, amount: bigint): void {
    this.exec("INSERT INTO buckets(scope,bucket,spent) VALUES (?,?,?) ON CONFLICT(scope,bucket) DO UPDATE SET spent=excluded.spent",
      scope, bucket, String(this.spent(scope, bucket) + amount));
  }
  private audit(now: number, action: string, target: string | null, values: Record<string, string | number | boolean | null> = {}): void {
    // Callers supply only fixed action names, random IDs, booleans and amounts.
    this.exec("INSERT INTO audit(at,action,target_id,values_json) VALUES (?,?,?,?)", now, action, target, JSON.stringify(values));
  }

  rateLimit(scopes: { key: string; limit: number }[], now: number): void {
    const minute = Math.floor(now / 60_000);
    const allowed = this.tx(() => {
      this.exec("DELETE FROM rate_limits WHERE minute < ?", minute - 1);
      let ok = true;
      for (const scope of scopes) {
        const r = this.one<{ minute: number; count: number }>("SELECT minute,count FROM rate_limits WHERE scope=?", scope.key);
        const count = r?.minute === minute ? r.count : 0;
        if (count >= scope.limit) ok = false;
        // Saturation bounds counters even during sustained abuse. Denied
        // attempts still commit, rather than rolling back with an exception.
        this.exec("INSERT INTO rate_limits VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET minute=excluded.minute,count=excluded.count",
          scope.key, minute, Math.min(count + 1, scope.limit + 1));
        // A saturated global gate stops before creating arbitrary peer rows.
        if (!ok) break;
      }
      return ok;
    });
    if (!allowed) throw new GatewayError(429, "rate_limited");
  }

  authenticate(tokenHash: string, now: number): SessionRow {
    const session = this.one<SessionRow>("SELECT * FROM sessions WHERE token_hash=?", tokenHash);
    if (!session || session.revoked || session.expires_at <= now || this.user(session.user_id).disabled) {
      throw new GatewayError(401, "invalid_session");
    }
    return session;
  }

  createInvite(id: string, hash: string, existingUser: string | null, expiresAt: number, now: number): void {
    this.tx(() => {
      if (existingUser && this.user(existingUser).disabled) throw new GatewayError(409, "user_disabled");
      this.exec("INSERT INTO invites(id,token_hash,user_id,expires_at) VALUES (?,?,?,?)", id, hash, existingUser, expiresAt);
      this.audit(now, "invite_created", id, { user_id: existingUser, expires_at: expiresAt });
    });
  }

  enroll(inviteHash: string, sessionHash: string, sessionId: string, newUserId: string, policy: Policy, now: number) {
    return this.tx(() => {
      const invite = this.one<InviteRow>("SELECT * FROM invites WHERE token_hash=?", inviteHash);
      if (!invite || invite.revoked || invite.used_at !== null || invite.expires_at <= now) throw new GatewayError(401, "invalid_invite");
      const userId = invite.user_id ?? newUserId;
      if (invite.user_id) {
        if (this.user(userId).disabled) throw new GatewayError(401, "invalid_invite");
      } else {
        const count = this.one<{ count: number }>("SELECT COUNT(*) AS count FROM users")!.count;
        if (count >= policy.max_users) throw new GatewayError(429, "enrollment_full");
        this.exec("INSERT INTO users(id,created_at,daily_limit,monthly_limit) VALUES (?,?,?,?)", userId, now, policy.user_daily_microusd, policy.user_monthly_microusd);
      }
      const expiresAt = now + policy.session_ttl_seconds * 1000;
      this.exec("UPDATE invites SET used_at=? WHERE id=?", now, invite.id);
      // A recovery invite replaces sessions, never the account or its history.
      this.exec("UPDATE sessions SET revoked=1 WHERE user_id=?", userId);
      this.exec("INSERT INTO sessions(id,user_id,token_hash,expires_at) VALUES (?,?,?,?)", sessionId, userId, sessionHash, expiresAt);
      this.audit(now, "enrolled", userId, { invite_id: invite.id, session_id: sessionId });
      return { user_id: userId, session_id: sessionId, expires_at: expiresAt };
    });
  }

  reserve(session: SessionRow, id: string, keyHash: string, bodyHash: string, modelName: string, model: ModelPolicy, maxOutput: number, policy: Policy, now: number): { row: RequestRow; replay: boolean } {
    return this.tx(() => {
      this.expireStaleInside(now);
      this.authenticate(session.token_hash, now);
      const prior = this.one<RequestRow>("SELECT * FROM requests WHERE user_id=? AND key_hash=?", session.user_id, keyHash);
      if (prior) {
        if (prior.body_hash !== bodyHash) throw new GatewayError(409, "idempotency_conflict");
        return { row: prior, replay: true };
      }
      const control = this.control();
      if (control.paused || control.breached) throw new GatewayError(503, "gateway_paused");
      const user = this.user(session.user_id);
      const cost = reservePrice(model, maxOutput);
      const pricing = snapshotPricing(model);
      const dayBucket = `${day(now)}:${user.day_generation}`;
      const monthBucket = `${month(now)}:${user.month_generation}`;
      const globalMonth = month(now);
      const userHeld = BigInt(user.held);
      const globalHeld = BigInt(control.held);
      const checks: [bigint, number, string][] = [
        [this.spent(`user-day:${user.id}`, dayBucket) + userHeld + cost, user.daily_limit, "user_daily_limit"],
        [this.spent(`user-month:${user.id}`, monthBucket) + userHeld + cost, user.monthly_limit, "user_monthly_limit"],
        [this.spent("global-month", globalMonth) + globalHeld + cost, policy.global_monthly_microusd, "global_monthly_limit"],
        [BigInt(control.lifetime_spent) + globalHeld + cost, policy.global_lifetime_microusd, "global_lifetime_limit"],
      ];
      for (const [used, limit, code] of checks) if (used > BigInt(limit)) throw new GatewayError(429, code);
      const globalActive = this.one<{ n: number }>("SELECT COUNT(*) AS n FROM requests WHERE status IN ('reserved','dispatched')")!.n;
      const userActive = this.one<{ n: number }>("SELECT COUNT(*) AS n FROM requests WHERE user_id=? AND status IN ('reserved','dispatched')", user.id)!.n;
      if (globalActive >= policy.global_concurrency || userActive >= policy.user_concurrency) throw new GatewayError(429, "concurrency_limit");
      this.exec("UPDATE users SET held=? WHERE id=?", String(userHeld + cost), user.id);
      this.exec("UPDATE control SET held=? WHERE id=1", String(globalHeld + cost));
      this.exec(`INSERT INTO requests(id,user_id,key_hash,body_hash,model,status,created_at,deadline_at,reserve_microusd,
        day_bucket,month_bucket,global_month,input_rate,output_rate,max_input,max_output,pricing_json)
        VALUES (?,?,?,?,?,'reserved',?,?,?,?,?,?,?,?,?,?,?)`,
      id, user.id, keyHash, bodyHash, modelName, now, now + policy.request_timeout_ms, String(cost), dayBucket, monthBucket, globalMonth,
      pricing.short.input, pricing.short.output, model.max_input_tokens, maxOutput, JSON.stringify(pricing));
      return { row: this.request(id)!, replay: false };
    });
  }

  request(id: string): RequestRow | undefined { return this.one<RequestRow>("SELECT * FROM requests WHERE id=?", id); }

  markDispatched(id: string, session: SessionRow, now: number): void {
    this.tx(() => {
      this.authenticate(session.token_hash, now);
      const r = this.request(id)!;
      if (r.status !== "reserved" || r.deadline_at <= now) throw new GatewayError(408, "request_timeout");
      if (this.control().paused || this.control().breached) throw new GatewayError(503, "gateway_paused");
      this.exec("UPDATE requests SET status='dispatched',dispatched_at=? WHERE id=?", now, id);
    });
  }

  private releaseHold(r: RequestRow): void {
    const user = this.user(r.user_id);
    const control = this.control();
    const reserve = BigInt(r.reserve_microusd);
    const userHeld = BigInt(user.held) - reserve;
    const globalHeld = BigInt(control.held) - reserve;
    if (userHeld < 0n || globalHeld < 0n) throw new Error("ledger_invariant");
    this.exec("UPDATE users SET held=? WHERE id=?", String(userHeld), user.id);
    this.exec("UPDATE control SET held=? WHERE id=1", String(globalHeld));
  }

  failBeforeDispatch(id: string, code: string, now: number): void {
    this.tx(() => {
      const r = this.request(id)!;
      if (!held(r.status)) return;
      // Called only by the still-live request handler while it knows no
      // provider method was invoked. Stale recovery NEVER invokes this path.
      this.releaseHold(r);
      this.exec("UPDATE requests SET status='failed_before_dispatch',charged_microusd='0',completed_at=?,error_code=? WHERE id=?", now, code, id);
    });
  }

  uncertain(id: string, code: string, now: number, breach = false): void {
    this.tx(() => {
      const r = this.request(id)!;
      if (!held(r.status)) return;
      this.exec("UPDATE requests SET status='uncertain',completed_at=?,error_code=? WHERE id=?", now, code, id);
      if (breach) {
        this.exec("UPDATE control SET breached=1,paused=1 WHERE id=1");
        this.audit(now, "provider_breach", id, { code });
      }
    });
  }

  settle(id: string, completion: Completion, now: number): RequestRow {
    return this.tx(() => {
      const r = this.request(id)!;
      if (!held(r.status)) return r;
      const usage = completion?.usage;
      const counts = [usage?.cached_input_tokens ?? 0, usage?.cache_write_input_tokens ?? 0,
        usage?.cache_write_30m_input_tokens ?? 0, usage?.cache_write_12h_input_tokens ?? 0, usage?.reasoning_tokens ?? 0];
      const validDetails = counts.every(n => integer(n, 0, MAX_USAGE_TOKENS))
        && BigInt(counts[1]) === BigInt(counts[2]) + BigInt(counts[3])
        && usage && integer(usage.input_tokens, 0, MAX_USAGE_TOKENS) && integer(usage.output_tokens, 0, MAX_USAGE_TOKENS)
        && BigInt(counts[0]) + BigInt(counts[1]) <= BigInt(usage.input_tokens) && counts[4] <= usage.output_tokens;
      if (!usage || !integer(usage.input_tokens, 0, MAX_USAGE_TOKENS) || !integer(usage.output_tokens, 0, MAX_USAGE_TOKENS)
        || !validDetails || !["stop", "tool_calls", "length"].includes(completion.finish_reason)) {
        this.exec("UPDATE requests SET status='uncertain',completed_at=?,error_code='usage_missing_or_invalid' WHERE id=?", now, id);
        this.exec("UPDATE control SET breached=1,paused=1 WHERE id=1");
        this.audit(now, "provider_breach", id, { code: "usage_missing_or_invalid" });
        return this.request(id)!;
      }
      const cost = r.pricing_json ? settlementPrice(JSON.parse(r.pricing_json) as Pricing, usage)
        : price({ input_microusd_per_million_tokens: r.input_rate, output_microusd_per_million_tokens: r.output_rate }, usage.input_tokens, usage.output_tokens);
      const overrun = Boolean(completion.bound_violation) || cost > BigInt(r.reserve_microusd) || usage.input_tokens > r.max_input || usage.output_tokens > r.max_output;
      const details = usage.cached_input_tokens === undefined ? null : JSON.stringify({ cached_input_tokens: counts[0], cache_write_input_tokens: counts[1],
        cache_write_30m_input_tokens: counts[2], cache_write_12h_input_tokens: counts[3], reasoning_tokens: counts[4] });
      this.releaseHold(r);
      this.addSpent(`user-day:${r.user_id}`, r.day_bucket, cost);
      this.addSpent(`user-month:${r.user_id}`, r.month_bucket, cost);
      this.addSpent("global-month", r.global_month, cost);
      this.exec("UPDATE control SET lifetime_spent=? WHERE id=1", String(BigInt(this.control().lifetime_spent) + cost));
      this.exec("UPDATE requests SET status=?,charged_microusd=?,input_tokens=?,output_tokens=?,completed_at=?,error_code=?,usage_details_json=? WHERE id=?",
        overrun ? "overrun" : "completed", String(cost), usage.input_tokens, usage.output_tokens, now, overrun ? "provider_bound_exceeded" : null, details, id);
      if (overrun) {
        // Commit the full cost AND permanent circuit breaker together. Never
        // throw here: a rollback would conceal spend and undo the freeze.
        this.exec("UPDATE control SET breached=1,paused=1 WHERE id=1");
        this.audit(now, "provider_breach", id, { cost_microusd: String(cost), code: "provider_bound_exceeded" });
      }
      return this.request(id)!;
    });
  }

  private expireStaleInside(now: number): void {
    this.exec("UPDATE requests SET status='uncertain',completed_at=?,error_code='stale_inflight' WHERE status IN ('reserved','dispatched') AND deadline_at<=?", now, now);
    // Monetary holds are untouched, even for the reserve-to-dispatch crash gap.
  }
  expireStale(now: number): void { this.tx(() => this.expireStaleInside(now)); }
  nextDeadline(): number | null {
    return this.one<{ deadline: number | null }>("SELECT MIN(deadline_at) AS deadline FROM requests WHERE status IN ('reserved','dispatched')")!.deadline;
  }

  allowance(userId: string, policy: Policy, now: number) {
    const u = this.user(userId);
    const userHeld = BigInt(u.held);
    const daySpent = this.spent(`user-day:${u.id}`, `${day(now)}:${u.day_generation}`);
    const monthSpent = this.spent(`user-month:${u.id}`, `${month(now)}:${u.month_generation}`);
    const dailyReset = Date.parse(`${day(now)}T00:00:00.000Z`) + 86_400_000;
    const date = new Date(now);
    const monthlyReset = Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1);
    const window = (limit: number, spent: bigint, resetsAt: number) => ({ limit_microusd: String(limit), spent_microusd: String(spent), held_microusd: String(userHeld), remaining_microusd: String(nonnegative(BigInt(limit) - spent - userHeld)), resets_at: resetsAt });
    const c = this.control();
    return { user_id: u.id, mode: policy.mode, tools: Object.keys(policy.tools), capabilities: { protocol_version: 1, max_tools: MAX_TOOLS, max_tool_calls_per_response: MAX_TOOL_CALLS, max_messages: 128, max_body_bytes: policy.max_body_bytes }, reset_timezone: "UTC", daily: window(u.daily_limit, daySpent, dailyReset), monthly: window(u.monthly_limit, monthSpent, monthlyReset),
      gateway_paused: Boolean(c.paused || c.breached), models: Object.entries(policy.models).map(([id, m]) => ({ id, min_output_tokens: m.provider === "openai_responses" ? 16 : 1, max_output_tokens: m.max_output_tokens })) };
  }

  status(policy: Policy, now: number) {
    const c = this.control();
    const monthSpent = this.spent("global-month", month(now));
    const globalHeld = BigInt(c.held);
    const window = (limit: number, spent: bigint) => ({ limit_microusd: String(limit), spent_microusd: String(spent), held_microusd: c.held, remaining_microusd: String(nonnegative(BigInt(limit) - spent - globalHeld)) });
    return { paused: Boolean(c.paused), breached: Boolean(c.breached), reset_timezone: "UTC", monthly: window(policy.global_monthly_microusd, monthSpent), lifetime: window(policy.global_lifetime_microusd, BigInt(c.lifetime_spent)),
      users: this.one<{ count: number }>("SELECT COUNT(*) AS count FROM users")!.count,
      requests: this.all<{ status: string; count: number }>("SELECT status,COUNT(*) AS count FROM requests GROUP BY status"),
      audit: this.all<{ at: number; action: string; target_id: string | null; values_json: string }>("SELECT at,action,target_id,values_json FROM audit ORDER BY id DESC LIMIT 100").map(({ values_json, ...a }) => ({ ...a, values: JSON.parse(values_json) as unknown })) };
  }

  setLimits(userId: string, daily: number, monthly: number, now: number): void {
    this.tx(() => {
      this.user(userId);
      this.exec("UPDATE users SET daily_limit=?,monthly_limit=? WHERE id=?", daily, monthly, userId);
      this.audit(now, "limits_changed", userId, { daily_microusd: daily, monthly_microusd: monthly });
    });
  }

  resetUser(userId: string, daily: boolean, monthly: boolean, now: number): void {
    this.tx(() => {
      this.user(userId);
      this.exec("UPDATE users SET day_generation=day_generation+?,month_generation=month_generation+? WHERE id=?", Number(daily), Number(monthly), userId);
      // Historical buckets, current holds and global lifetime spend survive.
      this.audit(now, "allowance_reset", userId, { daily, monthly });
    });
  }

  revoke(kind: "users" | "sessions" | "invites", id: string, now: number): void {
    this.tx(() => {
      if (!this.one(`SELECT id FROM ${kind} WHERE id=?`, id)) throw new GatewayError(404, "not_found");
      this.exec(`UPDATE ${kind} SET ${kind === "users" ? "disabled" : "revoked"}=1 WHERE id=?`, id);
      if (kind === "users") this.exec("UPDATE sessions SET revoked=1 WHERE user_id=?", id);
      this.audit(now, `${kind}_revoked`, id);
    });
  }

  setPaused(paused: boolean, now: number): void {
    this.tx(() => {
      if (!paused && this.control().breached) throw new GatewayError(409, "provider_breach_requires_repair");
      this.exec("UPDATE control SET paused=? WHERE id=1", Number(paused));
      this.audit(now, "pause_changed", null, { paused });
    });
  }
}

export function publicRequest(row: RequestRow) {
  return { request_id: row.id, model: row.model, status: row.status, created_at: row.created_at, deadline_at: row.deadline_at,
    reserved_microusd: row.reserve_microusd, charged_microusd: row.charged_microusd, error: row.error_code,
    usage: row.input_tokens === null ? null : { input_tokens: row.input_tokens, output_tokens: row.output_tokens, ...(row.usage_details_json ? JSON.parse(row.usage_details_json) : {}) } };
}
