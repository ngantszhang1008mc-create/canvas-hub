# Overall widgets

Overall is a personal workbench. Deadlines/tasks (with the optional Schedule), Continue and Needs attention are configurable cards beside saved interactive tools. Checklist ticks, score-model sliders, grade-projector inputs and hours entries use the existing component renderer and `/components/{id}` state/undo/version API. No component is copied, reset or converted into a snapshot. Local substeps, whole-assignment completion and Canvas submission remain distinct.

Use the `>` toolbar to add/manage widgets. Each card has contextual icon actions with keyboard/hover labels and the existing touch confirmation behavior. Pin a card, keep it in the library (persistent hide), set a condition, widen it, or move it earlier/later with accessible buttons. Course/chat tool toolbars offer **Place on Overall**. The existing Trackers pin is separate and explicitly labeled. Cards retain their live DOM while placement/order changes, so editing isn't replaced by a refreshed snapshot. On narrow cards the score rows reflow and sliders remain visible.

## Persistence and defaults

Migration `0014_overall_widgets.sql` adds account-owned settings and placements; it depends on 0012 only. The runner applies each missing version, so it works with no 0013 and also if the separate adapter migration 0013 arrives later. Tables are additive, included in normal database backups and deleted with their account. Existing component data, versions, source bindings and Trackers pins are untouched. The three built-ins seed once per account. Existing generated tools remain in the library until explicitly placed; no automatic GPA/semester planning cards are added.

`mode` is `pinned`, `conditional` or `library`. A saved library placement suppresses automatic visibility until explicitly changed. Pinning can retain a prior rule for later reuse. Saving uses the placement version; ordering uses the board revision. Agent proposals carry an ID and base version and cannot replace an accepted placement. An explicit user save clears the pending proposal, including when the user chooses to keep the tool hidden. Only user-facing review endpoints can accept a proposal. Components stay under the existing account/source scope checks and SQLite owner triggers reject cross-account component references.

The library returns up to 500 recent components plus every explicitly placed component. Opening **Place on Overall** from an older tool requests that exact component as well. Existing tools remain accessible in their course/chat/Trackers views. The event picker lists up to 300 recently observed calendar items; the API can reference any known account-owned event ID.

## Restricted rules

Rules are structured data, evaluated locally when reading the board. There is no LLM call, automatic collection, freeform code or inferred urgency.

- `date_window`: inclusive `start` and `end` local dates (`YYYY-MM-DD`), an IANA `time_zone`, and a short user/evidence-backed `reason`. Optional `item_id` and `max_age_days` tie it to known evidence. A user-chosen window is labeled with the explicit reason and dates; it doesn't claim an official assessment deadline.
- `event_today`: a stored `calendar_event` `item_id`, IANA `time_zone`, and optional `max_age_days` (default 7; 1–90). Only its confirmed, timezone-aware `start_at` determines the day. Source-field freshness is checked separately from item freshness. The associated prepared checklist/tool is the actual editable component; no seminar/exam advice is invented.
- `active_work`: exactly one known `item_id` or `course_id`, matched against the explicit saved active workspace. This follows “Keep working here”, not browsing history or inferred intent.

Linked rule sources must belong to the account and, for course-linked components, the same course. Missing/deleted/unavailable/stale sources and unresolved event starts yield **unknown**, hide the conditional card and show a review count and explanation in the widget library. Pinned tools remain usable with their existing source-status/override controls. Conditions are evaluated on loading/refreshing Overall and after settings changes; a continuously open page does not poll. Daily checklists retain progress across days; recurrence/reset behavior is deliberately not inferred.

New rules use the account/widget default zone, which the user can configure in Manage widgets. Each time rule records its own zone; changing the default never rewrites accepted rules. Unknown zones are rejected, not silently treated as UTC. Date comparisons operate on local calendar dates, including 23/25-hour DST days.

## Interfaces

- `GET /api/accounts/{aid}/widgets` (`component_id` optionally includes one older tool)
- `PUT /widgets/{key}`: `{config:{mode,rule?,expanded?}, expected_version}`
- `PUT /widgets/order`: `{keys:[all saved keys exactly once], revision}`
- `PUT /widgets/time-zone`: `{time_zone}`
- `POST /widgets/{key}/review`: `{proposal_id, approve}`
- Agent tools: `list_overall_widgets` (read), `propose_overall_widget` (existing UI permission and scoped component checks). Proposal output directs the user to review in Overall. No automatic placement on tool creation.

## Synthetic review

Run `uv run python scripts/preview-widgets.py --port 8733 --metadata /tmp/widget-preview.json` from a source checkout. This uses a fresh temporary home, local fake AI and no collector. It doesn't replace the existing PR23 preview. The metadata contains a private loopback access URL, not a public deployment.

`canvas_hub/testing/widget_demo.py` records the actual input rules for a pinned daily checklist, a prepared checklist associated with today's synthetic seminar, and an HMAW score model visible during an explicit local date window. Data is synthetic and dates are derived at seeding. Tests exercise persistence/undo, concurrent version conflicts, proposal invalidation, source freshness, owner isolation, migration upgrade paths, HK midnight and US DST edges, plus direct editing/navigation/order/review at desktop and 380px in Light/Dark. Browser screenshot location can be set with `CANVAS_HUB_SCREENSHOTS`.
