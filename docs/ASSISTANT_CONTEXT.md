# Assistant context management

The Assistant keeps original messages append-only and builds a separate provider
projection. In Chat, **Context & usage → Compact conversation** summarizes older
complete turns using the selected setup. Provider settings offer automatic
compaction (default off) and an optional verified context capacity. Both actions
consume model quota. Summary usage is stored separately and included in the
conversation's recorded-response totals, including cache counters when supplied.
Unknown usage after errors, cancellation or restart is labelled unknown. New
answer attempts are recorded before dispatch, including failures and cancellation;
historical failed attempts cannot be reconstructed. Input/output totals already
include any reported cache/reasoning subsets. These figures are not a complete
billing ledger; see [the usage contract](PROVIDER_USAGE.md).

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
| `auto_compact` | `false` (manual compaction remains available) |
| `auto_threshold_percent` | `80` |
| `keep_turns` | `2` |
| `summary_tokens` | `2000` |
| `history_tokens`, `tool_tokens`, `document_tokens` | Share remaining input space; an explicitly configured category ceiling still applies |

These are configurable **application budgets, not advertised model limits**.
Unknown models/endpoints are not assigned invented capacities. Verify the actual
endpoint's capacity and configure it, especially for small local models. Estimates
include provider-native reasoning, schemas, message framing and non-ASCII text;
10% headroom and response/reasoning space are reserved. No paid tokenizer call is
used. Explicit extra-provider output limits increase the reserve. Anthropic and
compatible calls set an output bound; Codex's app-server retains control of its
native output limit, so its reserve is a planning allowance. Actual provider usage
may differ from estimates. Direct SDK retries are disabled; the narrow pre-acceptance
compatibility fallback is documented in [the usage contract](PROVIDER_USAGE.md).
Compaction always uses a single attempt without capability negotiation.

## Tool selection and discovery

The circular arrow beside the chat input opens a searchable list of all currently
permitted groups and tools. `/tracker` (or another group/tool command) adds a
removable chip. Selecting tools or entering a slash command alone does **not**
send a message, call a model or execute a tool. Add a question and press Send.
Workspace actions preload only the permitted source/tracker tools their prompt
requires. This passes through the same scope, provider disclosure and send
validation; selecting schemas does not execute actions or grant permissions.
Chips apply to that message only and clear after a successful send; changing chat
or leaving the view discards an unsent selection. The requested selection remains
in the user message's `tool_selection` metadata and is shown in history. There is
no sticky conversation permission or tool preference. Slash syntax is recognized
only at the start of a message; normal slash text elsewhere is unchanged.

The account-scoped `GET .../assistant/tools` returns permitted metadata; the send
endpoint accepts `tool_selection` such as `["group:tracker", "tool:read_document"]`.
The server validates it before storing a message. A small default set plus
`discover_tools` remains available even with no explicit selection. Discovery can
search/page tool metadata and use `select` to replace discovered choices while
retaining manual choices for the current run. Tool order follows fixed groups and
names, independent of discovery order. Each discoverable set is capped at 32 schemas
per request; excessive tool counts fail visibly and atomically. Complete schemas
share the actual context budget with system text, history and source content;
the server checks the whole request before each provider dispatch, including
configured capacity, output reserve and headroom. There is no separate fixed
8,000-token schema slice. This does not add a step limit. Discovery can switch
tools later, so absent schemas do not mean a capability is unavailable.
Selecting tools returns a compact acknowledgement unless catalog search/paging
was also requested. Codex receives only schemas not already supplied in the same
native turn; `refresh_schemas: true` explicitly rereads selected definitions.
Prior discovery results remain in the saved history unchanged.

Permissions remain authoritative and are rechecked during a run. Selection never
enables a permission or confirms Canvas writes. Historical calls/results stay
intact even after the advertised set changes; the shared registry still validates
current tool calls independently of historical advertisement. Retry continues to
send `tools=[]` and rejects discovery/dispatch as well as all other tool calls.
Codex's native tool list is fixed within a turn: discovery returns the selected
schemas and `call_discovered_tool` invokes only a loaded, permitted tool through
the existing validated registry, without restarting a pending native turn.
Dispatched document results retain their document budget classification and
original execution name. No automatic old-tool-output masking is introduced.

For `allowance_gateway`, its reviewed setup remains the tool authority. The picker
filters to that setup's approved names (`profile_id` selects the catalog); changing
setups clears chips and reloads the list before another send. No chips preserves
the reviewed list, and chips narrow it to the selected tools plus permitted
defaults. Local discovery/dispatch names are not advertised to this fixed-registry
provider. Its fixed catalog supports up to 128 reviewed names, subject to context
and advertised gateway request limits; the local discovery-only 32-schema limit
does not truncate it. Additional gateway tools require the existing setup review.
Withdrawing gateway consent/session or changing its reviewed policy stops further
requests and local tools; already dispatched requests may have used allowance.

The picker supports keyboard navigation, removable chips, Escape, outside click,
focus restoration, viewport clamping and route cleanup. It uses a leftward circular
launcher with a searchable list; it does not change the separate tracker layout.

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

`list_context_sources` catalogs identity, version and provenance metadata, with
bounded pages and explicit `next_offset`. `include_preview: true` adds opening
excerpts, labelled as incomplete previews. Full rules remain accessible through
the versioned reader; a catalog never establishes complete reading coverage. `search_context_sources`
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
notice field and derived source/chunk/FTS tables. Migration `0018` distinguishes
ordinary chat attempts from summary usage. No API keys or credentials enter these tables. Existing
backups include checkpoints; portable source imports rebuild the derived index.

Integration surfaces: Assistant runner/config/provider adapters, the new compact
route, the shared tool registry, component state services, search/extraction hooks,
and Assistant-only sections of `web/static/app.js`. The combined branch also
integrates tracker layout, external login and the optional allowance provider.
The gateway is independently authoritative for reservations and settlement;
local telemetry cannot authorize quota. Source bodies use the separate
[request-local source contract](SOURCE_CONTEXT_PAYLOADS.md).

Source invalidation remains conservative and account-wide: source versions,
imported revisions and resource extraction invalidate the checkpoint. The next
request uses original history until safely compacted again. Tracker and local
note/to-do changes retain the historical summary and completed-action manifest,
with a freshness notice requiring current reads and fresh tracker state tokens.
This also covers live edits during summarization. Older broad fingerprints are
invalidated once. There is no vector
search, semantic quality guarantee, exact tokenizer, monetary-price calculation or
multi-pass summarization of already enormous histories. An active Codex tool turn
is never restarted for compaction; an oversized turn stops with its results saved.

Validation uses synthetic data, in-process provider doubles, real SDKs against
local fake Anthropic/OpenAI endpoints, a local Codex protocol peer and Chromium.
No real account, API credits, hosted deployment or live model call is used. Focused
checks cover budgets, group integrity, failure/cancel/concurrency, persistence,
isolation/invalidation, guarded tracker patches, bounded retrieval/traversal and
the compaction/history/usage browser flow. See [combined validation and startup](INTEGRATION_CANVAS_NEXT.md);
the full repository suite was not rerun for this integration.
