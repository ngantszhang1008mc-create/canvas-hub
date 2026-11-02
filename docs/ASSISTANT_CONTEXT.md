# Assistant context management

The Assistant keeps original messages append-only and builds a separate provider
projection. In Chat, **Context & usage → Compact conversation** summarizes older
complete turns using the selected setup. Provider settings enable automatic
compaction (default on) and an optional verified context capacity. Both actions
consume model quota. Summary usage is stored separately and included in the
conversation's recorded-response totals, including cache counters when supplied.
Unknown summary usage after errors, cancellation or restart is labelled unknown.
Ordinary answer attempts that fail or stop before saving a response are not
tracked in these totals and may have used quota; the UI states that limitation.
These figures are not a complete billing ledger.

Compaction retains the latest two user turns and all their assistant/tool groups.
The checkpoint includes goals, decisions, unresolved questions, source/tracker
references and a deterministic tool execution manifest. Native reasoning and tool
pairs in retained turns are replayed unchanged. The checkpoint and usage survive
restart. Failed, invalid, non-reducing, cancelled or concurrently stale summaries
do not replace context. Automatic compaction makes at most one summary attempt
per user run, with SDK retries/fallbacks disabled. An oversized active turn or
history too large for a single summary stops with an actionable budget error;
essential context is not silently dropped and actions are not re-executed.

## Budget configuration

Each profile accepts a `context` object in `PUT /api/assistant/config`. It is local
configuration, not forwarded as provider request settings.

| Field | Default |
| --- | --- |
| `capacity` | Application safety ceiling: Anthropic/Codex 64,000; compatible endpoints 32,768 |
| `response_tokens` | Anthropic 16,000; others 4,096 |
| `auto_compact` | `true` |
| `auto_threshold_percent` | `80` |
| `keep_turns` | `2` |
| `summary_tokens` | `2000` |
| `history_tokens`, `tool_tokens`, `document_tokens` | 60%, 40%, 40% of remaining input space, with an additional aggregate ceiling |

These are configurable **application budgets, not advertised model limits**.
Unknown models/endpoints are not assigned invented capacities. Verify the actual
endpoint's capacity and configure it, especially for small local models. Estimates
include provider-native reasoning, schemas, message framing and non-ASCII text;
10% headroom and response/reasoning space are reserved. No paid tokenizer call is
used. Explicit extra-provider output limits increase the reserve. Anthropic and
compatible calls set an output bound; Codex's app-server retains control of its
native output limit, so its reserve is a planning allowance. Actual provider usage
may differ from estimates. The existing normal-chat provider retry behavior is
unchanged; the new single-attempt rule applies to compaction.

## Tracker state API v2

- `list_components` includes identity, purpose and version.
- `get_component_context` returns compact metadata, a version and effective state
  token, and totals calculated by the existing local formula engine. Request
  `row_ids`, or page `include_rows`, when specific inputs are needed.
- `patch_component_state` requires the exact `expected_version` and `state_token`.
  Maps merge recursively; `null` removes a key. Arrays replace as a unit, protected
  by both concurrency guards. Untouched values and linked to-do changes survive.
  A conflict requires a fresh read and review, never blind retry.
- Legacy `get_component` reads and `update_component` definition proposals/pinning
  remain available. **Legacy `update_component.state` is deliberately refused**
  with `component_state_api_v2`, before any mutation. Callers must migrate to the
  guarded patch tool. The old argument remains recognized to provide that explicit
  migration error instead of an ambiguous schema failure. Browser tracker writes
  and their existing state/base version behavior are unchanged.

## Documents and coverage

`list_context_sources` catalogs metadata and opening excerpts. These are labelled
previews, not complete or model-generated summaries. `search_context_sources`
returns at most four 1,400-character chunks (two by default), filtered by course,
item or source. `read_context_source` reads up to 8,000 original extracted-text
characters with required source/version checks and a `next_offset` for exhaustive
traversal. Stable source IDs, content versions, item revisions, source URLs, and
character ranges accompany excerpts. Section/page hints are included only when
the extraction preserves headings/form feeds; physical PDF pages are not guessed.

The local FTS index updates with imports and re-extraction, including empty/failed
extraction and replacement of a current attachment. Existing installations
backfill at most ten stored sources per catalog/search call; the response reports
the remaining unindexed sources. Finish backfill (repeat catalog from offset zero)
before catalog traversal. Coverage also reports files without indexed text and
links the model to collection coverage. Top-k search results never claim complete
reading coverage; comprehensive extraction must traverse every relevant source.
All source text, previews and historical summaries are untrusted data, not system
instructions. Account ownership is checked on every retrieval.

## Integration and limits

Migration `0017_assistant_context.sql` adds checkpoint/usage tables, a conversation
notice field and derived source/chunk/FTS tables. Coordinate its number with other
unmerged migrations. No API keys or credentials enter these tables. Existing
backups include checkpoints; portable source imports rebuild the derived index.

Integration surfaces: Assistant runner/config/provider adapters, the new compact
route, the shared tool registry, component state services, search/extraction hooks,
and Assistant-only sections of `web/static/app.js`. No tracker layout, external
login or allowance backend code changes. A quota backend can later consume
`assistant_context_usage` (provider/model/status/raw usage/estimated input) and the
conversation usage response; this branch does not assume that backend is merged.

Invalidation is conservative and account-wide: source versions, imported revisions,
resource extraction, tracker versions and local note/to-do changes invalidate the
checkpoint. The next request uses original history until safely compacted again.
This can cause extra summary work after unrelated data changes. There is no vector
search, semantic quality guarantee, exact tokenizer, monetary-price calculation or
multi-pass summarization of already enormous histories. An active Codex tool turn
is never restarted for compaction; an oversized turn stops with its results saved.

Validation uses synthetic data, in-process provider doubles, real SDKs against
local fake Anthropic/OpenAI endpoints, a local Codex protocol peer and Chromium.
No real account, API credits, hosted deployment or live model call is used. Focused
checks cover budgets, group integrity, failure/cancel/concurrency, persistence,
isolation/invalidation, guarded tracker patches, bounded retrieval/traversal and
the compaction/history/usage browser flow. Combined integration validation belongs
to the coordinating task; the full repository suite was not rerun here.
