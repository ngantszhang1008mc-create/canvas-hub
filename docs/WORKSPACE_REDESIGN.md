# Academic workspace

The default Overall workspace is a personal academic workbench of directly interactive widgets. Deadlines/tasks (including the optional Schedule), Continue and actionable Needs attention cards sit beside saved checklists and relevant course tools. Users pin, hide, reorder and configure explicit contextual conditions; existing component IDs, state and source evidence are shared with course/chat views. Grade/GPA planning is not automatically added, while an explicitly placed course score calculator is fully operable on Overall. See [Overall widgets](OVERALL_WIDGETS.md) for placement, rules and review contracts. Existing hash links remain valid.

Each assignment workspace has its own stored conversation, checklist progress, requirements and expandable source material. A course has a separate scoped conversation. Global Assistant remains available for cross-course questions. The left navigation can collapse; the optional Agent pane opens beside the work area. At phone widths navigation and Agent start closed. This is browser responsiveness, not OS window pinning.

The `>` launcher exposes relevant icon actions with hover/focus descriptions. Pointer/keyboard activation can run the selected action; touch offers description plus explicit activation. The first use of a provider/model/source scope displays what will be sent. Later actions using that acknowledged configuration can start directly. Changes to the disclosed setup require acknowledgment again. All tests use fake providers. Closing a pane does not reinterpret an in-flight request as belonging to another assignment. Stop uses the existing cancellation mechanism. Canvas writes still require the existing exact preview and user confirmation.

## Completion and shared state

Three independent facts are shown: checklist substep progress, the user's whole-assignment local completion marker, and observed Canvas submission/grade state. Completing one or every checklist step never submits work or marks the whole assignment done. Assignment, quiz and calendar mirrors share the explicit local completion marker. Migration 0010 retains legacy tasks and recognizes current/historical component-linked substeps; legacy undated markers without such evidence remain completion markers to preserve prior user choices.

Components have stable account-owned IDs and optional course/item links. Overall, course, assignment and drawer presentations refer to the same component. Scoped creation uses a stable purpose key to reuse prior work. A selected assignment/component/step, next action, view and conversation are saved per account. Switching assignments explicitly resets fields from the previous assignment.

## Source synchronization

Component definitions, source values and user state are distinct. `sources.bindings` select an account-owned local `item_id`, an allowlisted Canvas field, and a target path using stable member IDs, such as `buckets/proposal/max`. `sources.collection` declares assignment membership by course and creates stable `canvas:<local item ID>` members in a checklist section or score model. `sources.review_sources` identifies text/resource evidence used to interpret rules. Tools validate all of these; neither source text nor a binding can execute code. Existing safe formula parsing remains in use.

After an observation package successfully commits, deterministic source synchronization updates declared fields and reconciles new matching assignments without an LLM. It preserves checklist state, notes, hypothetical targets and explicit overrides. Missing/unavailable/invalid sources retain last-known values and report the limitation; they are not zero or deletion. Partial collection does not remove members. The source status includes actual observation freshness separately from the synchronization attempt time. This runs after collection; it does not change refresh cadence or claim push synchronization.

Explicit source overrides remain labeled and can return to Follow source. Text or attachment changes that could alter interpreted rules mark a component as needing review. Reparse is a user-triggered Agent action. Replacing a component's manually authored definition or consequential grading rules requires a reviewable proposal and user approval. Proposals reject changes against a stale base or changed review evidence. Source rules are not silently inferred from changed PDF text.

Relevant operations: `configure_component_sources`, `set_component_source_override`, `get_component`, `list_components`, `update_component` (definition proposals); HTTP component `sources`, `source-override`, and `approve` endpoints; workspace `conversation`, `action/prepare`, and `action/start` endpoints. Generic global Assistant settings remain the provider configuration entry point.

## Optional follow-up work

No new GPA engine, shared time-tracking ledger or OS always-on-top window is included. Existing grade projectors remain single-course planning tools. Existing hours entries retain their tracker context; Overall does not add study and service hours together. Automatic collection, desktop notifications and AI summaries remain opt-in under their existing settings. The optional Overall Schedule view is included; advanced calendar editing remains in the existing Planner.

Verification results and screenshots are reported with the implementation delivery; examples use synthetic Canvas data only.

## Synthetic review preview

From a source checkout with development dependencies installed (`uv sync --locked`), run `uv run python scripts/preview-demo.py --port 8731 --metadata /tmp/canvas-hub-demo.json`. The protected metadata file contains the loopback launch URL. The server creates a temporary synthetic account and uses a local fake provider, with collection unavailable and Canvas writes disabled. It does not reuse any saved Canvas Hub home or real credentials. It lasts only while that process and the cloud environment remain running; loopback is not a public/iOS URL. Use a supported cloud browser handoff to open it remotely.
