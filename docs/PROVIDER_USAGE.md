# Assistant usage and stable request prefixes

Reviewed 2026-11-02. This change is local telemetry and request construction. The
gateway owns reservations, prices, settlement and free allowance; none of the
local counts or estimates may authorize spending or release a reservation.

## Usage contract

`Turn.usage` from the direct adapters uses `usage_version: 1`. `input_tokens`
includes cache reads/writes; `output_tokens` includes reasoning. Cache and
reasoning fields are breakdowns, **never additional tokens**. The original
documented numeric fields remain in `provider_usage`. Unknown values are `null`,
not zero. `source` distinguishes provider/gateway counters from estimates;
`status` is reported, partial, unknown or estimated; `final: false` identifies a
nonterminal snapshot. A reported status means both inclusive totals are present,
not that every optional breakdown is available or that this is a billing ledger.

Totals include only terminal reported fields, with per-field `missing_fields`,
`unreported_requests` and `estimated_requests`. Nonterminal counts and estimates
are excluded from those reported subtotals. The UI labels missing breakdowns as
unreported and mixed coverage as partial. The existing context budget estimate
remains separate. No provider tokenizer or extra model request is used.

Provider accounting:

| Provider | Stream usage request | Normalization |
| --- | --- | --- |
| Anthropic | Messages SDK final usage, no extra usage flag | Total input is uncached input + cache read + cache creation. `output_tokens_details.thinking_tokens` is included in output. TTL write breakdowns are retained but not added again. |
| OpenAI Chat Completions | `stream_options.include_usage: true` | Prompt/completion totals include cache/reasoning. Read empty-choice usage chunks after the finish chunk. |
| Gemini OpenAI endpoint | `stream_options.include_usage: true` | Candidate completion and reasoning can be separate. Add explicit reasoning only when total = prompt + completion + reasoning. If total already = prompt + completion, do not add it. An unexplained remainder leaves inclusive output unknown, preserving the raw counters. |
| DeepSeek | `stream_options.include_usage: true` | Supports usage on the final content chunk. Cache hits are a prompt subset; cache misses are not cache writes. |
| OpenRouter | No flag; usage is automatic | Standard totals; optional cached, cache-write and reasoning breakdowns. |
| Ollama | `stream_options.include_usage: true` for the Ollama preset on loopback, or official cloud endpoint | Standard reported totals, optional details remain unknown if absent. |
| Other compatible endpoint | No automatically inferred support; user-supplied `stream_options` is supported | Standard documented Chat Completions fields. Contradictory totals are partial; no reasoning inferred from text. |
| Codex app-server | Existing `thread/tokenUsage/updated` notifications | Use differences of cumulative thread totals at tool boundaries to cover multiple requests and ignore repeated snapshots. Older peers exposing only `last` retain that limited coverage. |
| Allowance gateway | Existing normalized terminal usage, unchanged gateway protocol | Read `cached_input_tokens`, `cache_write_input_tokens`, `reasoning_tokens` and inclusive totals without adding them twice. Quota authority remains in the gateway. |

Capability detection uses the actual endpoint, not just the selected preset.
Usage absent at the end of a successful answer does not cause another request.

## Retry and persistence

Direct SDK automatic retries are disabled because a connection failure may have
occurred after acceptance and billing. OpenAI compatibility fallback is confined
to an initial HTTP 400 explicitly rejecting `stream_options`/`include_usage` or
`stream` as unsupported. It removes only the rejected streaming capability and
remembers that rejection for the provider instance. An ambiguous 400, 429, 5xx,
connection error, accepted-stream error, cancellation or tool parse error never
triggers this retry. Summary/source-analysis `_single_attempt` remains one
attempt with no capability negotiation. The existing explicit Retry action and
tool/cancellation recovery remain in place.

Migration `0018_assistant_usage_purpose.sql` extends PR36's
`assistant_context_usage` table with a purpose (`summary` by default for existing
rows; `chat` for ordinary calls). New chat attempts are written before provider
dispatch and finalized even on errors/cancellation. Successful display messages
link their `usage_request_id`, so totals do not count the ledger and message
twice. Restart marks unfinished attempts interrupted using the existing recovery
path. Summary attempts remain included exactly once, including failed or
cancelled ones. Gateway's private idempotency journal is not changed or replayed.
Source analysis has its own lifecycle and is not included in conversation totals.
Historical failed chat requests from before this migration cannot be reconstructed.

The local gateway adapter retains validated terminal usage even if subsequent
stream framing, tool parsing, cancellation or journal persistence fails. The
attempt ledger counts it once; retained usage never authorizes tool execution.
Each call clears its previous usage before authorization/preflight. Protocol v1
has no midstream token counter: a missing or invalid terminal leaves usage
unknown, with no inference from text or quota reservations.

## Prefix stability

Tools are sorted by name and object keys are serialized consistently, including
schemas and reconstructed neutral tool arguments. Array order, message order,
source text and native Anthropic content/reasoning are preserved. The same
enabled tool set produces the same tool prefix regardless of group selection
order; a changed set legitimately changes the prefix and disabled tools are not
retained. System text is passed unchanged, without inserting request IDs, usage
metadata or timestamps into that prefix. Existing provider caching settings are
preserved. Stable bytes improve reuse eligibility; they cannot guarantee a hit,
which also depends on model, routing, minimum prefix length and cache lifetime.

The combined version defaults automatic compaction off and retains original
history. Tool selection and transient source context are integrated separately
from accounting; neither changes gateway settlement or authorizes extra requests.
See [combined validation and startup](INTEGRATION_CANVAS_NEXT.md).

## Official references

- [OpenAI streaming options](https://developers.openai.com/api/reference/resources/chat) and [reasoning usage](https://developers.openai.com/api/docs/guides/reasoning).
- [Anthropic cache categories](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) and [thinking token breakdown](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost).
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai), [Google compatibility response examples](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/migrate/openai/examples), and [native usage categories](https://ai.google.dev/api/generate-content#UsageMetadata). The conservative reconciliation above avoids assuming a total-token remainder is reasoning.
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/).
- [OpenRouter usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).
- [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility).
- [Codex usage protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/typescript/v2/ThreadTokenUsage.ts) and [token breakdown](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/typescript/v2/TokenUsageBreakdown.ts).

Validation uses SDKs with local synthetic HTTP transports and protocol peers,
plus focused context/recovery and gateway integration checks. No real API keys,
model calls, source data or paid cache-warming calls are involved.
