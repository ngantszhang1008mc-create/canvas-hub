# Security and cost boundaries

This gateway is a small, invitation-only service for a subsidized model trial.
It is not a hosted copy of Canvas Hub. Collection, the Canvas database, browser
profiles, file access, and tool execution stay in the local Python process.

## Information crossing the boundary

The selected conversation, system instructions, function descriptions, and any
course excerpts included in messages or tool results pass through the gateway.
The production OpenAI Responses adapter also sends them to OpenAI. A local
client must disclose that transfer before first use and let the user choose
which context to include. An allowance is not a promise that processing is local.

Canvas passwords, Canvas access tokens, SSO cookies, browser storage, browser
profiles, the local app access token, and unrelated files must never be sent.
The API has no fields for these values and does not accept arbitrary provider
URLs or headers. This is a protocol boundary, not a content classifier: a user
can still paste a secret into ordinary message text. The local integration must
construct messages deliberately rather than serialize application state.

The gateway does not intentionally persist conversation bodies, generated text,
tool arguments/results, raw authorization tokens, or provider keys in its
ledger. Its database contains account/session metadata, quota settings, request
fingerprints and status, usage totals, and administrative audit metadata.
Operators must also keep request-body/header logging disabled in Cloudflare,
reverse proxies, tracing integrations, crash reporters, and provider tooling.
Ordinary infrastructure access logs may still reveal traffic timing and network
metadata; provider retention depends on the selected provider's configuration.

## Cost invariants

1. A server-approved model and server-owned prices determine the reservation.
   The client cannot report its own bill, price, remaining allowance, or user ID.
2. A transaction admits a request only when both user budgets and global budgets
   can cover the full bounded reservation. Concurrency and request-rate limits
   are separate controls; a zero-priced or cheap call is not unlimited traffic.
3. The reservation becomes durable before any potentially billable provider
   dispatch. Reservation and settlement use the same authoritative ledger.
4. Only a trusted provider adapter may report definitive usage or proof of no
   dispatch. A client closing its stream is never evidence of zero provider cost.
5. A timeout, missing final usage, interrupted stream, or lost worker must retain
   the worst-case liability. Conservative accounting can consume more allowance
   than was actually billed; it must not consume less on an ambiguous outcome.
6. A quota reset does not remove owner spending or idempotency history. Existing
   calls settle against the accounting windows captured when admitted.
7. Duplicate attempts with the same user/key cannot dispatch a second model call.
   Streaming output is not stored for replay. A lost response may be unrecoverable
   even when the ledger can confirm its charge.

The model budget does **not** cap Cloudflare requests, storage, CPU, networking,
taxes, currency conversion, or charges made with the same provider account outside
this gateway. It depends on correct server model limits/rates and a provider
honoring those limits. Before the production adapter receives paid traffic, use a dedicated provider
project/key, configure the provider's available billing controls, and reconcile
the ledger with provider billing. Treat invoice discrepancies as an incident.

## Authentication and abuse

An invitation is a one-time bearer capability, not verified institutional
identity. Possession permits redemption. Do not publish invite tokens in issues,
URLs, screenshots, analytics, or shared logs. Invitation issuance must be limited
to the intended people: one person with several independently issued accounts can
otherwise receive several allowances.

Session tokens are bearer credentials too. The local client should keep
them in its private credential storage, exclude them from backups, and use HTTPS
for a remote gateway. Session expiry and revocation prevent future admission;
they cannot undo provider work already dispatched. Recover access through an
operator-controlled process, not by trusting a client-provided account name.

The initial admin secret is a deployment control credential. Keep it outside the
app and browser, restrict access, and rotate it if exposed. A production service
should put the administration surface behind a separately verified identity/MFA
boundary and record individual operator identities. Local development tokens are
not production credentials.

Rate and concurrency limits reduce accidental or authenticated abuse. They do
not replace edge denial-of-service protection. A single global ledger is useful
for the small invitation rollout because every reservation sees the same owner
budget. It is also a throughput and availability bottleneck. Do not split it into
independent per-user ledgers without a new, tested global reservation protocol.

## Tool and application boundaries

Only function definitions and streaming function-call data cross the gateway.
It must never execute a function, fetch a Canvas page, follow a model-generated
URL, access a local file, or run provider-hosted tools. The local application must
wait for a complete successful response, validate full JSON arguments against
its own tool schema, check permissions, and retain existing confirmation gates
before dispatching a tool. Partial streamed arguments are untrusted text.

When the free allowance is exhausted, the local client must stop or ask the user
to choose an existing BYOK setup explicitly. It must not silently retry against
another paid key, change models/providers, or expand the owner's spending limit.

## Operations and remaining production work

- Review model pricing, context/output bounds, reasoning-token billing, caching,
  and all chargeable extras for each real adapter. Bind each request to the price
  snapshot used at reservation; never calculate settlement from client data.
  The included adapter fixes the endpoint, Luna model, standard processing and
  reasoning modes, and cache lifetime; see [its supported surface](openai-provider.md).
- Preserve the Durable Object namespace and fixed ledger identity across releases.
  Changing identity or restoring old database state can forget spend and replay
  protection. Pause admission during recovery, reconcile provider totals, and
  only resume from a ledger that includes all already-incurred liabilities.
- Review retention and deletion requirements before inviting real users. Account
  deletion cannot simply delete accounting/idempotency rows while credentials or
  retries remain valid. Archive or aggregate only under a designed policy.
- Test provider cancellation, missing usage, and real streaming termination in a
  separate approved staging exercise before enabling paid traffic. Offline mocks
  verify application invariants; they do not certify a provider's billing behavior.
- Add availability monitoring based on counts/status/latency only. Never attach
  prompt excerpts, tool data, cookies, tokens, or provider response bodies to
  error reports.

Cloudflare's [SQLite storage API](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)
documents synchronous transactions and durable output gates. Its
[alarm API](https://developers.cloudflare.com/durable-objects/api/alarms/) permits
repeated alarm delivery, so recovery must be idempotent. See also the
[secrets guidance](https://developers.cloudflare.com/workers/configuration/secrets/)
and [Durable Object limits](https://developers.cloudflare.com/durable-objects/platform/limits/)
when preparing a deployment.
