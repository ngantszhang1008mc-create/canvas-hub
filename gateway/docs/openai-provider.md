# OpenAI Responses adapter

The production adapter sends requests to the fixed
`https://api.openai.com/v1/responses` endpoint using the server's `OPENAI_API_KEY`.
Only the reviewed `gpt-6-luna` native model is supported in this first version;
the client selects an operator-defined alias. The endpoint, native model, prices,
tools, processing tier, and cache behavior are not client request fields.

## Supported request surface

The adapter maps plain-text message history and local function calls/results to
Responses input items. Only server-approved function schemas are sent. Requests
use streaming, `store:false`, `background:false`, standard processing
(`service_tier:"default"`), disabled input truncation, and 30-minute implicit
prompt caching. Redirects and automatic HTTP retries are disabled.

Reasoning effort is fixed to `none` for the initial stateless client contract.
This keeps function round-trips compatible with the local neutral transcript
without adding opaque encrypted reasoning state. There is no client option to
enable another reasoning mode. Any reasoning tokens reported in authoritative
usage are nevertheless accounted for as part of the output total.

Images, file inputs, provider-hosted tools, multi-agent requests, response IDs for
server-side conversation state, regional endpoints, and alternate service tiers
are outside this adapter. The model's broader capabilities do not enable those
features in this gateway.

## Reservation and settlement

The reviewed Luna context bound is 1,050,000 tokens. A production reservation
uses that full accepted-input bound, including any provider rendering overhead,
plus the enforced output bound at the highest applicable configured rates. It
does not use characters, bytes, or an estimated tokenizer ratio as a proof of the
input cost bound. This is deliberately conservative: a small request can require
considerably more temporary headroom than its final charge.

The output cap includes reasoning tokens and must be within the provider's
supported range. Final settlement uses only terminal provider usage and the price
snapshot captured at reservation:

```text
ordinary input = input_tokens - cached_tokens - cache_write_tokens
input cost = ordinary input × ordinary rate
           + cached reads × cached rate
           + 30-minute cache writes × 30-minute write rate
           + 12-hour cache writes × 12-hour write rate
output cost = output_tokens × output rate
```

Divide the combined numerator by one million and round up once to integer
micro-USD. Cache-write rates are total rates, not surcharges. Reasoning tokens
are already included in `output_tokens` and must not be added again. The long
context rate set applies to the entire request when authoritative input exceeds
272,000 tokens. Missing or inconsistent usage is not zero usage.

When the per-lifetime cache-write breakdown is present, its components must sum
to total writes. Without that breakdown, the adapter can attribute writes to the
explicitly requested 30-minute lifetime. Configured rates must cover the reviewed
provider prices; an operator may choose more conservative rates. Gateway quota
charges are therefore not a replacement for the provider invoice.
Reservations also cover the more expensive 12-hour write category. If the
provider reports it despite the fixed 30-minute request, settlement records the
full reported cost and pauses admission as a policy breach.

## Streaming and uncertainty

SSE is decoded incrementally with bounded buffering and total response limits.
The adapter forwards only validated text and function-call fragments through the
existing NDJSON contract. The local app still validates complete function
arguments and permissions before execution.

Only a validated terminal response permits settlement. An output-limit terminal
response is marked `length`, so its partial tools cannot run. HTTP errors,
malformed/truncated streams, missing terminal usage, aborts, and inconsistent
model/tier information do not create a refund or an automatic retry. The ledger
retains the reservation, and protocol/accounting breaches can pause admission.

All adapter verification uses mocked HTTP/SSE in the Workers runtime. No actual
OpenAI request or account configuration is part of this change. Actual model
quality, account access, latency, and invoice reconciliation remain unverified.

## Official references checked on 2026-11-02

- [Luna model specification](https://developers.openai.com/api/docs/models/gpt-6-luna)
  establishes the model ID, context/output bounds, function support and price tiers.
- [Create response API](https://developers.openai.com/api/reference/python/resources/responses/methods/create/)
  defines output limits, processing tier, storage and request fields.
- [Streaming event reference](https://developers.openai.com/api/reference/resources/responses/streaming-events/)
  defines text, function and terminal event envelopes.
- [Prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
  explains read/write usage and cache-write lifetime accounting.
- [Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
  explains output-token accounting and stateless continuation requirements.

Review these facts before changing native models, pricing, features, or request
parameters. Adding a generic upstream URL/model passthrough would invalidate the
reviewed reservation bound.
