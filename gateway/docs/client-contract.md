# Local client contract

The gateway is a separate service. The existing Canvas Hub app is unchanged by
this directory. A Python provider adapter can call this API from the
local companion process and leave collection, storage, and tool dispatch local.

## Transport and identity

Use HTTPS for remote gateways and bearer session authentication. Tokens belong
in the local app's private credential store, never a query string, browser
storage, conversation history, backup, or tool result. The gateway does not need
the local app token or any Canvas credential. Calls from the browser are outside
this contract; browser UI requests should go through the existing loopback app.

An operator issues a single-use invitation. `POST /v1/enroll` with
`{"invite":"<token>"}` exchanges it for a session. Subsequent calls send
`Authorization: Bearer <session-token>`. An invitation is not a password and
cannot be reused for sign-in. Session expiration requires a new operator-issued
grant for the same account so its quota history is preserved.

Successful redemption returns HTTP `201` with `{user_id, session_id, expires_at,
token}`. Keep `token` private. Redemption consumes the invite and returns the
session credential once; a lost enrollment response requires operator recovery,
not another attempt to reuse that invitation.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/enroll` | Redeem a one-time invite |
| `GET /v1/allowance` | Read the authenticated account's quota state |
| `POST /v1/responses` | Start one bounded model request |
| `GET /v1/requests/:id` | Read this account's request/accounting status |

Account and request IDs are opaque server-generated values. All timestamps are
Unix milliseconds. Usage token counts are JSON numbers; monetary response fields
are decimal strings in micro-USD so clients do not lose precision. Input policy
and administrative limits use bounded integer JSON numbers.

`GET /v1/allowance` reports `daily` and `monthly` objects containing
`limit_microusd`, `spent_microusd`, `held_microusd`, `remaining_microusd`, and
`resets_at`, plus `reset_timezone: "UTC"`, `gateway_paused`, `mode` (`mock` or
`production`), approved `tools` names, and `models` (each with `id`,
`min_output_tokens`, and `max_output_tokens`). This is a display snapshot; only a
successful server reservation authorizes another call.

The same response advertises the versioned wire limits:

```json
{"capabilities":{"protocol_version":1,"max_tools":128,"max_tool_calls_per_response":32,"max_messages":128,"max_body_bytes":65536}}
```

`max_body_bytes` reflects deployment policy; the other limits above are this
protocol version's bounds. Up to 128 approved definitions can be enabled for a
turn, covering the current app's 61–78 tools. This does not raise the separate
32-call limit in one response. Check the version and limits explicitly; do not
silently truncate tools, messages, or user context to fit them.

Request status includes `request_id`, `model`, `status`, `created_at`,
`deadline_at`, `reserved_microusd`, `charged_microusd`, `error`, and `usage`.
`charged_microusd` is null while cost is unresolved. A request may be `reserved`,
`dispatched`, `completed`, `failed_before_dispatch`, `uncertain`, or `overrun`.
Uncertain requests retain a hold rather than pretending their actual bill is
known; those holds also reduce later periods until authoritatively settled.
Settled `usage` always includes `input_tokens` and `output_tokens`. The real
adapter also reports `cached_input_tokens`, `cache_write_input_tokens`,
`cache_write_30m_input_tokens`, `cache_write_12h_input_tokens`, and
`reasoning_tokens`. Cache counts are subsets of input; reasoning is a subset of
output. Do not add these details to the totals a second time.

## Sending one model request

Generate a new cryptographically random `Idempotency-Key` for one intended model
invocation and retain it with the exact request body until the outcome is known.
For a transport retry, use the same key and body. A deliberate new model attempt
needs a new key and may consume more allowance. The key is scoped to the gateway
account, not a short-lived session or local conversation.

`POST /v1/responses` accepts an approved model alias, a message list, a bounded
`max_output_tokens`, and optional approved function names. Definitions are owned
by the server's policy; the client does not upload arbitrary schemas. It does not accept a
provider URL, owner API key, reported cost/usage, a claimed user identity, arbitrary
provider options, or provider-hosted tools. The gateway owns the model catalog,
accepted sizes, prices, quotas, and dispatch policy. The example policy is a
synthetic development policy, not a settled free dollar entitlement.

Each tool round is a separate model request and separate reservation. Send only
the minimal context needed for that round. If a request cannot fit its worst-case
reservation, it may be declined even though the remaining allowance is positive.
The server cannot safely assume that the eventual response will be short.

The response uses newline-delimited JSON (`application/x-ndjson`), not OpenAI
Chat Completions SSE. Decode UTF-8 incrementally and buffer until a full newline;
network chunks need not align with JSON records. Text and function arguments can
arrive in fragments. Do not treat an HTTP `200` or one text fragment as a complete
model result. Require the successful terminal record and complete tool arguments.

Records have these shapes:

```json
{"type":"meta","request_id":"<uuid>","reserved_microusd":"<integer>"}
{"type":"text.delta","delta":"text fragment"}
{"type":"tool_call.delta","index":0,"id":"call_123","name":"demo_read_only","arguments_delta":"{"}
{"type":"tool_call.delta","index":0,"arguments_delta":"}"}
```

The terminal `done` record contains the request-status fields plus
`finish_reason` (`stop`, `tool_calls`, or `length`). An `error` record contains
`request_id`, a stable `error` code, and `status`. Tool ID/name appear on the first
fragment; assemble `arguments_delta` by `index`. Never execute calls from a
`length`, `error`, or unterminated response. `X-Request-ID` also identifies the
original streaming request.

## Retry, stop, and fallback behavior

An idempotent replay returns request metadata; it does not regenerate or replay
the model output. The gateway intentionally stores no output transcript. A
stream lost after dispatch may therefore be charged and unrecoverable. Present
that outcome honestly and require a deliberate new attempt before spending again.

A replay is JSON with `replay: true` and `output_replay_available: false`, using
HTTP `202` for `reserved`/`dispatched` and `200` for other statuses. A changed body
under the same key returns `409 idempotency_conflict`. Keys survive sessions and
quota resets. Changing the approved catalog can cause validation to fail before
replay lookup; rotating the admin secret changes the keyed request fingerprint
and makes old-key comparisons conflict. Neither case authorizes a new automatic
provider attempt.

Closing the stream requests cancellation but does not prove that the provider
stopped before billing. Timeout or ambiguous usage retains a conservative
charge. Poll request status at a bounded rate if the local process is uncertain;
never submit a new key automatically just because a connection broke.

On quota, global budget, concurrency, pause, authentication, or provider errors,
stop the current attempt. Show the returned state and the next reset time when
available. The user may explicitly select a locally configured BYOK provider;
that provider continues to use the existing local credential path. Do not send
their BYOK key through this gateway or silently switch payment sources.

## Mapping to the current Python assistant

The existing integration seam is `canvas_hub/assistant/providers.py`:

| Local object | Gateway adapter responsibility |
| --- | --- |
| System prompt and neutral transcript | Build only allowed message fields; preserve conversation order |
| `on_text` callback | Accumulate deltas and call with the text so far |
| `ToolCall(id, name, input, input_error)` | Assemble complete fragments, validate JSON object arguments, reject partial/truncated calls |
| `Turn.text`, `Turn.tool_calls`, `Turn.stop` | Produce only after the successful terminal event |
| `Turn.usage`, `Turn.model` | Display server-reported metadata; do not use it to authorize spending |
| `Stopped` | Cancel transport, retain the request key/status, treat usage as potentially charged |
| `ProviderError` | Preserve quota/auth/ambiguous-result distinctions; disable automatic paid retries |

The included Responses/Luna adapter uses reasoning effort `none` and this
stateless text/function contract. Provider-native continuation state is not a
request field. Enabling broader reasoning modes may require a versioned
continuation envelope and model-specific validation; it must not become an
unrestricted `extra_body` passthrough.

## Request example

Use a 16–128 character key containing only letters, digits, `_`, or `-`; a random
UUID is suitable. The model alias and tool names below are schematic: they must
exist in the deployed policy.

```http
POST /v1/responses
Authorization: Bearer <session-token>
Content-Type: application/json
Idempotency-Key: 89c222fb-6ce1-4c13-9daa-18ef8cc58f9d

{
  "model": "mock-text",
  "messages": [
    {"role": "system", "content": "Answer using only the supplied excerpt."},
    {"role": "user", "content": "This is synthetic test text."}
  ],
  "max_output_tokens": 128,
  "tools": []
}
```

Allowed message shapes:

```json
{"role":"user","content":"text"}
{"role":"system","content":"instructions"}
{"role":"assistant","content":"text","tool_calls":[{"id":"call_123","name":"approved_name","arguments":"{}"}]}
{"role":"tool","tool_call_id":"call_123","content":"tool result text"}
```

Every assistant call ID in submitted history must be unique and have exactly one
matching subsequent tool result. Historical calls are validated separately from
the tools enabled for this turn: `tools:[]` may disable new calls while preserving
earlier call/result history. Arguments must encode a JSON object; the local tool layer remains
responsible for validating them against the actual schema. Messages accept text
only; attachments, screenshots, remote URLs for fetching, and arbitrary native
provider blocks are outside this first protocol.

Before tools run, the local app remains responsible for validating the tool name
and full arguments, applying local permissions, and showing existing write
confirmations. A gateway response conveys model output, not user authorization.
