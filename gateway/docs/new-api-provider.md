# New API relay integration

Canvas Hub keeps its existing invitation, allowance and assistant interface.
Requests follow this path:

```text
Canvas Hub local process -- gateway session --> Canvas allowance gateway
  -- server-only relay key --> New API -- operator's DeepSeek key --> DeepSeek
```

The gateway reserves the user's daily/monthly allowance and the owner's
monthly/lifetime budgets before contacting New API. New API manages upstream
channels and its own users/tokens. The Canvas ledger remains the source for the
allowance shown in Canvas Hub. New API balances, currency units and usage reports
are not copied into that ledger.

## Which interface controls what

The **Canvas gateway ledger is the sole authority for Canvas allowance**. Its
daily/monthly balances, held amounts and owner ceilings are what the app uses
to admit requests. A positive Canvas balance does not prove that New API has
quota, an enabled channel or a usable upstream account.

| Interface | Controls | Does not change |
| --- | --- | --- |
| New API operator console | DeepSeek channels/keys, New API accounts and relay-token restrictions, its own quota and usage reports | Canvas invitations, sessions, daily/monthly grants, global ceilings or held amounts |
| Canvas gateway admin API | Invitations, per-user relay references, Canvas limits/resets, revocation and pause | New API accounts, upstream balances or provider invoices |
| Canvas Hub Assistant settings | Invitation redemption, own account ID, current binding configuration, Canvas allowance and model reservation amounts | Administrative credentials, relay keys or either system's funding policy |

The New API console is not embedded in Canvas Hub. Adjusting a quota there does
not grant Canvas allowance, and resetting Canvas allowance does not top up New
API. There is no automatic account or two-way balance synchronization.

This implementation has been exercised with synthetic credentials and mocked
upstream responses only. No New API service, account, channel, real key, funded
invitation or public deployment was created by this change.

## Reviewed protocol

The review used New API commit
[`ba5f7367797566175a6cd97a748d430af3602a01`](https://github.com/QuantumNous/new-api/tree/ba5f7367797566175a6cd97a748d430af3602a01)
and DeepSeek's official documentation on 2026-11-03. Pin the deployed New API
version and repeat the compatibility review before upgrading it.

The only supported native model is `deepseek-flash`. The server owns the model
alias, endpoint, tools, prices and limits. The adapter sends one
`POST /v1/chat/completions` with `thinking:{type:"disabled"}`,
`reasoning_effort:"none"`, the accepted `max_tokens`, `stream:true` and
`stream_options:{include_usage:true}`. It does not send undocumented `n` or
`parallel_tool_calls` options. Response validation accepts one choice at index
zero and the exact approved model. No client-supplied options, headers, URLs,
reasoning state or hosted tools are forwarded.

The reviewed full context is 1,048,576 tokens. The native output bound is 393,216
tokens; this gateway accepts at most 128,000 to preserve the current client
contract, with 1,024 in the example policy. Policy uses the peak uncached rates of $0.30 per million
input tokens and $1.20 per million output tokens, or higher operator-selected
rates. Cached or discounted usage does not reduce Canvas allowance charges.
See the official [model limits](https://api-docs.deepseek.com/api/list-models/),
[pricing](https://api-docs.deepseek.com/quick_start/pricing/) and
[Chat Completions contract](https://api-docs.deepseek.com/api/create-chat-completion/).

Text and function fragments are converted to the existing gateway NDJSON
contract. A valid finish reason, `[DONE]` and clean stream termination are all
required. A function can run only after successful gateway completion and the
existing local permission checks. No function runs in New API or the Worker.

## Conservative allowance accounting

New API can synthesize token usage, and a visible response does not prove how
many upstream attempts were billed. The adapter therefore never treats its
reported tokens as authoritative billing. Each request snapshots
`accounting:"full_reservation"` in the ledger when admitted. A complete successful
response consumes the entire reservation and returns `usage:null`; it does not
invent token counts. Optional relay usage is checked only for obvious bounds
violations. The accounting mode cannot be changed by a provider completion or
by a later policy change.

At the example rates and an output cap of 1,024 tokens, each successful model
round consumes **315,802 micro-USD ($0.315802) of Canvas allowance**, even for a
short answer. This is the rounded-up full input-context bound plus the output
cap; it is not an assertion that DeepSeek billed that amount. Every subsequent
tool round reserves and consumes another such amount. An allowance smaller
than the required reservation cannot admit a request.

An HTTP failure, cancellation, timeout, lost response or incomplete stream keeps
the full amount held as `uncertain`. Invalid protocol or a reported bounds
violation also trips the persistent pause/breach control. A user reset does not
refund these holds. The integration provides no automatic billing reconciliation
or administrative refund of uncertain costs.

These controls bound admission in the Canvas ledger under the reviewed upstream
assumptions. **They do not establish a strict New API or DeepSeek invoice cap.**
The gateway cannot prove from a stream that New API honored retries, model
selection, pricing or output limits. Its own quota system is a second control,
not a substitute for those assumptions. Infrastructure costs and direct use of
the upstream account are outside this ledger.

## Server configuration and identities

Copy `policy.new-api.example.json` into the intended deployment's `POLICY`
binding explicitly; Wrangler does not load it automatically. All four budget
defaults are zero. Keep the existing Durable Object namespace, class and
`singleton-v1` identity. `NEW_API_BASE_URL` is an operator-owned HTTPS origin such
as `https://relay.example.invalid`, with no path, credentials, query or fragment.
The adapter appends `/v1/chat/completions` and rejects redirects.

Choose one identity mode:

| Mode | Server bindings | Meaning |
| --- | --- | --- |
| Shared (default) | `NEW_API_RELAY_KEY` secret; optional `NEW_API_IDENTITY_MODE="shared"` | One ordinary New API service user's relay token funds all Canvas accounts. New API sees aggregate service usage; Canvas keeps individual allowance records. |
| Per user | `NEW_API_IDENTITY_MODE="per_user"`, `NEW_API_RELAY_KEYS` JSON secret | Each Canvas account is manually bound to a reference resolving to an ordinary New API user's relay token. |

In per-user mode, `NEW_API_RELAY_KEYS` has the shape
`{"canvas_user_a":"<relay-token>","canvas_user_b":"<relay-token>"}`. These are
schematic placeholders, not working credentials. References contain 1–64
letters, digits, `_` or `-`; the map supports up to 1,000 entries. Store the
entire map using the server's secret mechanism. Do not set `NEW_API_RELAY_KEY`
in per-user mode, even to an empty value; the mixed configuration fails closed.
The shared mode likewise rejects a per-user key map.

For per-user operation:

1. In New API, preprovision an ordinary account and a restricted relay token for
   each intended Canvas identity. Keep both its login credentials and its relay
   token under operator control. Use distinct accounts/tokens when individual
   New API quotas, revocations and reports are required.
2. Add each token to `NEW_API_RELAY_KEYS` under its opaque reference. Authenticate
   to the Canvas gateway with `ADMIN_SECRET`, then create a new-account invite
   with `POST /admin/invites` and
   `{"expires_in_seconds":3600,"relay_key_ref":"canvas_user_a"}`. The reference
   is stored server-side. Redemption creates the Canvas user and assigns the
   reference in the same transaction; the client receives only its Canvas
   session and account ID. A credential removed after invite creation leaves
   that user with a missing-credential state and cannot cause a relay dispatch.
3. For an existing Canvas account, obtain its own `user_id` from Assistant
   settings or the admin audit. Authenticate with `ADMIN_SECRET` and call
   `POST /admin/users/:user_id/new-api` with
   `{"relay_key_ref":"canvas_user_a"}`. This records only the reference and an
   audit entry, and returns `{"ok":true}`. It accepts neither token values nor
   a client-provided New API user ID.
   `GET` on the same path reports the reference and its current local
   configuration state to the administrator. `POST` with
   `{"relay_key_ref":null}` removes the binding. Recovery invitations accept
   `user_id` but reject `relay_key_ref`; recovery preserves the latest binding,
   including any later explicit rebind or removal.
4. Continue using Canvas's existing limit and invitation controls. New API's
   user/token disable and quota checks additionally apply when the bound token
   reaches the relay. A New API rejection occurs after gateway dispatch and
   conservatively retains the reservation; Canvas does not query that quota in
   advance or show it as the Canvas balance.

An unbound account, or a reference whose key has been removed, cannot dispatch a
New API call. There is no fallback to a shared key. Binding changes affect future
dispatch; work already sent may still incur cost. Revoke the gateway session/user
or pause admissions when immediate prevention of new local admissions is needed.
An explicitly empty per-user secret map (`{}`) keeps administrative inspection
and allowance reads available while all per-user relay dispatch is unavailable.
Missing or malformed secret bindings still fail configuration validation.

Allowance metadata identifies its source as `canvas_gateway`. For New API models,
it includes the shared/per-user identity mode, a configuration-only binding state,
and reservation amounts at the model's minimum and maximum output caps. No relay
reference, token or New API account identifier is returned to the app. These
reads make no New API or DeepSeek request. A configured binding has not been
tested for live account status or quota; a separate upstream rejection can still
retain a conservative hold after dispatch. The app refreshes these states when
checking allowance and before generation; saved catalog data is not a live
binding check.

No New API management credential is used by the runtime or sent to the client.
No user provisioning, login, balance conversion, quota synchronization or SSO is
implemented. New API's reviewed token-creation handler uses the authenticated
account as token owner; an admin-supplied `user_id` is not a safe way to mint
another user's relay token. See its
[token creation handler](https://github.com/QuantumNous/new-api/blob/ba5f7367797566175a6cd97a748d430af3602a01/controller/token.go#L350).

## Required relay controls before paid staging

- Use dedicated DeepSeek channels and keys owned by the operator. Several keys
  may share one upstream account and balance; key count does not multiply funds.
  A separate channel per key is a straightforward reviewed configuration. Any
  multi-key rotation setup still needs the same one-attempt requirement.
- Set New API's effective request policy `RetryTimes` to the string `"0"`.
  Confirm it on the pinned deployment; defaults or environment assumptions are
  insufficient. Set each selected channel's `http_protocol` to `"http1"` to
  avoid the reviewed lower transport's HTTP/2 retry path. Review
  [relay attempts](https://github.com/QuantumNous/new-api/blob/ba5f7367797566175a6cd97a748d430af3602a01/controller/relay.go#L177),
  [channel settings](https://github.com/QuantumNous/new-api/blob/ba5f7367797566175a6cd97a748d430af3602a01/relaykit/dto/channel_settings.go#L29)
  and the [transport path](https://github.com/QuantumNous/new-api/blob/ba5f7367797566175a6cd97a748d430af3602a01/relay/channel/api_request.go#L457).
- Disable retry/failover in every reverse proxy or intermediary. Keep model
  mapping, parameter/header overrides, billable plugins, hosted tools and
  transformations that change the approved request disabled. Preserve the
  DeepSeek endpoint, model, thinking setting and output cap. A change to any of
  these assumptions requires a renewed accounting review.
- Restrict each relay token to the reviewed model/group and gateway network
  path. Do not give Canvas users relay keys or New API account logins: either
  can allow direct requests or token creation that bypasses the Canvas ledger.
  Restrict New API administration separately and keep all management credentials
  outside Canvas clients. Do not log prompt bodies, tool data or auth headers.
- Review actual provider prices/bounds, funding, per-user and global budgets,
  secret storage, retention and recovery. Perform a separately authorized
  staging exercise covering text, tools, cancellation, New API quota/revocation,
  exact upstream attempt counts and billing reconciliation before funded invites.
  Offline tests cannot verify live relay configuration or certify provider bills.

Changing New API's channel/price policy, adding users or raising its quota never
raises Canvas's own limits. Raising Canvas's limits never grants New API quota.
Operate and reconcile both systems explicitly.
