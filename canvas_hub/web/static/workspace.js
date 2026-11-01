// Daily work and contextual workspaces. Rendering never starts an Agent request.
import { icon } from "./icons.js";

export function makeWorkspace(env) {
  const { h, mount, api, A, fmtDate, ago, componentView } = env;
  let sequence = 0;
  let localActive = null;
  const query = (params) => "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== ""));
  const itemId = (x) => x.item_id ?? x.id;
  const rowsOf = (c) => (c.spec?.sections || []).flatMap((s) => s.rows || []);
  const unavailable = (status) => Array.isArray(status?.unavailable) ? status.unavailable.length > 0 : !!status?.unavailable;
  const activeValue = (result) => result && Object.hasOwn(result, "active") ? result.active : result;
  const progressOf = (c) => {
    const rows = rowsOf(c);
    return { total: rows.length, done: rows.filter((r) => c.state?.done?.[r.id]).length };
  };
  const errorText = (e) => e?.message || "This information is unavailable.";
  function fieldLabel(component, target) {
    const parts = String(target || "Source").split("/");
    const leaf = parts.at(-1);
    const name = { max: "maximum", due: "due date", threshold: "pass mark", earned: "earned score" }[leaf] || leaf.replaceAll("_", " ");
    const group = parts[0] === "buckets" ? component.spec?.buckets?.find((r) => r.id === parts[1]) :
      parts[0] === "sections" ? rowsOf(component).find((r) => r.id === parts[3]) :
      parts[0] === "categories" ? component.spec?.categories?.flatMap((c) => c.items || []).find((r) => r.id === parts[3]) : null;
    return group ? `${group.label || group.name || "Item"} · ${name}` : name;
  }
  function notice(message, retry) {
    return h("div", { class: "workspace-notice", role: "status" }, h("span", null, message),
      retry ? h("button", { class: "sm", onclick: retry }, "Retry") : null);
  }
  async function remember(data) {
    const result = env.setActive ? await env.setActive(data) : await api(A("/workspace"), { method: "PUT", body: data });
    localActive = activeValue(result) ?? data;
    return localActive;
  }
  function progress(c) {
    const p = progressOf(c);
    return h("span", { class: "workspace-progress", title: "Checklist steps completed; separate from submission" },
      h("progress", { value: p.done, max: p.total || 1, "aria-label": `${p.done} of ${p.total} checklist steps complete` }),
      h("span", null, `${p.done}/${p.total} steps`));
  }

  // Mouse and keyboard activate directly; touch can inspect before confirming.
  function launcher(actions) {
    const id = `workspace-actions-${++sequence}`;
    let selected = null, executing = false;
    const detail = h("div", { class: "workspace-action-description", id: `${id}-description`, "aria-live": "polite" }, "Choose an action to see what it does.");
    const confirm = h("button", { class: "workspace-action-confirm", disabled: true });
    const options = h("div", { class: "workspace-action-options", role: "group", "aria-label": "Available actions" });
    const panel = h("div", { class: "workspace-action-popover", id, hidden: true, role: "dialog", "aria-label": "Workspace actions" }, options, detail, confirm);
    const toggle = h("button", { class: "workspace-launcher-toggle", title: "Workspace actions", "aria-label": "Workspace actions", "aria-expanded": "false", "aria-controls": id },
      h("span", { "aria-hidden": "true" }, ">"));
    const root = h("div", { class: "workspace-launcher" }, toggle, panel);
    function close(focus = false) {
      panel.hidden = true; toggle.setAttribute("aria-expanded", "false");
      if (focus) toggle.focus();
    }
    function choose(action, button) {
      selected = action;
      options.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      detail.replaceChildren(h("strong", null, action.label), h("p", null, action.description || action.label));
      confirm.textContent = action.confirm || action.label;
      confirm.disabled = executing;
    }
    async function execute(action) {
      if (!action || executing) return;
      executing = true; confirm.disabled = true;
      try { await action.run(); close(); }
      catch (e) { detail.replaceChildren(notice(errorText(e))); }
      finally { executing = false; confirm.disabled = false; }
    }
    for (const action of actions) {
      const button = h("button", { class: "workspace-action-icon", title: `${action.label}: ${action.description || action.label}`,
        "aria-label": action.label, "aria-pressed": "false", "aria-describedby": `${id}-description`, onclick: (event) => {
          choose(action, button);
          if (event.pointerType !== "touch" && !matchMedia("(hover: none) and (pointer: coarse)").matches) execute(action);
        } }, icon(action.icon || "message"));
      button.addEventListener("focus", () => choose(action, button));
      button.addEventListener("mouseenter", () => choose(action, button));
      options.append(button);
    }
    toggle.addEventListener("click", () => {
      const opening = panel.hidden;
      panel.hidden = !opening;
      toggle.setAttribute("aria-expanded", String(opening));
      if (opening) options.querySelector("button")?.focus();
    });
    confirm.addEventListener("click", async () => {
      if (!selected || confirm.disabled) return;
      await execute(selected);
    });
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); close(true); }
      if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && options.contains(document.activeElement)) {
        e.preventDefault();
        const buttons = [...options.querySelectorAll("button")];
        const index = buttons.indexOf(document.activeElement);
        buttons[(index + (e.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length].focus();
      }
      if (e.key === "Tab" && !panel.hidden) {
        if (e.shiftKey && document.activeElement === toggle) { e.preventDefault(); confirm.focus(); }
        else if (!e.shiftKey && document.activeElement === confirm) { e.preventDefault(); toggle.focus(); }
      }
    });
    // Aborting disconnected listeners prevents a new listener for every deadline row.
    const controller = new AbortController();
    document.addEventListener("click", (e) => {
      if (!root.isConnected) { controller.abort(); return; }
      if (!root.contains(e.target)) close();
    }, { signal: controller.signal });
    return root;
  }

  function sourceStatus(component) {
    const status = component.source_status;
    const overrides = Object.keys(component.source_overrides || {});
    if (!Object.keys(status || {}).length && !Object.keys(component.source_config || {}).length && !overrides.length && !component.pending_spec) return null;
    const fields = Array.isArray(status?.fields) ? status.fields : Object.entries(status?.fields || {}).map(([target, value]) => ({ target, ...(typeof value === "object" ? value : { state: value }) }));
    if (!fields.length) fields.push(...(component.source_config?.bindings || []));
    const needsReview = !!status?.needs_review || !!component.pending_spec;
    const summary = needsReview ? "Source changes need review" : unavailable(status) ? "Source unavailable" : overrides.length ? "Source linked · local edits" : "Source linked";
    const detail = h("details", { class: `workspace-source${needsReview || unavailable(status) ? " needs-attention" : ""}` },
      h("summary", null, summary, status?.last_success_at ? h("span", { class: "workspace-source-time" }, ` · checked ${ago(status.last_success_at)}`) : null));
    const actions = h("div", { class: "workspace-source-actions" });
    const run = (label, fn) => h("button", { class: "sm", onclick: async (e) => {
      const button = e.currentTarget; button.disabled = true;
      try { await fn(); } catch (error) { actions.append(notice(errorText(error))); }
      finally { button.disabled = false; }
    } }, label);
    if (component.pending_spec) {
      const proposed = component.pending_spec.spec;
      const oldRows = rowsOf(component);
      const nextRows = rowsOf({ spec: proposed });
      detail.append(h("p", { class: "small" }, `A proposed update is ready${nextRows.length ? `: ${nextRows.length} checklist steps (${oldRows.length} currently)` : ""}. Your current work stays in place until you approve it.`));
      if (nextRows.length) detail.append(h("ul", { class: "workspace-proposal" }, nextRows.map((r) => h("li", null, r.label))));
      if (env.approveSpec) actions.append(run("Approve source update", () => env.approveSpec(component)));
    }
    if (unavailable(status)) detail.append(h("p", { class: "small" }, "Stored values are available. Open the source or check collection to restore access."));
    if (fields.length) detail.append(h("ul", { class: "workspace-source-fields" }, fields.map((f) => h("li", null,
      h("span", null, f.target ? fieldLabel(component, f.target) : f.field || f.name || "Source", f.state ? ` · ${String(f.state).replaceAll("_", " ")}` : ""),
      f.item_id ? h("a", { href: `#/item/${f.item_id}` }, "Open source") : null))));
    if (overrides.length) detail.append(h("div", { class: "workspace-source-overrides" }, overrides.map((target) => h("div", { class: "spread small" },
      h("span", null, `${fieldLabel(component, target)} · edited locally`),
      env.followSource ? run("Follow source", () => env.followSource(component, target)) : null))));
    if (env.reparse && (component.source_config || component.pending_spec || needsReview)) actions.append(run("Reparse stored sources", () => env.reparse(component)));
    detail.append(actions);
    return detail;
  }

  async function allDeadlines() {
    const params = { start: "1960-01-01T00:00:00Z", end: "2100-01-01T00:00:00Z", status: "all", limit: 500, include_calendar: true };
    const data = await api(A("/deadlines") + query(params));
    const cursors = new Set();
    while (data.next_cursor && !cursors.has(data.next_cursor)) {
      cursors.add(data.next_cursor);
      const next = await api(A("/deadlines") + query({ ...params, cursor: data.next_cursor }));
      data.deadlines.push(...next.deadlines);
      data.next_cursor = next.next_cursor;
    }
    return data;
  }

  async function recentChanges() {
    const params = { since: new Date(Date.now() - 7 * 86400000).toISOString(), include_first_observed: false, kinds: "field_changed,source_unavailable", limit: 100 };
    const result = await api(A("/changes") + query(params));
    const cursors = new Set();
    while (result.has_more && result.next_cursor && !cursors.has(result.next_cursor)) {
      cursors.add(result.next_cursor);
      const next = await api(A("/changes") + query({ ...params, cursor: result.next_cursor }));
      result.events.push(...next.events); result.next_cursor = next.next_cursor; result.has_more = next.has_more;
    }
    return result;
  }

  async function overallView(main) {
    const loading = h("div", { class: "workspace-loading", role: "status" }, "Loading your workspace…");
    mount(main, loading);
    const results = await Promise.allSettled([
      allDeadlines(), api(A("/courses")), api(A("/freshness")), api(A("/workspace")),
      api(A("/components")), api(A("/annotations") + "?kind=task"), recentChanges(),
    ]);
    const [deadlines, courses, freshness, saved, components, annotations, changes] = results.map((r) => r.status === "fulfilled" ? r.value : null);
    if (!main.isConnected) return;
    localActive = saved?.active || null;
    const allComponents = components?.components || [];
    const courseList = courses?.courses || [];
    const zone = deadlines?.time_zone || Intl.DateTimeFormat().resolvedOptions().timeZone;
    const dayKey = (date) => {
      const parts = new Intl.DateTimeFormat("en-US", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(date));
      return ["year", "month", "day"].map((key) => parts.find((p) => p.type === key).value).join("-");
    };
    const today = dayKey(Date.now());
    const nextWeek = dayKey(Date.now() + 14 * 86400000);
    const taskRows = (annotations?.annotations || []).filter((a) => a.task_role !== "completion" && a.task_role !== "substep").map((a) => ({ ...a,
      localTask: true, item_id: a.item_id, title: a.title || a.body?.slice(0, 100) || "Untitled task", course_id: a.course_canvas_id,
      date: a.local_due ? { iso: a.local_due } : null, completed: a.task_state === "done" }));
    const entries = [...(deadlines?.deadlines || []), ...taskRows].sort((a, b) => String(a.date?.iso || "9999").localeCompare(String(b.date?.iso || "9999")));
    let filter = "upcoming", selectedCourse = "";
    const work = h("div", { class: "workspace-work" });
    const attention = h("section", { class: "workspace-attention", "aria-label": "Needs attention", hidden: true });
    const count = h("span", { class: "workspace-count", "aria-live": "polite" });
    const controls = h("div", { class: "workspace-filters", role: "group", "aria-label": "Filter work" });
    const buttons = new Map();
    for (const [key, label] of [["today", "Today"], ["upcoming", "Upcoming"], ["overdue", "Overdue"], ["all", "All"]]) {
      const button = h("button", { class: "workspace-filter", "aria-pressed": String(key === filter), onclick: () => { filter = key; draw(); } }, label);
      buttons.set(key, button); controls.append(button);
    }
    const courseSelect = h("select", { "aria-label": "Filter by course", onchange: (e) => { selectedCourse = e.target.value; draw(); } },
      h("option", { value: "" }, "All courses"), courseList.map((c) => h("option", { value: c.course_id }, c.course_code || c.title)));
    function entryRow(d) {
      const iso = d.date?.iso;
      const date = iso ? dayKey(iso) : null;
      const checklist = allComponents.find((c) => c.kind === "checklist" && String(c.item_id) === String(d.item_id));
      const source = d.localTask ? "Local task" : d.item_type === "calendar_event" ? "Calendar" : "Assignment";
      let canvasLabel = "";
      if (!d.localTask) {
        const sub = d.submission || {};
        canvasLabel = sub.excused ? "Excused" : sub.workflow_state === "graded" ? "Graded" : d.canvas_completed || sub.submitted_at || ["submitted", "pending_review"].includes(sub.workflow_state) ? "Submitted" :
          d.item_type === "calendar_event" ? "" : sub.state === "not_collected" || !d.submission ? "Status not collected" : "Not submitted";
      }
      const course = courseList.find((c) => String(c.course_id) === String(d.course_id));
      const row = h("li", { class: `workspace-work-row${date && date < today && !d.completed ? " is-overdue" : ""}`, dataset: { itemId: d.item_id || "", taskId: d.localTask ? d.id : "" } });
      const title = h("a", { class: "workspace-work-title", href: d.localTask ? `#/notes${d.item_id ? `?item_id=${d.item_id}` : ""}` : `#/item/${d.item_id}` }, d.title || "Untitled assignment");
      const meta = h("div", { class: "workspace-row-meta" },
        d.course_id ? h("a", { class: "workspace-course-label", href: `#/course/${d.course_id}` }, course?.course_code || course?.title || d.course_title || `Course ${d.course_id}`) : h("span", null, "Personal"),
        h("span", null, source),
        iso ? h("time", { datetime: iso, class: date < today && !d.completed ? "workspace-overdue" : "" }, date === today ? `Today · ${fmtDate(iso, { year: false })}` : fmtDate(iso, { year: false })) : h("span", null, "No due date"));
      const status = h("div", { class: "workspace-row-state" }, checklist ? progress(checklist) : null,
        canvasLabel ? h("span", { class: ["Submitted", "Graded", "Excused"].includes(canvasLabel) ? "workspace-submission" : "workspace-canvas-status" }, canvasLabel) : null,
        d.done_locally || d.localTask && d.completed ? h("span", { class: "workspace-local-done" }, "Done locally") : null,
        d.localTask && d.task_state === "blocked" ? h("span", { class: "workspace-overdue" }, "Blocked") : null);
      row.append(h("div", { class: "workspace-row-main" }, title, meta), status);
      if (d.localTask) row.append(h("button", { class: "workspace-row-action", "aria-label": `${d.completed ? "Reopen" : "Complete"} local task: ${d.title}`, title: "Changes only this local task", onclick: async (e) => {
        const button = e.currentTarget; button.disabled = true;
        try { await api(A(`/annotations/${d.id}`), { method: "PATCH", body: { task_state: d.completed ? "open" : "done" } }); d.completed = !d.completed; d.task_state = d.completed ? "done" : "open"; draw(); }
        catch (error) { row.append(notice(errorText(error))); button.disabled = false; }
      } }, icon(d.completed ? "clock" : "checklist")));
      return row;
    }
    function draw() {
      buttons.forEach((button, key) => button.setAttribute("aria-pressed", String(key === filter)));
      const visible = entries.filter((d) => {
        if (selectedCourse && String(d.course_id) !== selectedCourse) return false;
        if (filter === "all") return true;
        if (d.completed || d.canvas_completed || d.done_locally) return false;
        if (!d.date?.iso) return filter === "upcoming";
        const date = dayKey(d.date.iso);
        return filter === "today" ? date === today : filter === "overdue" ? date < today : date >= today && date <= nextWeek;
      });
      count.textContent = `${visible.length} ${visible.length === 1 ? "item" : "items"}`;
      mount(work, visible.length ? h("ul", { class: "workspace-work-list" }, visible.map(entryRow)) :
        h("div", { class: "workspace-empty" }, h("h3", null, filter === "today" ? "Nothing due today" : filter === "overdue" ? "No overdue work" : "No work in this view"),
          h("p", null, filter === "all" ? "Collected deadlines and your local tasks will appear here." : "Choose All to see all collected work, including completed items.")));
    }
    const continueZone = h("section", { class: "workspace-continue", "aria-label": "Continue", hidden: !localActive });
    if (localActive) {
      const activeItem = localActive.item || entries.find((d) => String(d.item_id) === String(localActive.item_id));
      const c = localActive.component || allComponents.find((c) => c.id === localActive.component_id);
      const step = rowsOf(c || {}).find((r) => r.id === localActive.step_id);
      const resume = localActive.view === "assistant" ? "Open Agent" : "Resume";
      continueZone.append(h("div", { class: "workspace-continue-label" }, "Continue"), h("div", { class: "workspace-continue-body" },
        h("a", { class: "workspace-work-title", href: `#/item/${localActive.item_id}` }, activeItem?.title || "Your active assignment"),
        localActive.next_action || step?.label ? h("p", null, localActive.next_action || step.label) : null,
        c?.kind === "checklist" ? progress(c) : null),
      h("a", { class: "btn workspace-resume", href: `#/item/${localActive.item_id}${localActive.view === "assistant" ? "?agent=1" : ""}` }, resume));
    }
    const issues = [];
    const overdue = entries.filter((d) => d.date?.iso && dayKey(d.date.iso) < today && !d.completed && !d.canvas_completed && !d.done_locally).length;
    if (overdue) issues.push(h("button", { class: "workspace-attention-link", onclick: () => { filter = "overdue"; selectedCourse = ""; courseSelect.value = ""; draw(); work.scrollIntoView({ block: "nearest", behavior: "smooth" }); } }, `${overdue} overdue ${overdue === 1 ? "item" : "items"}`, h("span", { "aria-hidden": "true" }, " →")));
    for (const c of allComponents.filter((c) => c.pending_spec || c.source_status?.needs_review || unavailable(c.source_status)).slice(0, 4)) {
      issues.push(h("a", { class: "workspace-attention-link", href: c.item_id ? `#/item/${c.item_id}` : c.course_id ? `#/course/${c.course_id}` : "#/trackers" },
        `${c.title} · ${unavailable(c.source_status) ? "source unavailable" : "review source changes"}`, h("span", { "aria-hidden": "true" }, " →")));
    }
    if (deadlines?.unresolved_dates?.length) issues.push(h("a", { class: "workspace-attention-link", href: "#/deadlines" }, `${deadlines.unresolved_dates.length} ${deadlines.unresolved_dates.length === 1 ? "date needs" : "dates need"} clarification →`));
    if (freshness?.items_not_currently_observed) issues.push(h("a", { class: "workspace-attention-link", href: "#/coverage" }, "Review unavailable Canvas sources →"));
    const changeLabels = { due_at: "Due date changed", start_at: "Date changed", body: "Requirements changed", description: "Requirements changed", syllabus_body: "Syllabus changed", rubric: "Rubric changed", score: "Score changed", grade: "Grade changed" };
    const important = (changes?.events || []).filter((event) => changeLabels[event.field] || event.kind === "source_unavailable");
    function excerpt(value) {
      if (value == null) return "not shown";
      if (value?.iso) return fmtDate(value.iso, { year: false });
      const raw = typeof value === "object" ? value.raw || (Array.isArray(value) ? value.map((r) => r.description || r.label || "").join("; ") : "Updated source value") : String(value);
      const clean = String(raw).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
      return clean.length > 90 ? clean.slice(0, 87) + "…" : clean;
    }
    for (const event of important.slice(-3).reverse()) {
      const label = event.kind === "source_unavailable" ? "Source unavailable" : changeLabels[event.field];
      issues.push(h("a", { class: "workspace-attention-link workspace-change", href: `#/item/${event.item_id}` },
        h("span", null, `${event.title || "Stored item"} · ${label} →`),
        event.kind !== "source_unavailable" ? h("span", { class: "workspace-change-excerpt" }, `${excerpt(event.old)} → ${excerpt(event.new)}`) : null));
    }
    if (important.length > 3) issues.push(h("a", { class: "workspace-attention-link", href: "#/changes" }, "View all source changes →"));
    if (issues.length) { attention.hidden = false; attention.append(h("h2", null, "Needs attention"), h("div", { class: "workspace-attention-list" }, issues)); }
    const health = h("div", { class: "workspace-health" }, h("span", { class: "workspace-health-dot", "aria-hidden": "true" }),
      h("a", { href: "#/collect", title: "View collection and data freshness" }, !freshness ? "Collection status unavailable" : freshness.newest_item_observation ? `Last read ${ago(freshness.newest_item_observation)}` : "Canvas has not been collected"));
    const failures = results.map((r, i) => r.status === "rejected" ? notice(`${["Work", "Courses", "Data freshness", "Continue state", "Checklists", "Local tasks", "Recent changes"][i]} unavailable: ${errorText(r.reason)}`, () => overallView(main)) : null).filter(Boolean);
    const root = h("div", { class: "workspace workspace-overall" },
      h("header", { class: "page-head workspace-page-head" }, h("div", null, h("h1", null, "Overall"), h("p", null, "Your work, across courses.")), health),
      failures, continueZone,
      h("section", { class: "workspace-today", "aria-label": "Today and upcoming work" },
        h("div", { class: "workspace-section-heading" }, h("h2", null, "Today & upcoming"), count),
        h("div", { class: "workspace-controls" }, controls, courseSelect), work), attention);
    draw(); mount(main, root);
  }

  function contentNodes(content) {
    return (Array.isArray(content) ? content.flat(Infinity) : [content]).filter(Boolean);
  }
  function contextualShell(main, scope, content) {
    const center = h("div", { class: "workspace-center" });
    const agentBody = h("div", { class: "workspace-agent-body" });
    const close = h("button", { class: "workspace-icon-button", title: "Close Agent", "aria-label": "Close Agent", onclick: async () => {
      pane.hidden = true; layout.classList.remove("agent-open");
      if (scope.item_id) try { await remember({ item_id: scope.item_id, view: "requirements" }); }
      catch (error) { center.append(notice(`Could not save the working view: ${errorText(error)}`)); }
    } }, "×");
    const pane = h("aside", { class: "workspace-agent", hidden: true, "aria-label": "Agent" },
      h("div", { class: "workspace-agent-heading" }, h("h2", null, "Agent"), close), agentBody);
    const layout = h("div", { class: "workspace workspace-context" }, center, pane);
    const showAgent = async (action) => {
      pane.hidden = false; layout.classList.add("agent-open");
      if (scope.item_id) await remember({ item_id: scope.item_id, view: "assistant" });
      if (action && env.startAction) await env.startAction(scope, action, agentBody);
      else if (env.openAgent) await env.openAgent(scope, agentBody);
      else mount(agentBody, h("a", { href: "#/assistant" }, "Open Assistant"));
      if (matchMedia("(max-width: 860px)").matches) pane.scrollIntoView({ block: "start", behavior: "smooth" });
    };
    const nodes = contentNodes(content);
    let heading = nodes.find((node) => node instanceof Element && node.matches(".page-head"));
    if (heading) center.append(heading);
    const toolbar = h("div", { class: "workspace-context-toolbar" });
    const source = h("div", { class: "workspace-context-sources" });
    const linked = h("section", { class: "workspace-linked", "aria-label": "Checklist" });
    const details = h("div", { class: "workspace-content" }, nodes.filter((node) => node !== heading));
    center.append(toolbar, linked, details, source);
    const actions = [
      ...(scope.item_id ? [{ id: "breakdown", icon: "checklist", label: "Break down assignment", description: "Read the stored assignment requirements and prepare a checklist with your Agent. Review the provider and sources before starting.", confirm: "Prepare breakdown", run: () => showAgent("breakdown") }] : []),
      { id: "sources", icon: "book", label: "View sources", description: "Inspect the stored requirements, attachments and linked source status in this workspace.", run: () => { source.querySelectorAll("details").forEach((d) => { d.open = true; }); (source.childElementCount ? source : details).scrollIntoView({ block: "start", behavior: "smooth" }); } },
      { id: "ask", icon: "message", label: "Ask Agent", description: "Open the conversation for this workspace. Your question is sent only when you choose Send.", confirm: "Open Agent", run: () => showAgent() },
      ...(!scope.item_id ? [{ id: "grading", icon: "chart", label: "Interpret grading", description: "Prepare a course grading model from stored syllabus sources. Review the provider and sources before starting.", confirm: "Prepare grading interpretation", run: () => showAgent("grading") }] : []),
    ];
    toolbar.append(h("span", { class: "workspace-context-label" }, scope.item_id ? "Assignment workspace" : "Course workspace"));
    if (scope.item_id) {
      const status = h("span", { class: "workspace-active-status", "aria-live": "polite" });
      const activate = h("button", { class: "sm workspace-activate", onclick: async () => {
        activate.disabled = true;
        try { await remember({ item_id: scope.item_id, view: "requirements" }); status.textContent = "Saved for Continue"; activate.textContent = "Active workspace"; }
        catch (e) { status.textContent = errorText(e); }
        finally { activate.disabled = false; }
      } }, "Keep working here");
      toolbar.append(status, activate);
    }
    toolbar.append(launcher(actions));
    mount(main, layout);
    return { layout, center, source, linked, details, showAgent };
  }

  function prepareReadableContent(details) {
    // Keep requirements readable while secondary evidence can be expanded in place.
    for (const card of [...details.querySelectorAll(".card")]) {
      const label = card.querySelector("h2")?.textContent?.trim() || "";
      if (!["Details", "History", "Attachments", "Linked items", "Document", "Notes & tasks", "Your notes", "Connections"].includes(label)) continue;
      if (card.closest("details")) continue;
      const disclosure = h("details", { class: "workspace-evidence" }, h("summary", null, label));
      card.replaceWith(disclosure); disclosure.append(card);
    }
    details.querySelectorAll(".rich").forEach((el) => el.classList.add("workspace-requirements"));
  }

  function renderLinked(target, c, scope, source) {
    const wrap = h("div", { class: "workspace-component", dataset: { componentId: c.id } });
    if (c.kind === "checklist" && scope.item_id) {
      const nextRows = rowsOf(c).filter((r) => !c.state?.done?.[r.id]);
      if (nextRows.length) {
        const step = h("select", { "aria-label": `Active step in ${c.title}` },
          h("option", { value: "" }, "Choose your next step"), nextRows.map((r) => h("option", { value: r.id }, r.label)));
        const state = localActive;
        if (String(state?.component_id) === String(c.id)) step.value = state.step_id || "";
        const stepStatus = h("span", { class: "workspace-active-status", "aria-live": "polite" });
        step.addEventListener("change", async () => {
          if (!step.value) return;
          const row = nextRows.find((r) => r.id === step.value);
          step.disabled = true;
          try {
            await remember({ item_id: scope.item_id, component_id: c.id, step_id: row.id, next_action: row.label, view: "requirements" });
            if (env.onStepActive) await env.onStepActive(c, row);
            stepStatus.textContent = "Next step saved";
          } catch (e) { stepStatus.textContent = errorText(e); }
          finally { step.disabled = false; }
        });
        wrap.append(h("div", { class: "workspace-next-step" }, step, stepStatus));
      }
    }
    wrap.append(componentView({ component: c.kind, view: c }));
    const status = sourceStatus(c);
    if (status) source.append(status);
    target.append(wrap);
  }

  async function assignmentView(main, item, content) {
    const scope = { item_id: Number(itemId(item)), ...(item.course_canvas_id ? { course_id: item.course_canvas_id } : {}) };
    const shell = contextualShell(main, scope, content);
    const due = (item.fields || []).find((f) => ["due_at", "start_at"].includes(f.field));
    const deadline = item.deadline_status || {};
    const submission = deadline.submission;
    const submitted = !!deadline.canvas_completed || !!submission?.submitted_at || ["submitted", "pending_review", "graded"].includes(submission?.workflow_state);
    const canvasLabel = submission?.excused ? "Excused" : submission?.workflow_state === "graded" ? "Graded" : submitted ? "Submitted" : submission && submission.state !== "not_collected" ? "Not submitted" : "Canvas status not collected";
    const localState = h("span", { class: "workspace-local-done", hidden: !deadline.done_locally }, "Done locally");
    const completion = h("button", { class: "sm workspace-local-completion", title: "Private local completion marker; Canvas submission is separate", onclick: async () => {
      completion.disabled = true;
      try {
        await api(A(`/deadlines/${scope.item_id}/local-completion`), { method: "PATCH", body: { completed: !deadline.done_locally } });
        deadline.done_locally = !deadline.done_locally; localState.hidden = !deadline.done_locally;
        completion.textContent = deadline.done_locally ? "Reopen locally" : "Mark done locally";
      } catch (error) { statusLine.append(notice(errorText(error))); }
      finally { completion.disabled = false; }
    } }, deadline.done_locally ? "Reopen locally" : "Mark done locally");
    const dueLabel = due?.value?.iso ? h("time", { datetime: due.value.iso }, `Due ${fmtDate(due.value.iso, { year: false })}`) : due?.value?.raw ? `Due ${due.value.raw} · date unresolved` : due && due.value === null ? "No due date" : "Due date not collected";
    const statusLine = h("div", { class: "workspace-deadline-status", "aria-label": "Assignment status" },
      h("span", null, dueLabel), h("span", { class: submitted ? "workspace-submission" : "workspace-canvas-status" }, canvasLabel), localState, completion);
    shell.center.querySelector(".workspace-context-toolbar").after(statusLine);
    prepareReadableContent(shell.details);
    try {
      const saved = activeValue(await (env.getActive ? env.getActive() : api(A("/workspace"))));
      if (!shell.layout.isConnected) return;
      localActive = saved;
    }
    catch { /* The checklist still works when resume state cannot be read. */ }
    async function loadLinked() {
      if (!shell.layout.isConnected) return;
      mount(shell.linked); mount(shell.source); shell.linked.hidden = false;
      const data = await api(A("/components") + query({ item_id: scope.item_id }));
      if (!shell.layout.isConnected) return;
      const checklists = (data.components || []).filter((c) => c.kind === "checklist");
      if (checklists.length) shell.linked.append(h("h2", null, "Your checklist"), h("p", { class: "workspace-section-note" }, "Steps track your progress. Canvas submission and local completion are shown separately."));
      checklists.forEach((c) => renderLinked(shell.linked, c, scope, shell.source));
      if (!checklists.length) shell.linked.hidden = true;
      const optional = (data.components || []).filter((c) => c.kind !== "checklist");
      shell.details.querySelector("[data-workspace-optional]")?.remove();
      if (optional.length) {
        const tools = h("details", { class: "workspace-evidence", dataset: { workspaceOptional: "true" } }, h("summary", null, "Optional tools"));
        optional.forEach((c) => renderLinked(tools, c, scope, shell.source)); shell.details.append(tools);
      }
    }
    const refresh = () => loadLinked().catch((e) => shell.linked.append(notice(`Checklist unavailable: ${errorText(e)}`, refresh)));
    await refresh();
    const controller = new AbortController();
    window.addEventListener("workspace-updated", () => { if (shell.layout.isConnected) refresh(); else controller.abort(); }, { signal: controller.signal });
    const agent = new URLSearchParams(location.hash.split("?")[1]).get("agent");
    if (agent === "1" || agent === "breakdown") await shell.showAgent(agent === "breakdown" ? "breakdown" : undefined);
  }

  async function courseView(main, course, content) {
    const scope = { course_id: String(course.course_id) };
    const shell = contextualShell(main, scope, content);
    // The original course view's grading editor remains available on demand.
    for (const card of [...shell.details.querySelectorAll(".grading-card")]) {
      const expand = h("details", { class: "workspace-evidence" }, h("summary", null, "Grade planning"));
      card.replaceWith(expand); expand.append(card);
    }
    async function loadLinked() {
      if (!shell.layout.isConnected) return;
      mount(shell.linked); mount(shell.source); shell.linked.hidden = false;
      shell.details.querySelectorAll("[data-workspace-optional]").forEach((el) => el.remove());
      const data = await api(A("/components") + query(scope));
      if (!shell.layout.isConnected) return;
      const direct = (data.components || []).filter((c) => !c.item_id);
      const checklists = direct.filter((c) => c.kind === "checklist");
      const assignmentChecklists = (data.components || []).filter((c) => c.item_id && c.kind === "checklist");
      if (checklists.length) shell.linked.append(h("h2", null, "Course checklist"));
      checklists.forEach((c) => renderLinked(shell.linked, c, scope, shell.source));
      if (assignmentChecklists.length) shell.linked.append(h("h2", null, "Assignment checklists"),
        h("ul", { class: "workspace-course-checklists" }, assignmentChecklists.map((c) => h("li", null,
          h("a", { href: `#/item/${c.item_id}` }, c.title || "Assignment checklist"), progress(c)))));
      shell.linked.hidden = !checklists.length && !assignmentChecklists.length;
      for (const [label, list] of [["Grade planning", direct.filter((c) => ["score_model", "grade_projector"].includes(c.kind))],
        ["Optional tools", direct.filter((c) => !["score_model", "grade_projector", "checklist"].includes(c.kind))]]) {
        if (!list.length) continue;
        const tools = h("details", { class: "workspace-evidence", dataset: { workspaceOptional: "true" } }, h("summary", null, label));
        list.forEach((c) => renderLinked(tools, c, scope, shell.source)); shell.details.prepend(tools);
      }
    }
    const refresh = () => loadLinked().catch((e) => shell.linked.append(notice(`Course tools unavailable: ${errorText(e)}`, refresh)));
    await refresh();
    const controller = new AbortController();
    window.addEventListener("workspace-updated", () => { if (shell.layout.isConnected) refresh(); else controller.abort(); }, { signal: controller.signal });
    const agent = new URLSearchParams(location.hash.split("?")[1]).get("agent");
    if (agent === "1" || agent === "grading") await shell.showAgent(agent === "grading" ? "grading" : undefined);
  }

  function installNavigation() {
    if (document.querySelector(".workspace-nav-toggle")) return;
    const sidebar = document.querySelector("#sidebar");
    if (!sidebar) return;
    document.body.classList.add("workspace-shell");
    const narrow = matchMedia("(max-width: 860px)");
    let open = !narrow.matches;
    const toggle = h("button", { class: "workspace-nav-toggle", title: "Toggle navigation", "aria-label": "Toggle navigation", "aria-controls": "sidebar" }, icon("sliders"));
    const scrim = h("button", { class: "workspace-nav-scrim", "aria-label": "Close navigation", hidden: true });
    function update() {
      document.body.classList.toggle("workspace-nav-closed", !open);
      document.body.classList.toggle("workspace-nav-open", open);
      toggle.setAttribute("aria-expanded", String(open));
      scrim.hidden = !open || !narrow.matches;
      sidebar.inert = !open;
    }
    toggle.addEventListener("click", () => { open = !open; update(); });
    scrim.addEventListener("click", () => { open = false; update(); toggle.focus(); });
    sidebar.addEventListener("click", (e) => { if (narrow.matches && e.target.closest("a[href]")) { open = false; update(); } });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && narrow.matches && open) { open = false; update(); toggle.focus(); } });
    narrow.addEventListener("change", () => { open = !narrow.matches; update(); });
    document.body.append(toggle, scrim); update();
  }
  return { overallView, assignmentView, courseView, launcher, sourceStatus, installNavigation };
}
