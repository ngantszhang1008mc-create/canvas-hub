# Operator guide

This version includes OpenAI Responses and New API/DeepSeek adapters plus an
explicit offline mock mode. It can be developed and tested without a Cloudflare account, provider
account, or paid API call. These instructions describe a future deployment path;
this change does not deploy or create accounts.

## Local development

Use Node.js 24 and npm. From this directory's parent (`gateway/`):

```bash
npm ci
npm run check
npm run build
```

`check` runs TypeScript and the offline functional tests. `build` invokes
Wrangler with `--dry-run`; it bundles the Worker without deploying it. Tests use
synthetic credentials and course-free messages, isolated storage, and mock
provider behavior. They never need an upstream key.

`npm run dev` explicitly selects Wrangler's `mock` environment. It never selects
the real adapter. For that local HTTP server, create a disposable admin credential without printing
it or putting it in shell history:

```bash
python3 - <<'PY'
import os
import secrets

fd = os.open('.dev.vars', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write('ADMIN_SECRET=' + secrets.token_urlsafe(32) + '\n')
PY
npm run dev
```

Creation intentionally fails if `.dev.vars` already exists. Reuse or deliberately
rotate your existing local credential instead of silently overwriting it. The
development server binds only to `127.0.0.1`; use its printed port. Keep its
storage and `.dev.vars` out of version control. Removing local Wrangler storage
also removes local accounts, spending records, and idempotency protection.

All four budget fields in the checked-in policies are zero. For a local mock
exercise, explicitly set positive synthetic values in `wrangler.jsonc` under
`env.mock.vars.POLICY` before starting the server; keep them out of production.
The default `vars.POLICY` is production and cannot run without `OPENAI_API_KEY`.
`policy.example.json` is the standalone mock example and
`policy.production.example.json` is the reviewed Luna example.
`policy.new-api.example.json` is the New API/DeepSeek example; explicitly copy it
into the intended `POLICY` binding to select that adapter. Wrangler does not
automatically read these files. A New API-only policy requires `NEW_API_BASE_URL`
and its selected relay-key configuration, not `OPENAI_API_KEY`. Tests supply explicit mock configuration and
mocked production upstream responses, never a live key.

## Policy

The `POLICY` binding is server-owned configuration. Amounts are integer micro-USD
(`1,000,000` units = one USD). Rates are integer micro-USD per million tokens.
Choose actual allowance amounts and funding limits before any real rollout.
Mock model rates are synthetic; production model rates must meet the reviewed
provider floors or use more conservative operator-selected values.

| Fields | Meaning |
| --- | --- |
| `mode` | Required `mock` or `production`; mixed provider modes are rejected |
| `user_daily_microusd`, `user_monthly_microusd` | Default new-account limits |
| `global_monthly_microusd`, `global_lifetime_microusd` | Owner limits across every account |
| `session_ttl_seconds`, `invite_ttl_max_seconds` | Session validity and maximum invite lifetime |
| `request_timeout_ms` | Bounded execution deadline |
| `max_body_bytes`, `max_stream_bytes` | Request and streamed-response limits |
| `user_concurrency`, `global_concurrency` | Maximum admitted calls still in flight |
| `user_requests_per_minute` | Per-account authenticated request rate, including allowance/status reads |
| `ingress_per_minute`, `global_ingress_per_minute` | Per-peer and global ingress bounds |
| `auth_attempts_per_minute`, `admin_attempts_per_minute` | Authentication/administration gates |
| `max_users` | Maximum invited accounts in this ledger |
| `models` | Approved aliases, adapter, token bounds, and server-owned prices |
| `tools` | Approved function definitions; no execution occurs here |

Rate counters use fixed minute windows. Responses rejected by a saturated global
gate do not create new per-peer rows. All ingress, including administration,
shares the global request gate; production edge protection remains necessary.

Daily periods start at `00:00 UTC`. Monthly periods start at `00:00 UTC` on the
first calendar day. The lifetime owner counter never resets with a calendar
window or user adjustment. Resetting a user allowance is an explicit grant of
additional capacity inside the owner's remaining ceiling, not a refund from the
provider. Lowering a limit below already-used capacity stops further admission;
it cannot undo existing charges.

An unresolved request keeps its full hold across daily/monthly boundaries and
manual resets. This deliberately favors preserving the owner's bound over
automatically restoring an uncertain allowance. Authoritative completion can
settle it; this version has no administrative button that refunds uncertain
holds. Do not treat a user reset as such a refund.

OpenAI production model entries use `provider:"openai_responses"`,
`native_model:"gpt-6-luna"`, and the reviewed full `max_input_tokens:1050000` bound.
`pricing` contains `unit:"microusd_per_million_tokens"`,
`long_context_threshold_tokens:272000`, and `short`/`long` rate sets. Each rate set
has `input`, `cached_input`, `cache_write_30m`, `cache_write_12h`, and `output`.
The production example supplies the complete shape; unsupported models, reduced
input bounds, or rates below the reviewed floors fail configuration validation.
The tool catalog can contain up to 128 definitions. The client discovers names
and versioned limits through `GET /v1/allowance`; a larger catalog or request is
rejected rather than silently shortened. Historical call/result records do not
need to remain in the tools selected for the current turn.

OpenAI reservations cover the full input bound at the most expensive category plus the
accepted output bound at the highest output rate. The 12-hour cache-write rate is
included conservatively, even though requests explicitly select 30-minute writes.
Reported 12-hour writes are fully priced and trigger the breach circuit breaker.
A request retains its price snapshot and accounting periods through settlement,
including across policy changes and UTC boundaries. See the
[adapter notes](openai-provider.md) for exact usage arithmetic.

New API entries use `provider:"new_api"`, `native_model:"deepseek-flash"`, the
full `max_input_tokens:1048576` bound, and a gateway output cap from 1 to 128,000.
Input/output rate floors are respectively 300,000 and 1,200,000 micro-USD per
million tokens. Success consumes the full reservation without reporting token
usage; at the example 1,024 output cap that is $0.315802 per model round.
Failures retain uncertain holds. The immutable accounting mode is stored with
the request, so later policy changes cannot convert it to usage-based charging.
Read the [New API guide](new-api-provider.md) before configuring paid traffic;
its relay/proxy restrictions are prerequisites for the conditional cost bound,
not proof of a strict upstream invoice cap.

## Administration

Administrative calls use `Authorization: Bearer <ADMIN_SECRET>` and JSON bodies.
Keep the credential in an operator tool, not the Canvas Hub client. Avoid putting
secrets in command arguments, URLs, or committed request fixtures. Use a protected
HTTP client configuration or an in-memory script that reads the local secret.

| Method and path | JSON body / purpose |
| --- | --- |
| `GET /admin/status` | No body; owner monthly/lifetime totals, pause/breach state, counts, latest 100 audit entries |
| `POST /admin/invites` | `{"expires_in_seconds":3600}`; optional `user_id` for recovery, or `relay_key_ref` to prebind a new account in per-user mode; the two options are mutually exclusive |
| `POST /admin/invites/:id/revoke` | `{}`; invalidate an unused invitation |
| `POST /admin/users/:id/limits` | `{"daily_microusd":<integer>,"monthly_microusd":<integer>}` |
| `POST /admin/users/:id/reset` | `{"daily":true,"monthly":false}`; both booleans required, at least one true |
| `POST /admin/users/:id/revoke` | `{}`; permanently disable the account and revoke its sessions |
| `GET /admin/users/:id/new-api` | Inspect local identity mode, binding reference and configuration state; no upstream request |
| `POST /admin/users/:id/new-api` | `{"relay_key_ref":"opaque_reference"}` to bind, or `{"relay_key_ref":null}` to unbind; only in `per_user` mode |
| `POST /admin/sessions/:id/revoke` | `{}`; revoke one session |
| `POST /admin/pause` | `{"paused":true}` or `{"paused":false}` |

Invite creation returns HTTP `201` with `invite_id`, the one-time `invite` token,
and `expires_at` in Unix milliseconds. Revocation, limit/reset changes, and pause
changes return `{"ok":true}`. Recovery invite redemption revokes the account's
earlier sessions. No mutation accepts arbitrary operator notes or prompt data.
An overrun or invalid authoritative usage trips a persistent breach flag; the
pause endpoint cannot clear it. Repair and reconcile the cause before a reviewed
code/operational recovery.

New API identity is shared by default through the `NEW_API_RELAY_KEY` secret.
For individual New API user quotas/reports, set
`NEW_API_IDENTITY_MODE="per_user"`, keep a `NEW_API_RELAY_KEYS` JSON secret map,
and prebind a new account through its invitation or bind an existing account
through the route above. Omit the shared key in
this mode. Missing or removed references cannot dispatch and never fall back.
Accounts/tokens are preprovisioned manually; keep both New API account logins
and relay tokens private to the operator. Canvas balances and invitations are
not synchronized from New API. See the [identity workflow](new-api-provider.md#server-configuration-and-identities).

Deliver issued invitations through an already authorized private channel. No
email or messaging service is built in. Expired sessions should receive a grant
for the same gateway account, preserving quota and retry history. Creating a new
account for the same person grants a distinct allowance.

Revocation or pausing stops future admissions. A provider call already sent may
continue to incur its reserved cost; do not reset global spending in response.
Use request status to distinguish settled usage from a conservative uncertain
charge. Do not manually delete uncertain requests or edit totals in SQL.

## Preparing a deployment

1. Review the policy, invitation process, data-transit disclosure, and
   [security boundaries](security.md). Keep the first rollout small.
2. Use a dedicated Worker and preserve its Durable Object class, namespace, and
   fixed ledger name across releases. Keep migrations additive. A new namespace
   is a fresh spending ledger, not a harmless deployment rename.
3. Add `ADMIN_SECRET` and the chosen adapter's key secrets with Wrangler's secret
   mechanism or the Cloudflare dashboard: `OPENAI_API_KEY` for OpenAI, or
   `NEW_API_RELAY_KEY` / `NEW_API_RELAY_KEYS` for New API's chosen identity mode.
   Provider credentials reach the adapter only this way;
   never add it to `vars`, source, a client bundle, or a public repository.
4. Protect operator access with an appropriate identity boundary and MFA. Disable
   request body/header logging and any tracing that captures conversation data.
   Set edge abuse controls and monitor infrastructure costs independently.
5. Run focused tests and the dry-run build after relevant changes. The real adapter
   has only been exercised with mocked upstream HTTP/SSE; conduct a separately
   approved staging exercise before issuing funded invitations. Explicit
   production policy, secrets, and positive user/global budgets are all required.
6. Deploy only after a separate deployment decision. Verify the fixed ledger,
   configured budgets, TLS, secret bindings, and restricted operator access before
   issuing real invitations. New API also requires the pinned relay review,
   effective `RetryTimes:"0"`, channel `http_protocol:"http1"`, no intermediary
   retries or request overrides, and the revised client disclosure.

For an authorized future deployment, typical secret/deployment commands are
`npx wrangler secret put ADMIN_SECRET`, `npx wrangler secret put OPENAI_API_KEY`,
and `npx wrangler deploy`. Do not run them
as part of local development. See Cloudflare's
[secret instructions](https://developers.cloudflare.com/workers/configuration/secrets/)
for environment-specific bindings.

## Recovery

The SQLite ledger adds nullable `pricing_json` and `usage_details_json`, an
`accounting_mode` defaulting to `authoritative_usage` on old request rows, and a
nullable `new_api_key_ref` on user rows on startup within a transaction.
These additive migrations preserve existing
accounts, holds, totals, and request keys; old requests retain their stored
input/output rates. Keep the existing `v1` Durable Object class migration and
singleton identity. No new namespace or manual SQL migration is needed.
An additional nullable `new_api_key_ref` on invitations records preassigned
bindings. Recovery invitations do not overwrite existing user bindings.

Pause admissions before investigating accounting discrepancies, changing provider
cost assumptions, or restoring data. A restore to an older ledger can forget
both charges and replay protection. Reconcile all provider usage since the restore
point, preserve conservative liabilities, then make an explicit resume decision.
Do not restore availability by replacing the singleton name or deleting the DO.

There is no automatic provider-billing reconciliation or destructive accounting
cleanup in this version. A support workflow for disputed conservative charges,
backups/restores, account deletion, and a retention schedule needs a separate
review before production use.
