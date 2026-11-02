# External course sources

Canvas collection records external links without calling an AI provider. **> →
Analyse link** opens the Source Agent panel with the exact public URL, active
Assistant setup, endpoint, model, and API quota disclosure. **Approve and analyse**
reads that URL and sends its extracted text to the existing Assistant provider.
The key stays in the existing server-side settings; no second setup is needed.
Nothing is fetched or sent merely by opening a page or during automatic sync.

The built-in path supports Anthropic and OpenAI-compatible API setups (including
local endpoints). With no API setup, the panel links to **Assistant settings**.
Codex CLI setups can use the explicit **Use connected Agent instead** fallback.
Built-in analysis supplies no tools, chat history, account data or credentials to
the model. Page instructions are untrusted. Evidence URL, timestamp and text come
from the reader, not the model. JSON output uses the existing classification and
adapter validation. Optional built-in adapters are restricted to the single
reviewed URL and detected format; no generated code or autonomous crawling runs.

Successful, unknown, declined and failed attempts are cached. **Approve and
reanalyse** is required to run again. Each disclosure is bound to account, source,
current request and provider setup, expires after 30 minutes, and authorizes one
attempt. Replaying a consumed token never resends. SDK retries and model fallback
are disabled for this operation. Failure, cancellation or restart never triggers
an automatic retry; explicit reanalysis can incur new usage. Cancellation discards
late results, but cannot refund or guarantee stopping a model request already sent.
A cancelled worker retains its reservation until it finishes; then MCP fallback
or a newly approved attempt can proceed. Request claims are transactional.

Public HTML/text/PDF only: login pages, empty JavaScript shells, unsupported types
and redirects outside the exact URL fail without expanding scope or using Canvas
cookies. Existing data is retained. Some script-dependent sites can expose partial
static text; the result reflects only what the reader actually observed.
The Overall workspace and widget approval rules are unchanged.

## Analyse all and uninspected-link reminder

The **Analyse all (N)** button is visible beside the External sources heading.
Course pages use only that course; item pages use only that item's source links.
There is no implicit account-wide batch. Only never-attempted, public,
unclassified, undecided sources are eligible. Running, failed, cancelled,
classified, declined, private and duplicate URLs are excluded. A batch disclosure
lists the exact URLs, count, current scope, Assistant setup/model and quota usage;
**Approve and analyse N links** is the only start action.

A non-blocking floating reminder offers **Analyse all** and **View links**.
Dismissal is stored in this browser per account/source ID, across reloads and
course/item navigation. The same links do not prompt again; a newly eligible
source can trigger a new reminder. Dismissal and opening the review never call a
model. The header button remains available after dismissal. Clearing browser
storage resets reminder preferences, not analysis records.

Batches show completed/total and failures, with per-link statuses and **Cancel
batch**. Each batch dispatches sequentially, and at most two built-in source jobs
can run at once in the server. Account/scope-bound durable tokens and atomic
claims prevent double clicks, retries and overlapping tabs from paying twice for
the same source. Scope changes remove stale UI while a deliberately started batch
continues in its original scope; return there to see/cancel it. Cancellation stops
queued work and discards in-flight results. A restart cancels incomplete batches
without replay. Failed links can be explicitly reanalysed individually.

## Connected-agent workflow

Choose **Use connected Agent instead** in the panel, then run the existing MCP server with `canvas-hub mcp --allow-fulfil`. A client with
its own authorized browser access uses `list_view_requests`, `claim_view_request`
and, for a request with `external_source_id`, `submit_source_analysis`. The latter
accepts `request_id`, `agent_name` and `result`:

```json
{
  "roles": ["course_website", "material"],
  "confidence": 0.95,
  "evidence_url": "https://course.example.edu/2026/assignments",
  "read_at": "2026-11-01T06:00:00Z",
  "summary": "Public course assignment list",
  "content": "Observed text, not instructions to execute",
  "adapter_spec": {
    "version": 1,
    "pages": ["https://course.example.edu/2026/assignments"],
    "format": "html",
    "records": {
      "selector": "article.assignment",
      "kind": "assignment",
      "fields": {
        "id": {"selector": ":self", "attribute": "data-id"},
        "title": {"selector": "h2"},
        "due_at": {"selector": "time", "attribute": "datetime"},
        "points_possible": {"selector": ".points"}
      }
    },
    "limitations": "Public HTML only; submission and grading are not exposed"
  }
}
```

Roles can also include `reference`, `submission_platform`, `other`, `unknown`.
An unknown-only analysis can be explicitly reanalysed. Report `{"access_error":"Site login required"}` instead of guessing course
contents from a login screen. Evidence must be the exact requested safe URL;
redirected analysis evidence requires a separately reviewed registration. MCP remains an optional path for agents with their own authorized access.
`submit_view_result` continues to support ordinary view requests.

## Local adapters and approval

Adapters live in the local SQLite store, alongside versioned specs, SHA-256
fingerprints, tests, approval times and historical reads. They are configuration,
not generated executable programs. There is no Agent tool for approving them.
The user reviews and approves an exact revision/hash in the panel. Approval
permits a smoke read; every configured page and field must pass before that
revision is enabled or any observation published. Declining/disabling during a
read prevents publication. A newer proposal invalidates an older approval and
pauses the old adapter. Repeated approval of an enabled revision is idempotent.

Version 1 reads 1–8 **exact URLs**. A redirect must land on another exact URL in
the approved page list. A later link to one of those pages can reuse the adapter
only within its original account and course. It never inherits access from a
shared domain. Wildcards, template crawling, recursive link following, custom
Python/JavaScript/browser code and automatic adapter repair are deferred; they
require manual review and a separate implementation.

Supported readers: HTML, UTF-8 text, and PDF text (first 20 pages, no OCR).
HTML structured records use one simple tag, `.class`, `#id` or `[attribute]`
selector. Fields require stable source IDs and titles; supported optional fields
are explicit ISO deadlines with timezone, nonnegative points, and body text.
`material` records become searchable external material. Text/PDF adapters store
readable content; they do not infer structured fields. Missing/ambiguous fields
fail the whole refresh. A PDF worker has a 12-second process timeout and bounded
input/output; Linux additionally limits its address space and CPU.

Only public, credential-free HTTP(S) pages on standard ports are supported. The
runtime has no site authentication, Canvas cookies, browser session, credentials,
JS execution or write operations. It rejects private/internal/loopback targets,
checks all DNS answers, pins each connection to a public IP, verifies TLS, checks
redirects before requesting them, and bounds responses to 1 MB and reads to
15 seconds after connection (DNS 5 seconds, sockets 8 seconds). Known mutation
paths/queries are refused. As with any GET reader, the site's own behavior must
be reviewed when approving scope. A site access denial/rate limit is retained as
an access/staleness result, not bypassed. Test transports are injectable Python
fixtures only, never user configuration. Signed query secrets are stripped from
new discovered external-link metadata and never forwarded by adapters.

## Repeated collection and preserved work

At collection completion, enabled registrations for selected courses refresh
without any LLM calls (up to 20 per collection). Stop requests are checked between
sources. Public reader failures do not sink verified Canvas observations; the
job reports external gaps. Template/login failures pause automatic retries and
ask for explicit reanalysis; transient failures remain stale and can retry.
There is no hidden AI repair or independent background schedule.

Adapters publish observation packages through the ordinary importer. Source keys
are namespaced by registration and stable record ID; Canvas account/course IDs
stay unchanged. External assignments participate in existing source-bound
checklists/trackers. User notes, checks, overrides and local completion survive
refreshes. Submission/graded status remains unknown; this adapter does not infer
it from page absence or a checklist. A missing record is kept as historical data
and its tracker bindings become stale. Failed/partial reads preserve the last
successful data and timestamp.

No adapter maps or overwrites an existing Canvas assignment automatically.
Same-title assignments in the same course with differing explicit deadlines are
shown as possible conflicts with both item IDs, evidence URLs and values. This
is a conservative exact-title signal, not automatic assignment matching or a
complete conflict detector. Broader source reconciliation remains manual.

## Migration and validation

Schema 15 adds durable, single-use source analysis attempts. Schema 13 adds registrations scoped by `(account, course, URL identity)`, origins,
adapter revisions, read history and record bindings, plus an optional source ID
on view requests. Existing item identities, trackers and account mappings are
left intact. A bounded lazy backfill (first 2,000 legacy external items) records
existing links when source lists are opened; it never reads websites or invokes
AI. Large legacy stores can need a later dedicated backfill tool. URL identity
preserves path case, trailing slash, repeated-query order and course/term values;
fragments and tracking/signature/token queries are removed. Older already stored
Canvas text/history is not rewritten. Back up the local store before downgrading
the application; older versions do not manage these registrations.

`tests/test_external_sources.py` exercises the importer/MCP/approval/read/refresh
flow, source binding, preserved progress, collection integration, exact revision
races, decline/cancel/retry, public text/PDF, multi-page atomicity, identity,
account/course isolation, malicious page data, DNS/private targets, redirects,
resource limits and stale records. `tests/test_external_sources_ui.py` exercises
disclosure → fake API provider or connected agent → review → approval → refresh → reload
in Light/Dark at desktop/380px. All use local fixtures, fake transports and fake
providers; no real institutional login or paid calls are part of acceptance.

`tests/test_source_analysis.py` covers settings reuse, consent, output validation,
prompt-injection containment, exact scope, cancellation, deduplication, stale
setup/source disclosures, failure redaction, cached attempts and MCP ownership.
No live API keys or institutional login are needed.
