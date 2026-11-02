# Operator guide

This version is mock-only. It can be developed and tested without a Cloudflare
account, provider account, or paid API call. These instructions describe a future
deployment path; this change does not deploy or create accounts.

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

For the local HTTP server, create a disposable admin credential without printing
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

All four budget fields in the checked-in `POLICY` are zero. For a local mock
exercise, explicitly set positive synthetic values in `wrangler.jsonc`'s policy
before starting the server; keep them out of a production configuration. The
standalone `policy.example.json` shows the same schema but is not automatically
loaded by Wrangler. Tests supply their own positive synthetic budgets.

## Policy

The `POLICY` binding is server-owned configuration. Amounts are integer micro-USD
(`1,000,000` units = one USD). Rates are integer micro-USD per million tokens.
The values in the sample are synthetic; choose actual allowance amounts and
funding limits before any real rollout.

| Fields | Meaning |
| --- | --- |
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

Model reservations use the configured full input bound plus the accepted output
bound at server prices. Do not tune this bound downward merely to make more
requests fit: a future real adapter must prove the provider cannot accept a more
expensive request. A request retains its reservation-time price and accounting
periods through settlement, including across policy changes and UTC boundaries.

## Administration

Administrative calls use `Authorization: Bearer <ADMIN_SECRET>` and JSON bodies.
Keep the credential in an operator tool, not the Canvas Hub client. Avoid putting
secrets in command arguments, URLs, or committed request fixtures. Use a protected
HTTP client configuration or an in-memory script that reads the local secret.

| Method and path | JSON body / purpose |
| --- | --- |
| `GET /admin/status` | No body; owner monthly/lifetime totals, pause/breach state, counts, latest 100 audit entries |
| `POST /admin/invites` | `{"expires_in_seconds":3600}`; optional `user_id` issues a recovery invite for that existing account |
| `POST /admin/invites/:id/revoke` | `{}`; invalidate an unused invitation |
| `POST /admin/users/:id/limits` | `{"daily_microusd":<integer>,"monthly_microusd":<integer>}` |
| `POST /admin/users/:id/reset` | `{"daily":true,"monthly":false}`; both booleans required, at least one true |
| `POST /admin/users/:id/revoke` | `{}`; permanently disable the account and revoke its sessions |
| `POST /admin/sessions/:id/revoke` | `{}`; revoke one session |
| `POST /admin/pause` | `{"paused":true}` or `{"paused":false}` |

Invite creation returns HTTP `201` with `invite_id`, the one-time `invite` token,
and `expires_at` in Unix milliseconds. Revocation, limit/reset changes, and pause
changes return `{"ok":true}`. Recovery invite redemption revokes the account's
earlier sessions. No mutation accepts arbitrary operator notes or prompt data.
An overrun or invalid authoritative usage trips a persistent breach flag; the
pause endpoint cannot clear it. Repair and reconcile the cause before a reviewed
code/operational recovery.

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
3. Add the admin secret with Wrangler's secret mechanism or the Cloudflare
   dashboard. A future paid adapter must receive its provider key the same way;
   never add it to `vars`, source, a client bundle, or a public repository.
4. Protect operator access with an appropriate identity boundary and MFA. Disable
   request body/header logging and any tracing that captures conversation data.
   Set edge abuse controls and monitor infrastructure costs independently.
5. Run the documented tests and dry-run build. Enabling a real provider requires
   its separately reviewed adapter and approved staging exercise first. A provider
   secret alone cannot enable a paid model in this mock-only build.
6. Deploy only after a separate deployment decision. Verify the fixed ledger,
   configured budgets, TLS, secret bindings, and restricted operator access before
   issuing real invitations.

For an authorized future deployment, typical secret/deployment commands are
`npx wrangler secret put ADMIN_SECRET` and `npx wrangler deploy`. Do not run them
as part of local development. See Cloudflare's
[secret instructions](https://developers.cloudflare.com/workers/configuration/secrets/)
for environment-specific bindings.

## Recovery

Pause admissions before investigating accounting discrepancies, changing provider
cost assumptions, or restoring data. A restore to an older ledger can forget
both charges and replay protection. Reconcile all provider usage since the restore
point, preserve conservative liabilities, then make an explicit resume decision.
Do not restore availability by replacing the singleton name or deleting the DO.

There is no automatic provider-billing reconciliation or destructive accounting
cleanup in this version. A support workflow for disputed conservative charges,
backups/restores, account deletion, and a retention schedule needs a separate
review before production use.
