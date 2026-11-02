# Canvas Hub allowance gateway

An isolated Cloudflare Worker for trying AI with a bounded, operator-funded
allowance. It handles invitations, sessions, an approved model contract, streaming,
and a transactional usage ledger. Canvas collection, local databases, browser
sessions, and tool execution remain in the existing local application.

The production adapter implements OpenAI Responses for the approved `gpt-6-luna`
model. Development uses an explicit offline mock mode. Production defaults have
zero allowance and require server-side configuration and secrets before serving
requests. No live model call or deployment was performed to validate this change.
The local app includes an opt-in **Allowance gateway** provider with invitation
redemption, allowance display, and the same local tool permissions as other setups.

## What is implemented

- One-time invitation redemption, expiring/revocable bearer sessions, and
  authenticated administrative controls.
- Configurable per-user daily/monthly limits, UTC reset windows, manual user
  resets and limit adjustments, global monthly and lifetime ceilings.
- Atomic worst-case reservations in one SQLite Durable Object, authoritative
  settlement, conservative treatment of uncertain costs, and a pause mechanism.
- Per-user idempotency across retries and session changes, request status,
  request-rate limits, and user/global concurrency limits.
- Streaming text and function-call fragments, with functions executed only by
  the local client. No automatic provider retry or paid fallback.
- A bounded OpenAI HTTP/SSE adapter with cached-input/cache-write accounting,
  long-context price snapshots, and output totals that include reasoning once.
- Offline functional tests against the Workers runtime and its SQLite storage.

Allowance defaults are zero and do not establish a monetary entitlement. Mock
rates are synthetic; the production example records reviewed provider price
floors, which operators must review before enabling paid traffic.

## Reading and integration

- [Operator guide](docs/operations.md): local commands, configuration,
  administrative controls, and the deployment path.
- [Client contract](docs/client-contract.md): transport, streaming, retries,
  quota handling, and the Python adapter seam.
- [Minimal integration handoff](docs/integration-handoff.md): the exact local
  provider mapping and explicit mock setup for the combined test version.
- [OpenAI adapter](docs/openai-provider.md): supported requests, authoritative
  usage, conservative reservation, and official references.
- [Security and cost boundaries](docs/security.md): data transit, accounting
  assumptions, tool permissions, and production limitations.

The service deliberately has no Canvas login, signup emails, payment collection,
subscription billing, or access to the user's local filesystem. BYOK remains in
the local application and requires an explicit user choice.

## Architecture

```text
Canvas Hub local Python process
  | session bearer + selected messages / approved tool names
  v
Cloudflare Worker
  v
one fixed SQLite Durable Object
  | validate auth/model, atomically reserve all budgets
  v
provider adapter (explicit mock or approved OpenAI Responses)
  | text / function-call fragments + authoritative final usage
  v
settlement + streamed result to local process
  |
local validation / permissions / tool dispatch
```

One ledger keeps user and owner limits in the same atomic transaction. That is
a useful tradeoff for a small invitation rollout: consistency is straightforward,
while throughput is bounded by one object. Sharding budgets is a separate design
problem; creating one independent global budget per user would multiply owner
exposure.

Cloudflare currently recommends SQLite-backed Durable Objects for new storage;
the [storage API](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
provides synchronous transactions. Tests exercise the runtime locally, following
the [Workers test guidance](https://developers.cloudflare.com/workers/testing/vitest-integration/).

## Supported provider and remaining rollout work

The real adapter is limited to global Standard Responses requests for Luna,
text/function input, and fixed reasoning effort `none`. It reserves the reviewed
full context bound and enforced output cap; the client cannot supply native
options or an upstream URL. Keep `OPENAI_API_KEY` in a server-side secret binding.
Upstream HTTP/SSE tests are mocked with unmatched network disabled.

Cloudflare/infrastructure spending is outside the model allowance ceiling. A
paid rollout also needs operator access controls, provider billing safeguards,
retention/recovery policy, monitoring, app integration, and a separately approved
staging test against a real provider account.
