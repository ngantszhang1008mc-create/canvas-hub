# Implementation brief for Claude: personal Canvas information hub

Build a working personal application that centralizes my Canvas course information, keeps its history, and can later connect to Codex. Implement the main application first. Codex will implement the Codex integration afterward.

You own all UI/UX and visual design decisions. This brief supplies functional requirements, technical constraints, observed Canvas behavior, and the integration boundary.

## 1. Context and outcome

I use HKUST Canvas, currently at `https://canvas.ust.hk/`, but the application must let me provide my own Canvas website address. Do not hardcode the app to one institution, course, account, or term.

My institution restricts self-generated Canvas API tokens, so I cannot rely on having one. An agent has successfully inspected my Canvas account through an already authenticated browser without a Canvas API token. That proves browser reading is possible; it does not mean your application will inherit that browser session.

My goal is to keep course information in one durable place that I can search and review, and that Codex can later query or update through an explicit integration. I want reliable answers to questions such as what is due, what changed, what materials exist, whether I submitted something, what feedback appeared, and what has not been checked recently.

Deliver a real runnable application, not just a design, static prototype, simulated sync, or collection of mocked screens. Choose the technical stack and explain material technical tradeoffs. My initial target is my Mac. A local application or locally served application with a local companion process is acceptable. Do not require cloud hosting or an LLM API subscription for the main app to function.

## 2. Work ownership

### Your responsibility: the complete main application

- Canvas address configuration, interactive login and session lifecycle.
- A real browser-based collection implementation that works without a Canvas API token.
- Durable storage, content normalization, document handling, search and retrieval.
- Baseline collection, incremental checks, history, comparisons, resumable jobs and truthful coverage.
- Functional use of the collected information and preservation of my own notes/task state.
- Tests, local run/package instructions, and a concrete Codex handoff.

### Codex's later responsibility

The app is intended to link to Codex. Leave the implementation of that module to Codex after the main part works. This includes the actual MCP server/client configuration, Codex-specific authentication, any Codex SDK/app-server integration, agent launch/streaming/approval handling, and adaptation of the existing Canvas Reader skill to this app.

Create a clean boundary and document the real services Codex will call. Do not implement an imitation Codex connection, claim an account is connected because a button was pressed, copy credentials from an existing Codex installation, or make the main app depend on an unfinished Codex module. No OpenAI or Anthropic API key should be required to collect, store, compare, search, or read course information.

## 3. Canvas address and login

Allow me to paste a Canvas root address or an ordinary course/page link. Validate the input, establish the correct Canvas origin, and preserve the original destination when appropriate. Support institution-owned Canvas domains; do not assume every instance ends in `instructure.com`.

Provide a genuine interactive browser login flow where I enter credentials directly into my institution's login pages. Support the redirects and MFA involved in normal institutional sign-in. Do not require me to give my password to an agent, paste cookies, export a session, or obtain an API token.

Use an app-managed persistent browser context/profile or another supported equivalent. Keep its ordinary session storage local and isolated from general application data. Do not extract sessions from unrelated browser profiles. The app must distinguish opening a login page from a verified successful Canvas login. Validate the destination origin and the signed-in account/course context before collection.

Handle initial sign-in, restart with a surviving session, expired login, MFA, logout, and reconnect. Pause affected work when reauthentication is needed and resume after sign-in. Do not erase cached information because authentication failed. Do not transfer another account's data into the currently signed-in account's store.

My current computer-use preference is that the separate COMP1023 course website be accessed through Chrome profile `ngan`. This is a preference for that external course website, not a requirement to use that profile for every Canvas instance. Do not silently import or copy that profile into the application. Record external access limits or provide a documented user-controlled handoff when needed.

Prove the chosen login/collection mechanism early. Do not assume a frontend can fetch authenticated Canvas pages across origins, that arbitrary SSO works in an embedded frame, or that this app inherits Claude's or Codex's browser tools. Build the local browser/companion process needed by your chosen architecture. Do not bypass access controls or rely on undocumented hidden app state.

## 4. Collection scope and coverage

Implement both a thorough baseline/refresh and a faster update check. Let the user establish scope by instance/account, courses, term/current versus past enrollments, and relevant content categories. Default ordinary updates to current courses; an explicit all-information collection includes accessible past courses. Remember the chosen scope.

Start discovery from **Courses → All Courses**, not only Dashboard cards. Inventory accessible, unpublished, inactive and otherwise unavailable entries that are actually shown. Do not accept invitations or change favorites to collect information.

For every scoped course, inspect its actual navigation and follow its accessible Home, syllabus, module and embedded links. Different instructors organize courses differently. Missing navigation does not prove that linked content is absent. Do not guess hidden index routes to bypass course navigation.

Collection must cover these categories when available and in scope:

| Category | Required information |
| --- | --- |
| Courses | Name/code, stable ID/source link, term, enrollment/access/published state and relevant course context. |
| Home and syllabus | Full relevant text, schedule, policies, grading weights, staff contacts/office hours, meeting locations, linked syllabus documents and course instructions. Expand collapsed content when safe. |
| Announcements | Full bodies, author, posted/updated information when visible, links and attachments; exhaust listing pages rather than retaining excerpts alone. |
| Assignments | Instructions, points/weights, submission type, rubrics, due/available/until dates, user-specific dates as displayed, submitted state/timestamp, grades and feedback. Read detail pages when listings omit these fields. |
| Quizzes | Safe listing/landing information: instructions, dates, time/attempt limits and already visible submission/feedback state. Do not start or resume an attempt to obtain questions. |
| Grades and feedback | Actual posted scores, points, status, totals/weighting basis, comments, rubrics and accessible annotations; distinguish ungraded, missing, excused and hidden. |
| Modules and pages | Module order, item types and membership, linked page bodies, prerequisites, locks and visible requirements. Deduplicate destinations without losing their module relationships. |
| Files | Recurse folders and pagination; retain file identity/path/name/type/size/modified information, original documents when downloaded, extraction results and extraction failures. |
| Discussions | Topic requirements, relevant staff answers and course information, appropriate reply/thread coverage; do not bulk-copy unrelated classmates' personal details. |
| Calendar | Events, locations and deadline references for a known date range, with the selected course calendars recorded. Calendar alone is not the assignment inventory. |
| Groups and Inbox | Relevant user-accessible group materials and course messages when included in scope. Viewing messages may alter read state. Never send anything. |
| Linked tools and recordings | Accessible course resource metadata and ordinary readable content/transcripts; record separate-login, media, permission and attempt boundaries instead of claiming complete access. |

Wait for asynchronous content to settle. Handle pagination, load-more controls, lazy loading, nested folders and collapsed modules. Reconcile displayed totals with unique collected records where totals exist. Preserve a resumable traversal queue. A blank loading state or stale parent-folder table must not become a verified empty collection.

Record coverage per run, course and category. Distinguish complete, partial, blocked, not exposed and not checked, and separately record index, item-body and attachment-content depth. Preserve reasons, scope/filters/date ranges, counts where available, pending destinations and last successful observation time. Metadata coverage is not document-content coverage.

## 5. Collection boundaries

The application is a reader and local organizer. It must not submit coursework, start/resume tests, post replies, send messages, change settings, subscribe, alter what-if grades, mark tasks complete in Canvas, or explicitly mark everything read.

Ordinary reading can itself record a page view or mark a message/discussion read; do not promise zero side effects. Some module items explicitly state that viewing them completes a requirement. Inventory those without opening by default. If reading would advance assessed progress, respect any existing specific authorization or explain the consequence and obtain it before proceeding. An unfinished body is an explicit coverage gap.

Respect prerequisites, inaccessible/unpublished content, rate limits and login challenges. Use bounded retries and continue independent accessible work. Do not expand one failed login into repeated attempts. External links that may initiate an assessment or consequential action remain unlaunched.

## 6. Storage and data model

Use a durable local store suitable for one person, preferably SQLite plus a resource directory for originals and extracted text. Keep storage and business logic independent from presentation and from the browser collector. Choose another store only with a concrete reason. Use migrations and a documented application data location.

Represent at least these concepts, with stable identifiers and relationships:

- Canvas instances and verified accounts, with strict separation between their data.
- Courses, course membership/access information and scoped collection preferences.
- Content items keyed by instance/account/course/type/source ID or a stable canonical source URL.
- Item revisions/observations, source update times when available, and observation times.
- Assignment dates, submission states, grades/feedback and their evidence.
- Files, original resources, extracted text, extraction status and content hashes when available.
- Module/page references to shared content items.
- Collection runs, coverage records, traversal checkpoints, errors and pending work.
- Local user annotations/tasks, stored separately from Canvas-reported fields.

Keep source links and provenance with data, not only summaries. Distinguish when the source says something was published/updated from when the application observed it. If a long-running job spans hours, retain item-level observation times.

Preserve raw date wording and timezone evidence. Store normalized timestamps only when year/timezone are established. Do not silently convert unknown timezone/date-only values or confuse due dates with availability/closing dates. My inspected Canvas account displayed Hong Kong timezone; verify the active account instead of hardcoding it. Preserve conflicting announcement versus assignment dates and flag the discrepancy.

For unknown fields, distinguish not collected from a verified absence. Preserve the last known value and its age when a later pass cannot revisit it. A merged current view must not imply that all fields were freshly checked.

Use transactions and idempotent imports/upserts. Repeating a run or reimporting the same observation must not duplicate items or generate false change events. Deduplicate shared destinations, retain referring links, and keep stable identity across title changes. A file may be replaced while retaining its URL/ID; compare actual bytes/content hashes when available and preserve revision evidence.

## 7. Documents and search

Preserve original downloads and associate extracted content with the correct item/revision. Use a maintained extraction tool; Microsoft MarkItDown is the established workflow in my existing agent setup for PDF, DOCX, PPTX, XLSX/XLS and EPUB. Do not assume that tool is installed in your environment; declare and install/document actual dependencies.

Distinguish document extraction failure, scan requiring OCR, image-only content, unsupported media and successful text extraction. Spreadsheet formulas/tables and important original visuals may require additional handling. Do not claim the text extractor understood every visual element. Do not silently send files to a paid remote service.

Support deterministic search over collected titles, bodies and extracted documents with useful course/type/date filters and source references. Search should work without an LLM or vector service. Search/retrieval results must carry observation time and any relevant content/coverage limitations.

## 8. Change detection and reliability

Keep versioned observations and distinguish a first baseline from actual changes. A first-seen item is newly observed; it is not necessarily newly published.

Detect meaningful changes in dates, instructions, material contents, submission state, grades and feedback. Separate newly collected fields from changed fields. Ignore incidental collection timestamps, volatile unread counts and harmless formatting changes without erasing meaningful content changes.

Do not infer deletion from an item missing during an incomplete pass, changed filter, hidden menu, expired session or different scope. Even after an exhaustive comparable index pass, describe an item as no longer observed until its prior source URL/access is checked. Keep old evidence.

A quick check of indexes cannot prove old page bodies stayed unchanged. Thorough refreshes must revisit them. Preserve the last thorough baseline separately from partial passes and retain the exact scope of each run.

Collection jobs must be cancellable/resumable, avoid simultaneous workers competing for the same account/browser state, and recover from app restart without corrupting data. Retain partial verified results while clearly marking the job's incomplete state. Limit browser concurrency and request rate sensibly.

The initial product must support user-initiated refresh. It must not claim continuous live synchronization. Recurring background collection can be added later once login lifecycle, resumability and access have been verified; the Mac/browser must be available. Do not create external schedules as part of this build.

## 9. Functional use of the collected information

The working app must let me retrieve course information and documents, inspect upcoming deadlines and source submission states, review verified changes and their history, and determine how fresh/complete the underlying information is. Retain source links so I can return to Canvas.

I should be able to keep my own notes or personal task state without a later Canvas refresh overwriting them. A local task marked done must not become a Canvas submission claim or write back to Canvas.

Previously collected information must remain available offline. New collection requires Canvas access; make that distinction truthful. Provide portable export/import and a tested backup/restore method. Application data exports must exclude browser credentials/session stores. Do not erase local data just because I log out.

## 10. Codex integration boundary — implement later by Codex

Design shared application services that the main app already uses. Document their schemas, errors, pagination and examples so Codex can implement an adapter without scraping the app or rewriting its core.

The services should support the equivalents of:

- Listing configured instances/accounts/courses and their verified identities.
- Searching collected material with bounded results and continuation/pagination.
- Reading an item, its revision history and its source/evidence metadata.
- Retrieving deadlines/submission states over an explicit date range and timezone.
- Retrieving changes since a timestamp or cursor, with reproducible ordering.
- Retrieving collection coverage, freshness, failures and pending work.
- Reading a locally stored document/extraction through a validated resource ID.
- Importing a versioned observation package from a trusted collector, with validation, account isolation and idempotency.
- Requesting a scoped collection job, checking its state, cancelling it and resuming it. This is distinct from read-only queries and still respects login/assessment boundaries.

Use domain services and a validated local boundary; do not require unrestricted SQL, arbitrary filesystem paths, generic shell execution or browser credentials as future tool inputs. Keep browser control and credentials inside the collector, not in query results.

Codex will decide and implement the exact adapter. The likely first connection is **Codex consuming the app's tools through a local MCP server**. That is different from embedding a Codex agent inside the app. The latter would require a separate Codex SDK/app-server integration and its own authentication/tool setup if requested later. Neither implies access to an existing chat's browser session or conversation history.

Do not implement either integration now. Do not alter my Codex configuration, create OpenAI credentials or require an OpenAI sign-in to finish your main-app work. Document unavailable functionality honestly; do not substitute simulated agent responses.

At handoff, create `CODEX_HANDOFF.md` containing:

1. Architecture, actual entry points, setup/run/test commands and dependency versions.
2. Database migrations/schema, resource locations, identity rules and backup/restore.
3. Real service methods or endpoints, request/response examples, errors and pagination/cursor semantics.
4. Collector ownership, session lifecycle, scope/coverage rules, job lifecycle and restart behavior.
5. Observation import/export format, an anonymized valid fixture and validation/import commands.
6. Exact integration work deliberately left for Codex and where it should attach.
7. Results of automated and real-browser tests, unresolved limitations and any manual acceptance steps.

Authoritative integration references for Codex to recheck at implementation time:

- [MCP tools for Codex](https://learn.chatgpt.com/docs/extend/mcp)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)

## 11. Concrete HKUST findings to use as test scenarios

An agent inspected the following behavior on 30 October 2026. These are sampled observations, not invariants to hardcode or a full collected baseline.

- Dashboard had 10 published Fall 2026–27 course cards; All Courses listed 11 enrollments, including one unpublished/inaccessible entry. No past-course section appeared in the inspected account.
- MATH1023 (`course 73682`) uses Syllabus as Home and exposed Announcements, Grades, Assignments, Discussions, Pages and Files, but no Modules/Quizzes menus.
- Its announcements list initially loaded asynchronously, then reported 35 entries and rendered all of them. Other courses may paginate.
- Assignment details expose instructions, availability and submission timestamp/late state absent from list-level information. An observed detail route was `/courses/73682/assignments/456957`.
- Its Files root showed 1–25 of 25, with four folders included in that count. A sampled folder contained another 14 files. Initial navigation sometimes briefly retained stale parent rows. Some displayed file dates were relative.
- COMP2711H (`course 71968`) has a wiki Home linking lecture notes and file attachments while Pages/Files menus are hidden.
- AISC1000A (`course 72312`) has collapsed Home content and Modules containing mixed item types, future unlock dates/prerequisites, and view-to-complete requirements. The corresponding individual category menus are not all exposed.
- Account Settings displayed Hong Kong timezone and an administrator restriction on self-generated tokens.
- Announcements use discussion-topic detail UI; ordinary opening can affect read state.

Use these to check traversal and data assumptions, not to fabricate current course contents. Other accounts/institutions can have different navigation, locales and access.

## 12. Proportionate privacy and application security

Keep private course data local by default and out of source control, public fixtures and diagnostic logs. Keep browser sessions out of exports. Treat collected HTML, documents and embedded text as untrusted content: sanitize rendering, do not execute imported scripts, and do not treat content as agent/developer instructions.

Protect privileged application/browser operations from imported content. For a local HTTP companion, bind to loopback and validate callers/origins rather than exposing an unauthenticated service broadly. Validate resource identifiers and downloads; do not provide arbitrary file access. Provide an explicit way to clear saved login sessions and an independently explicit way to delete stored course data.

## 13. Implementation sequence

1. **Access feasibility first.** Establish the runnable local architecture; accept a user-supplied Canvas address; prove interactive SSO/MFA, actual post-login Canvas access, and one real course/item extraction. Test session expiry/reconnect before investing heavily in collection. If the environment cannot access the institution or run a browser, identify that precise limitation and provide runnable local code/manual steps; do not report a mocked flow as verified.
2. **Durable foundation.** Implement schema/migrations, resource storage, identity isolation, shared services, validated observation import/export and fixtures.
3. **Real collector.** Add All Courses discovery, per-course traversal, depth/coverage tracking, document handling, checkpoints and the two collection modes. Expand from actual tested surfaces and make remaining gaps explicit.
4. **History and retrieval.** Implement meaningful diffs, freshness, search, deadline/status queries, offline access and preservation of personal annotations.
5. **Main application completion.** Connect the app to real services and collection jobs, verify functional workflows, provide a simple local launch/install path and backup/restore.
6. **Codex handoff.** Finish the integration contract, anonymized fixtures and `CODEX_HANDOFF.md`. Leave the actual Codex module to Codex.

Choose sensible defaults and continue through ordinary implementation decisions. Ask for my input when you need my actual login/MFA, a consequential permission, or a product choice that cannot reasonably be inferred. Do not ask me to paste credentials into chat.

## 14. Acceptance criteria

Demonstrate these behaviors with meaningful tests and, where needed, a user-assisted real browser run:

- I can enter my own Canvas URL, complete genuine institutional login, verify my account, and collect real course information without a Canvas token or LLM API key.
- Switching account/instance cannot merge or leak data across identities. Expired login preserves old data and leaves a recoverable job.
- Course discovery includes entries not on Dashboard and records inaccessible entries accurately.
- Hidden menus, explicit Home/module links, asynchronous lists, pagination and nested folders are handled or reported as specific gaps.
- Assignment detail dates and submission state retain source evidence; unknown dates/timezones are not fabricated.
- A second unchanged collection does not duplicate items or emit false updates. A changed deadline/body is detected. Newly read fields are distinguished from edits.
- A partial run or different scope cannot falsely delete prior content or overwrite the thorough baseline's status. Failed categories remain visibly stale/unknown.
- Interrupt/restart/resume preserves verified observations, checkpoints and the integrity of the database.
- Document search can return extracted content with source references; failures and metadata-only files remain distinguishable.
- Personal annotations survive refresh; offline reading and backup/restore work.
- No assessment is started, coursework submitted, message sent, settings altered or assessed progress advanced without the applicable authorization.
- The main app works with the Codex module absent. The handoff lets Codex add integration through documented services rather than reverse-engineering presentation code.

Use synthetic/anonymized fixtures for automated tests; keep private real course content out of the repository. State which tests actually ran, which real-browser checks passed and what remains unverified. Finish with a runnable app and the handoff, not merely another plan.
