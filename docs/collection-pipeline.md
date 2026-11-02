# Collection pipeline

Core metadata uses the browser owner thread. Attachment downloads (two workers),
local document conversion/OCR (two disposable processes), and supplemental external
source verification (one worker) have separate queues and visible counters. Core
readiness does not mean that attachments, extraction, or verification are complete.
The account remains locked until the job finishes or is explicitly stopped.

`quick` is ordinary incremental sync. `baseline` establishes broad coverage but
reuses unchanged originals; `thorough` explicitly downloads originals again and
re-reads bodies. Existing quick-mode coverage qualifications remain visible.
Locked, view-gated, oversized, failed, and pending documents cannot establish full
attachment coverage. Unsupported formats remain stored with their extraction status.

`experimental_network_concurrency` is an account scope Boolean, off by default.
It allows at most two simultaneous API requests rather than one when background
file metadata needs revalidation. Metadata traversal itself remains sequential.
Binary downloads are separately bounded to two in both modes. All request starts
share a per-origin 350 ms budget, retry backoff, and circuit breaker. Discovered
file metadata is reused in memory so normal binary downloads do not hold the API
slot. After restart, missing download metadata is fetched after core readiness
unless Experimental overlap is enabled. No speedup is promised for this option.

Browser objects never cross threads. The browser owner verifies the account and
copies its storage state in memory; each background context owns its copy and is
disposed on its owner thread. Signed URLs and cookies are not written to queue
payloads. The durable queue stores file IDs and, after download, a content hash
checkpoint with safe metadata. A crash before that checkpoint may repeat an
in-flight GET; committed downloads and observations are reused on resume.

Pause, cancel, authentication failure, and shutdown stop admission and drain the
workers before database shutdown. Synchronous requests already in flight can take
up to their 45-second request timeout to return. Conversion has a 120-second job
timeout and kills its process tree on cancellation. Failed tasks can be explicitly
retried up to three attempts; pending work survives restart. Cancellation retains
observations and pending work but is terminal, while pause/interruption can resume.

Collection stats expose monotonic core/task/extraction and component refresh time,
per-kind queue wait for tasks enqueued in this process, request/retry/decoded-byte
counts, and original/extraction cache hits. They contain no body text or session
secrets. Queue waits before a restart are not reconstructed from wall clocks;
request metrics exclude browser subresources and compressed wire size. Disposable
converter/context startup has overhead; responsiveness is the measured goal.

## Synthetic validation

Run from an isolated checkout and data directory, using local fixtures only:

```sh
uv run pytest tests/test_collection_pipeline.py tests/test_component_dependencies.py -q -s
uv run pytest tests/test_client_pipeline.py tests/test_bounded_extraction.py -q
uv run pytest tests/test_pipeline_coverage.py tests/test_runner_lease.py -q
uv run pytest tests/test_collection_progress_ui.py -q
uv run pytest -q
```

The event-gated benchmark holds all eight binaries pending while asserting that
core metadata is usable, and checks that only two downloads start. A separate test
holds conversion while downloads finish. The component benchmark compares 50
bound components with one affected dependency and checks that only one refreshes.
Times are machine-local observations, not real-user Canvas speedup factors.

## Migration and integration

Migration `0016_component_dependencies.sql` adds indexed dependencies and a durable
component refresh outbox. It is independent of source-analysis migration 0015.
The migration runner checks applied versions individually, so a later missing 0015
still applies after 0016; tests cover both independent installation and this order.
Do not downgrade an upgraded data directory to an older build without restoring a
matching backup. Test in a new checkout and a new `CANVAS_HUB_HOME` directory; never
point preview tests at the user's normal app home or browser profile.

The importer retains `after_import` and `after_discovery`, and adds
`after_changed_import`; Hub binds only the latter to targeted component refresh.
Source-analysis integration must preserve all hooks. Other integration seams are
collection progress functions in `web/static/app.js`, account scope normalization,
and the existing `external_sources.refresh_registered` supplemental call. This
branch does not merge or modify the source-analysis branch or deploy anything.
