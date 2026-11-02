# Canvas Hub allowance gateway

An isolated Cloudflare Worker for trying AI with a bounded, operator-funded
allowance. It handles invitations, sessions, an approved model contract, streaming,
and a transactional usage ledger. Canvas collection, local databases, browser
sessions, and tool execution remain in the existing local application.

This first implementation uses an offline mock provider. It performs no model
network calls and requires no real provider key. It is a backend foundation, not
an enabled free tier in the current Canvas Hub UI. No service has been deployed.

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
- Offline functional tests against the Workers runtime and its SQLite storage.

Amounts and model rates in the example policy are test configuration. They are
not a settled monetary entitlement or a claim about any provider's current price.

## Reading and integration

- [Operator guide](docs/operations.md): local commands, configuration,
  administrative controls, and the deployment path.
- [Client contract](docs/client-contract.md): transport, streaming, retries,
  quota handling, and the future Python adapter seam.
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
provider adapter (offline mock in this version)
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

## Real provider readiness

The adapter seam is intended to support an explicitly reviewed OpenAI Responses
or Luna implementation later. Before enabling one, define an enforceable input
bound, enforce the output bound (including billed reasoning), price every
chargeable feature, parse authoritative terminal usage, and test ambiguous
cancellation/failure. Keep provider credentials in server-side secret bindings.
Do not turn the mock adapter into an arbitrary upstream URL proxy.

Cloudflare/infrastructure spending is outside the model allowance ceiling. A
paid rollout also needs operator access controls, provider billing safeguards,
retention/recovery policy, monitoring, and a separately verified staging test.
