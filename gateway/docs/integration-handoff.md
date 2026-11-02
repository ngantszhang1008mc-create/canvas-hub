# Minimal local-app integration

Keep the hosted gateway separate from the local app. The integration only needs
the adapter in `canvas_hub/assistant/gateway.py`; collection,
local database access, tool execution, and BYOK credential handling stay local.

## Setup

Store a gateway base URL, model alias, expected mode (`mock` for the combined
offline test), and the redeemed session token in the local private credential
store. Do not pass an OpenAI owner key through the app. Redeem an operator-issued
invitation with `POST /v1/enroll {"invite":"..."}`; persist the returned `token`
and expiry. This is gateway authentication, separate from Canvas/SSO.

Call `GET /v1/allowance` with `Authorization: Bearer <token>` before the initial
invocation. Confirm `mode` matches the explicitly selected local setup, choose an
advertised model alias, and intersect advertised `tools` names with the local
tool registry. Inspect the versioned capability limits and explicitly handle an
unsupported registry or request size; never silently truncate the app's tool set.
For mock tests, refuse a production-mode server. Allowance reads
are snapshots; the gateway still decides whether each request can be admitted.

## One request per model/tool round

Send `POST /v1/responses` with JSON, a fresh UUID `Idempotency-Key`, and
`X-Gateway-Mode` matching the reviewed setup. Require the allowance capability
`mode_header: "X-Gateway-Mode"`; every reservation rechecks that mode:

```json
{
  "model": "mock-text",
  "messages": [{"role":"user","content":"Synthetic integration text."}],
  "max_output_tokens": 128,
  "tools": []
}
```

Use the same key and exact body for transport retries. Keep the selected output
bound within the model metadata. There is no arbitrary `extra_body`, native
provider model, endpoint, reasoning option, usage report, or billing field.
Sending `tools:[]` disables new tool calls without invalidating complete historical
call/result pairs. This preserves the app's existing no-tools retry flow.

| Current local transcript | Gateway message |
| --- | --- |
| System string | `{"role":"system","content":system}` |
| User `sent_text` | `{"role":"user","content":sent_text}` |
| Assistant `text`, `tool_calls` | Assistant `content`; calls with `id`, `name`, and JSON-stringified `input` as `arguments` |
| Tool `results` | One `{"role":"tool","tool_call_id":id,"content":content}` per result |

Do not serialize local account objects, cookies, provider configs, browser state,
or tool registry internals. The gateway already owns approved tool schemas.

## Response and stop rules

Parse incremental UTF-8 NDJSON, buffering incomplete lines. `meta` supplies the
request ID; accumulate `text.delta` for the existing `on_text` callback and tool
fragments by `index`. On a terminal `done`, map `finish_reason` to `Turn.stop`:
`stop` → `end`, `tool_calls` → `tool_use`, `length` → `truncated`. Fill `Turn.model`
and `Turn.usage` from that terminal record. Execute complete tool calls only after
`tool_calls`, after local schema/permission checks; never execute truncated calls.

HTTP JSON replay responses contain `replay:true` and no model output. Query
`GET /v1/requests/:id` for accounting status, but do not infer that output can be
recovered or that a new model invocation is free. Errors and lost terminal records
must stop the round. On local `Stopped`, close the stream and preserve the key/ID;
disconnects may retain a full allowance hold.

Quota exhaustion, pause, configuration problems, or provider errors must not
select another paid setup automatically. Offer BYOK only as an explicit user
choice. Disclose that selected messages and course excerpts transit the gateway
and, in production mode, OpenAI.

The integrated Python client never retries a generation automatically. It saves
the UUID/body in owner-only `runtime/gateway-attempts/` before dispatch, keeps
unknown/cancelled attempts for diagnosis, and discards bodies after completion.
These files can contain conversation excerpts; treat the data home as private.

For a combined offline check, use the [local mock instructions](../../docs/INTEGRATION_CANVAS_NEXT.md).
The generated policy enables the app's real `list_components` tool, so a mock
response can exercise local dispatch and its next model round. It requires no
OpenAI key or outbound provider traffic. The committed example policies retain
zero budgets. See [the full wire contract](client-contract.md) for exact fields,
error behavior, and session recovery.
