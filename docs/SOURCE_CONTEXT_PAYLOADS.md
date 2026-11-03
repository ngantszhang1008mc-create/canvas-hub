# Source payloads for the integration test branch

Workspace sends keep the user's intent and a source-manifest reference in the
permanent prompt. Complete small source bodies are added only to the current
provider request. Old user messages and tool results remain unchanged.

## Interface and integration

This change is based on `codex/integration-canvas-next` at
`49507d1bab3f92123b077b6546f0bc204ddac778`, including PR36's existing source index
and bounded reader, the reviewed connected-site workflow, and the allowance
gateway. It adds no provider, accounting path, schema migration, or tool lock.

The shared Assistant changes are limited to:

- `AssistantService.send(..., *, request_context=None)` receives a server-prepared
  bundle matching the acknowledged workspace scope. HTTP clients cannot supply
  it directly. Only its metadata is saved as `source_context` for provenance and
  explicit retry; bodies are not stored in user messages.
- `_run(..., request_context=None)` passes the bundle to context preparation. On
  retry/restart it can restore a small source only at its recorded version.
  Changed/deleted sources produce an explicit gap, without sending replacement
  content under the old reference.
- Ordinary follow-ups retain the latest manifest from the same scoped
  conversation. They selectively restore up to two explicitly named small
  sources (whole IDs or titles/filenames), or one small source for an unspecified
  follow-up, with a 6,000-character total bound. A named large source stays a
  versioned reference; unrelated small text is not substituted. Remaining
  sources/ranges require deliberate reads, and changed versions are never
  silently substituted. Every retained reference is checked for account ownership
  and its exact recorded version using metadata only. All manifest IDs/versions
  remain present; this check does not load every source's body or establish
  reading coverage. No source bodies are appended to permanent history.
- `ContextService._prepare_context(..., request_context=None)` projects source
  evidence after the existing history projection and before budget measurement.
  If an existing compaction policy changes that projection, source coverage is
  recalculated against the actual remaining messages. Original history is not
  rewritten or masked by this change.

These keyword arguments are retained alongside tool selection and usage tracking.
Gateway configuration, reviewed tool allowlists, normalized usage, private
attempts, failure handling, and source/provider consent remain on the existing
integration paths. A local mock gateway test covers both a Workspace request and
source classification.

The combined version defaults `auto_compact` to `false`. Manual compaction remains
available; historical tool outputs are not automatically masked. No source change
enables compaction, adds a step limit, or switches models.

## Reading coverage

A source has its existing index `source_id`, exact `version`, and `total_chars`.
The initial request includes at most 20 source manifests. Extra sources and
unindexed attachments are named as gaps, with instructions to page the existing
catalog. This bounds the initial bundle, not the task or the reading tools.

Sources of at most 4,000 characters can be included whole, up to 12,000 characters
of inline text per request. Large sources receive no arbitrary opening slice.
The existing `read_context_source` retrieves their complete original extracted
text in bounded ranges. The legacy `syllabus_body` field is also indexed, with
selected older items repaired from their complete local fields, so tail rules
remain reachable without a new collection.

For each version, temporary context records ranges present in retained tool
results and ranges supplied inline. Overlapping ranges merge for coverage, and
already-present small-source ranges are not repeated. Errors, truncated JSON,
short text for a claimed range, and stale versions establish no coverage.
When a restored reference has changed or disappeared, its old tool text remains
verbatim in history but is labelled historical and excluded from current coverage.
Codex-dispatched reads use their recorded `executed_tool` name for the same coverage
checks, so an indirect read does not duplicate the inline source body.
Intentional rereads still work. Shared attachments retain all selected item
origins while their body is supplied once.

`complete_read` refers only to the indexed extracted text present in this request.
It does not certify original images, diagrams, PDF layout, collection coverage,
or all course requirements. Full grading/assignment interpretation still requires
all relevant ranges, attachments, structured fields, and evidence gaps to be
checked. Search hits alone do not establish comprehensive coverage.
Task-scoped questions start with relevant sources/search hits and read the
necessary original ranges rather than paging every unrelated file. Limited
coverage must be stated, not passed off as a complete rule. A missing formula,
image or exception is an explicit evidence gap: request the original and preserve
existing rules pending review, rather than inferring a formula from endpoints or
chasing unrelated courses.

## External classification

Classification uses bounded beginning/interior/ending excerpts, representative
role-bearing passages, and observed public DOM title/node/link evidence. Each
request and saved classification carries its text hash and supplied ranges.
Partial evidence is explicitly for classification, not complete rule extraction.
The complete locally extracted content continues to be stored unchanged.

Public adapter proposals retain the existing review workflow. Any proposed
record/field selectors must extract successfully from the same fetched DOM,
including valid required fields and unique record IDs; no second fetch is made.
Connected classification samples only the exact reviewed/sanitized text, keeps
its origin-only provider URL, sends no additional DOM or links, and cannot create
a public adapter. Provider/account/session/page changes still invalidate consent.

## Synthetic payload measurement

Baseline: `49507d1`. Metric: serialized UTF-8 bytes for source/user-message data,
not billed tokens, cache hits, end-to-end latency, or total provider cost. The
Workspace fixture is one assignment plus three complete 3,112-character text
attachments. The fifth-request comparison repeats the same question five times,
without tool calls; shared system/tool schemas and assistant replies are excluded.

| Payload | Before | After | Change |
| --- | ---: | ---: | ---: |
| Permanent user prompt string | 12,415 | 1,509 | 87.85% smaller |
| First request user messages | 12,566 | 16,126 | 28.33% larger |
| Fifth request user messages | 62,826 | 22,282 | 64.53% smaller |
| Public page classification | 110,127 | 5,077 | 95.39% smaller |

The first small-source request grows because it now includes version, range,
and provenance metadata. Repeated requests avoid accumulating document bodies in
user history. The classification fixture retains all 110,041 extracted characters
locally and sends 3,800 characters of representative excerpts plus metadata.
A complete extraction of a large document can still need additional tool calls;
these figures do not claim savings for that different task.

## Focused checks

`tests/test_source_payloads.py` exercises source deduplication, old-history
preservation, range traversal, late exceptions, stale versions, failed results,
restart/retry, shared origins, catalog pagination, actual-DOM validation,
connected reviewed-text boundaries, and real adapter use against a local mock
gateway. Existing Workspace, study, source analysis, connected analysis, context,
and gateway tests were also run with `-m 'not browser'`. No live provider, real
account data, deployment, installation, or main-branch merge was used.
