import { makeComponents } from "./components.js";
// Canvas Hub UI. Vanilla JS, no build step.
// Safety: everything is rendered with textContent except item HTML fields,
// which the server has already sanitized (and the CSP blocks scripts).

const S = {
  info: null,
  identities: [],
  accountId: null,
  account: null,
  session: null,
  jobPoll: null,
  sessionPoll: null,
};

// ---------- tiny DOM helpers ----------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v; // CSSOM: allowed under the strict CSP
      else if (k === "html") { // only for server-sanitized HTML
        el.innerHTML = v;
        el.querySelectorAll("a[href]").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
      }
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
const $ = (sel) => document.querySelector(sel);
function mount(el, ...kids) { el.replaceChildren(...kids.flat(Infinity).filter(Boolean)); }
function toast(msg, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
const store = {
  get(k) { try { return localStorage.getItem("ch." + k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem("ch." + k, v); } catch { /* ignore */ } },
};

// ---------- API ----------
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "Accept": "application/json" } };
  if (init.method !== "GET") init.headers["X-Canvas-Hub"] = "1";
  if (opts.headers) Object.assign(init.headers, opts.headers);
  if (opts.body !== undefined) {
    if (opts.raw) { init.body = opts.body; init.headers["Content-Type"] = opts.contentType || "application/octet-stream"; }
    else { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const err = new Error((data && (data.message || data.error)) || `HTTP ${r.status}`);
    err.code = data && data.error; err.detail = data && data.detail; err.status = r.status;
    throw err;
  }
  return data;
}
const A = (p) => `/api/accounts/${S.accountId}${p}`;
function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, v);
  const s = u.toString();
  return s ? "?" + s : "";
}
async function guard(fn, el) {
  try { return await fn(); }
  catch (e) {
    if (el) mount(el, errorBox(e));
    else toast(e.message, true);
    return null;
  }
}
function errorBox(e) {
  return h("div", { class: "banner bad" }, h("div", { class: "b-body" },
    h("div", { class: "b-title" }, "Something went wrong"), h("div", null, e.message),
    e.detail ? h("pre", { class: "text tiny" }, JSON.stringify(e.detail, null, 2)) : null));
}

// ---------- formatting ----------
const tz = () => (S.account && S.account.time_zone) || Intl.DateTimeFormat().resolvedOptions().timeZone;
function parseTs(s) { if (!s) return null; const d = new Date(s); return isNaN(d) ? null : d; }
function fmtDate(iso, opts = {}) {
  const d = parseTs(iso); if (!d) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, { timeZone: tz(), weekday: opts.weekday ? "short" : undefined, day: "numeric", month: "short",
      year: opts.year === false ? undefined : "numeric", hour: opts.time === false ? undefined : "2-digit", minute: opts.time === false ? undefined : "2-digit" }).format(d);
  } catch { return d.toISOString(); }
}
function fmtTime(iso) {
  const d = parseTs(iso); if (!d) return "";
  return new Intl.DateTimeFormat(undefined, { timeZone: tz(), hour: "2-digit", minute: "2-digit" }).format(d);
}
function dayKey(iso) {
  const d = parseTs(iso);
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz(), year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}
function dateParts(iso) {
  const d = parseTs(iso);
  const f = (o) => new Intl.DateTimeFormat(undefined, { timeZone: tz(), ...o }).format(d);
  const key = dayKey(iso);
  const today = dayKey(new Date().toISOString());
  const tomorrow = dayKey(new Date(Date.now() + 86400000).toISOString());
  const diff = Math.round((Date.parse(key) - Date.parse(today)) / 86400000);
  const rel = key === today ? "Today" : key === tomorrow ? "Tomorrow" : diff < 0 ? `${-diff} d ago` : `in ${diff} d`;
  return { dow: f({ weekday: "short" }), day: f({ day: "numeric" }), mon: f({ month: "short", year: "numeric" }), rel, today: key === today };
}
function ago(iso) {
  const d = parseTs(iso); if (!d) return "never";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 0) return "in " + dur(-s);
  if (s < 45) return "just now";
  return dur(s) + " ago";
}
function dur(s) {
  if (s < 3600) return Math.round(s / 60) + " min";
  if (s < 86400) return Math.round(s / 3600) + " h";
  if (s < 86400 * 60) return Math.round(s / 86400) + " d";
  return Math.round(s / 86400 / 30) + " mo";
}
function until(iso) {
  const d = parseTs(iso); if (!d) return "";
  const s = (d.getTime() - Date.now()) / 1000;
  return s >= 0 ? "in " + dur(s) : dur(-s) + " ago";
}
const TYPE_LABEL = { course: "Course", syllabus: "Syllabus", page: "Page", announcement: "Announcement", assignment: "Assignment",
  submission: "Submission", quiz: "Quiz", discussion: "Discussion", module: "Module", module_item: "Module item", folder: "Folder",
  file: "File", grade_summary: "Grades", calendar_event: "Calendar", group: "Group", conversation: "Inbox",
  external_tool: "Linked tool", external_link: "External link" };
const CAT_LABEL = { courses: "Courses", home: "Home & syllabus", announcements: "Announcements", assignments: "Assignments",
  quizzes: "Quizzes", grades: "Grades", modules: "Modules", pages: "Pages", files: "Files", discussions: "Discussions",
  calendar: "Calendar", groups: "Groups", inbox: "Inbox", external_tools: "Linked tools", linked_content: "Linked content" };
const STATUS_CLASS = { complete: "ok", partial: "warn", blocked: "bad", not_exposed: "muted", not_checked: "muted" };
const JOB_CLASS = { completed: "ok", completed_with_gaps: "warn", running: "info", queued: "info", needs_login: "bad",
  paused: "muted", interrupted: "warn", cancelled: "muted", cancel_requested: "muted", failed: "bad" };
const JOB_LABEL = { completed: "Completed", completed_with_gaps: "Completed with gaps", running: "Running", queued: "Queued",
  needs_login: "Needs sign-in", paused: "Paused", interrupted: "Interrupted", cancelled: "Cancelled",
  cancel_requested: "Cancelling", failed: "Failed" };
const EVENT_LABEL = { first_observed: "First seen", field_changed: "Changed", field_added: "Newly read",
  not_in_latest_index: "Not in latest list", source_unavailable: "Source unavailable", reobserved: "Seen again" };
const EVENT_CLASS = { first_observed: "muted", field_changed: "warn", field_added: "info", not_in_latest_index: "bad",
  source_unavailable: "bad", reobserved: "ok" };
const pill = (cls, text, title) => h("span", { class: "pill " + cls, title }, text);

function dateVal(v) {
  if (v === null) return h("span", { class: "muted" }, "none (as shown by Canvas)");
  if (v && typeof v === "object" && "raw" in v) {
    if (v.iso) return h("span", { title: `source: ${v.raw} (${v.source})` }, fmtDate(v.iso, { weekday: true }), " ", h("span", { class: "muted small" }, until(v.iso)));
    return h("span", { title: v.note || "" }, v.raw, " ", pill("warn", "unresolved", v.note || "time zone or year not established"));
  }
  return String(v);
}

function submissionPill(s) {
  if (!s) return null;
  if (s.state === "not_collected") return pill("muted", "Status not collected");
  if (s.excused) return pill("info", "Excused");
  if (s.missing) return pill("bad", "Missing");
  const graded = s.workflow_state === "graded";
  if (graded && s.grade_visible) return pill("ok", `Graded${s.score !== null && s.score !== undefined ? " · " + s.score : ""}`);
  if (graded && !s.grade_visible) return pill("muted", "Graded · score hidden");
  if (s.submitted_at || s.workflow_state === "submitted" || s.workflow_state === "pending_review")
    return pill(s.late ? "warn" : "ok", s.late ? "Submitted late" : "Submitted");
  if (s.workflow_state === "unsubmitted") return pill("muted", "Not submitted");
  return pill("muted", s.workflow_state || "unknown");
}

function fieldValue(name, f, item) {
  const v = f.value;
  if (f.kind === "date") return dateVal(v);
  if (f.kind === "html") return v ? h("div", { class: "rich", html: v }) : h("span", { class: "muted" }, v === null ? "none" : "(empty)");
  if (v === null) return h("span", { class: "muted" }, "none");
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v !== "object") return String(v);
  if (name === "comments" && Array.isArray(v)) return v.length ? h("ul", { class: "list" }, v.map((c) => h("li", null, h("div", { class: "strong" }, c.author || "?"), h("div", null, c.comment), h("div", { class: "li-meta" }, c.created_at && c.created_at.iso ? fmtDate(c.created_at.iso) : "")))) : h("span", { class: "muted" }, "no comments");
  if (name === "staff_replies" && Array.isArray(v)) return v.length ? h("ul", { class: "list" }, v.map((c) => h("li", null, h("div", { class: "strong" }, c.author || "Staff"), h("div", { class: "rich", html: c.message || "" }), h("div", { class: "li-meta" }, c.posted_at && c.posted_at.iso ? fmtDate(c.posted_at.iso) : "")))) : h("span", { class: "muted" }, "no staff replies");
  if (name === "rubric" && Array.isArray(v)) return h("table", { class: "t" }, h("tbody", null, v.map((r) => h("tr", null, h("td", null, r.description, r.long_description ? h("div", { class: "muted small" }, r.long_description) : null), h("td", { class: "num nowrap" }, r.points, " pts")))));
  if (name === "navigation" && Array.isArray(v)) return h("div", { class: "chips" }, v.map((n) => h("span", { class: "pill muted plain" }, n.label)));
  if (name === "items" && Array.isArray(v)) return h("ol", null, v.map((i) => h("li", null, i.title, " ", h("span", { class: "muted small" }, i.type))));
  if (name === "messages" && Array.isArray(v)) return h("ul", { class: "list" }, v.map((m) => h("li", null, h("div", { class: "strong" }, m.author || "?"), h("div", null, m.body))));
  return h("code", { class: "wrap-anywhere" }, JSON.stringify(v));
}

// ---------- shell ----------
function setActiveNav(view) {
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
}

function currentInstance() {
  if (!S.account) return S.identities[0] || null;
  return S.identities.find((i) => i.id === S.account.instance_id) || null;
}

function renderTopbar() {
  const inst = currentInstance();
  const sess = inst ? inst.session : null;
  const st = sess ? sess.status : "unknown";
  const label = { signed_in: "Signed in", login_open: "Waiting for sign-in", expired: "Session expired",
    signed_out: "Not signed in", error: "Sign-in check failed", unknown: "Sign-in not checked" }[st] || st;
  const cls = { signed_in: "ok", login_open: "info", expired: "bad", signed_out: "muted", error: "bad" }[st] || "muted";
  const kids = [];
  if (inst) {
    kids.push(h("div", { class: "row" }, h("span", { class: "strong" }, inst.label || inst.origin),
      pill(cls, label, sess && sess.message ? sess.message : "")));
    if (sess && sess.status === "signed_in" && sess.display_name)
      kids.push(h("span", { class: "muted small" }, `as ${sess.display_name}`));
  }
  kids.push(h("div", { class: "grow" }));
  if (inst && st !== "signed_in") {
    kids.push(h("span", { class: "muted small" }, "Showing stored data. New collection needs Canvas access."));
    if (st === "unknown") kids.push(h("button", { class: "ghost", onclick: () => refreshSession(true, true) }, "Check sign-in"));
    kids.push(h("button", { class: "primary", onclick: () => openLogin(inst.id) }, st === "login_open" ? "Show sign-in window" : "Sign in"));
  }
  if (inst && st === "signed_in" && S.accountId)
    kids.push(h("button", { onclick: () => startJob("quick") }, "Quick check"));
  if (S.accountId) kids.push(bell());
  mount($("#topbar"), kids);
}

// ---------- change bell ----------
let bellEl = null, bellFeed = null, bellLastUnread = 0;
function bell() {
  if (!bellEl) {
    const btn = h("button", { class: "ghost bell", "aria-haspopup": "dialog", "aria-label": "What's new" }, "🔔", h("span", { class: "bell-n", hidden: true }));
    const panel = h("div", { class: "bell-panel", hidden: true, role: "dialog", "aria-label": "What's new" });
    btn.addEventListener("click", (e) => { e.stopPropagation(); panel.hidden = !panel.hidden; if (!panel.hidden) drawBell(); });
    document.addEventListener("click", (e) => { if (!bellEl.contains(e.target)) panel.hidden = true; });
    bellEl = h("div", { class: "bell-wrap" }, btn, panel);
    pollBell();
    setInterval(pollBell, 60000);
  }
  return bellEl;
}
async function pollBell() {
  if (!S.accountId) return;
  const f = await api(A("/notifications") + qs({ limit: 30 })).catch(() => null);
  if (!f) return;
  bellFeed = f;
  const n = bellEl.querySelector(".bell-n");
  n.hidden = !f.unread; n.textContent = f.unread > 99 ? "99+" : String(f.unread);
  bellEl.querySelector(".bell").setAttribute("aria-label", f.unread ? `What's new: ${f.unread} unread` : "What's new");
  if (f.unread > bellLastUnread && store.get("desktopNotify") === "1" && "Notification" in window && Notification.permission === "granted") {
    const first = f.items.find((x) => x.unread);
    if (first) new Notification("Canvas Hub", { body: `${first.headline}: ${first.title}${f.unread > 1 ? ` (+${f.unread - 1} more)` : ""}` });
  }
  bellLastUnread = f.unread;
  const due = await api(A("/reminders/due"), { method: "POST" }).catch(() => null);
  for (const r of (due && due.reminders) || []) {
    toast(`⏰ ${r.title}`);
    if (store.get("desktopNotify") === "1" && "Notification" in window && Notification.permission === "granted") new Notification("Canvas Hub reminder", { body: r.title });
  }
  if (!bellEl.querySelector(".bell-panel").hidden) drawBell();
}
function drawBell() {
  const panel = bellEl.querySelector(".bell-panel");
  const f = bellFeed || { items: [], unread: 0 };
  const desk = h("input", { type: "checkbox", checked: store.get("desktopNotify") === "1" ? true : null });
  desk.addEventListener("change", async () => {
    if (desk.checked && "Notification" in window && Notification.permission !== "granted") {
      const p = await Notification.requestPermission();
      if (p !== "granted") { desk.checked = false; toast("The browser did not allow notifications", true); return; }
    }
    store.set("desktopNotify", desk.checked ? "1" : "0");
  });
  mount(panel,
    h("div", { class: "spread" }, h("strong", null, "What's new"),
      f.unread ? h("button", { class: "sm ghost", onclick: async () => { const r = await guard(() => api(A("/notifications/seen"), { method: "POST", body: { up_to: f.latest_change_id } })); if (r) { bellFeed = r; pollBell(); } } }, "Mark all read") : null),
    f.items.length ? h("ul", { class: "bell-list" }, f.items.map((x) => h("li", { class: x.unread ? "unread" : "" },
      h("a", { href: `#/item/${x.item_id}`, onclick: () => { panel.hidden = true; } }, h("div", { class: "small strong" }, x.headline, " · ", x.title || ""),
        h("div", { class: "tiny muted" }, [x.course, x.detail, ago(x.when)].filter(Boolean).join(" · ")))))) :
      h("div", { class: "small muted", style: "padding:10px 0" }, "Nothing new since the last collection."),
    h("label", { class: "check tiny", style: "margin-top:8px" }, desk, "Desktop notifications while Canvas Hub is open"),
    h("a", { class: "tiny", href: "#/collect" }, "Automatic refresh settings →"));
}

function renderAccountSwitch() {
  const accounts = S.identities.flatMap((i) => i.accounts.map((a) => ({ ...a, instLabel: i.label || i.origin })));
  if (accounts.length <= 1) {
    mount($("#account-switch"), accounts.length ? h("div", { class: "small muted wrap-anywhere" },
      accounts[0].display_name || accounts[0].canvas_user_id, " · ", accounts[0].instLabel) : null);
    return;
  }
  const sel = h("select", { "aria-label": "Account", onchange: (e) => { selectAccount(Number(e.target.value)); route(); } },
    accounts.map((a) => h("option", { value: a.id, selected: a.id === S.accountId ? true : null }, `${a.display_name || a.canvas_user_id} · ${a.instLabel}`)));
  mount($("#account-switch"), sel);
}

function renderFoot() {
  const v = S.info ? `v${S.info.version}` : "";
  const coll = S.info && S.info.collection_available ? "Collector ready" : "Collector not available in this process";
  mount($("#sidebar-foot"), h("div", null, v), h("div", null, coll), h("div", { class: "tiny" }, "Local only. Read-only toward Canvas unless you confirm a write."));
}

function selectAccount(id) {
  S.accountId = id;
  store.set("account", String(id));
  for (const i of S.identities) for (const a of i.accounts) if (a.id === id) S.account = a;
}

async function loadIdentities() {
  const d = await api("/api/identities");
  S.identities = d.instances;
  const all = S.identities.flatMap((i) => i.accounts);
  const remembered = Number(store.get("account"));
  if (all.length) {
    const pick = all.find((a) => a.id === (S.accountId || remembered)) || all[0];
    selectAccount(pick.id);
  } else { S.accountId = null; S.account = null; }
  renderAccountSwitch(); renderTopbar();
}

async function refreshSession(check = true, launch = false) {
  const inst = currentInstance();
  if (!inst) return;
  try {
    const s = await api(`/api/instances/${inst.id}/session${qs({ check, launch })}`);
    const before = inst.session && inst.session.status;
    inst.session = s;
    if (s.status === "signed_in" && before !== "signed_in") {
      await loadIdentities();
      if (location.hash.startsWith("#/setup")) route();
    }
    renderTopbar();
  } catch { /* browser unavailable */ }
}

function scheduleSessionPoll() {
  clearInterval(S.sessionPoll);
  const inst = currentInstance();
  const fast = inst && inst.session && inst.session.status === "login_open";
  S.sessionPoll = setInterval(() => refreshSession(true), fast ? 2500 : 60000);
}

async function openLogin(iid) {
  const s = await guard(() => api(`/api/instances/${iid}/login`, { method: "POST" }));
  if (!s) return;
  const inst = S.identities.find((i) => i.id === iid);
  if (inst) inst.session = s;
  toast(s.status === "signed_in" ? "Already signed in (verified with Canvas)" : "Sign in using the browser window that just opened.");
  renderTopbar(); scheduleSessionPoll();
  if (location.hash.startsWith("#/setup")) route();
}

async function startJob(mode, scope) {
  const j = await guard(() => api(A("/jobs"), { method: "POST", body: { mode, scope } }));
  if (j) { toast(`${mode === "quick" ? "Quick check" : mode === "baseline" ? "Baseline" : "Thorough refresh"} queued (job ${j.id})`); location.hash = "#/collect"; }
}

// ---------- router ----------
const VIEWS = {};
let lastView = null;          // the view route() drew last
async function route() {
  const hash = location.hash || "#/overview";
  const [path, query] = hash.slice(2).split("?");
  const parts = path.split("/");
  const params = new URLSearchParams(query || "");
  clearInterval(S.jobPoll);
  clearInterval(assistantPoll);
  document.body.classList.remove("chat-focus");      // only the assistant pages use focus mode; they turn it back on
  let view = parts[0] || "overview";
  if (!S.accountId && !["setup", "settings"].includes(view)) view = "setup";
  setActiveNav(view === "course" ? "courses" : view === "item" ? "" : view);
  // Switching between chats (or the assistant's tabs) keeps the page where it is: the old page stays on screen
  // until the new one is drawn, then the scroll position is put back.
  const keep = view === "assistant" && lastView === "assistant";
  lastView = view;
  const scrollY = window.scrollY;
  // Each page gets a fresh #main. A page still loading from before keeps the old, detached element, so when its
  // data arrives late it draws there instead of over the page the user has moved on to.
  const old = $("#main");
  const main = old.cloneNode(false);
  if (!keep) main.classList.remove("fill");
  delete main.dataset.keepScroll;                    // the clone copies it from the page before
  const lists = keep ? [...old.querySelectorAll(".conv-scroll, .chat-scroll")].map((el) => [el, el.scrollTop]) : [];
  old.replaceWith(main);
  if (keep) {
    main.append(...old.childNodes);
    for (const [el, top] of lists) el.scrollTop = top;          // moving them resets where they were scrolled to
    for (const el of main.children) el.inert = true;            // shown, but nothing sends to the old chat
    main.dataset.keepScroll = String(scrollY);
  }
  else mount(main, h("div", { class: "muted" }, "Loading…"));
  const fn = VIEWS[view] || VIEWS.overview;
  try { await fn(main, parts.slice(1), params); }
  catch (e) { mount(main, errorBox(e)); }
  if (keep && main.isConnected) window.scrollTo(0, scrollY);
  if (main.isConnected) main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);

// Assistant focus mode: the window holds just the chat list, the chat, its tabs and the composer (remembered in this browser).
// The page's toggle buttons are kept here, so Esc pressed while the page is still drawing updates them too.
let focusButtons = [];
function setChatFocus(on, buttons = focusButtons) {
  focusButtons = buttons;
  store.set("chatFocus", on ? "1" : "0");
  document.body.classList.toggle("chat-focus", on);
  for (const b of buttons) { b.textContent = on ? "Exit focus" : "⤢ Focus"; b.setAttribute("aria-pressed", on ? "true" : "false"); }
  window.dispatchEvent(new Event("resize"));          // the chat refits to the space it now has
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.body.classList.contains("chat-focus") && !document.querySelector(".cmp.fullscreen")) setChatFocus(false);
});

function pageHead(title, sub, ...actions) {
  return h("div", { class: "page-head" }, h("div", null, h("h1", null, title), sub ? h("p", null, sub) : null),
    actions.length ? h("div", { class: "row" }, actions) : null);
}
const empty = (title, text, ...actions) => h("div", { class: "empty" }, h("div", { class: "e-title" }, title), h("div", null, text),
  actions.length ? h("div", { class: "row", style: "justify-content:center;margin-top:12px" }, actions) : null);

// ---------- setup ----------
VIEWS.setup = async (main) => {
  const inst = S.identities[0];
  const sess = inst ? inst.session : null;
  const signedIn = sess && sess.status === "signed_in";
  const input = h("input", { type: "url", placeholder: "https://canvas.your-school.edu/ (or any course link)", style: "flex:1;min-width:260px",
    value: inst ? inst.entry_url || inst.origin : "" });
  const addBtn = h("button", { class: "primary", onclick: async () => {
    const r = await guard(() => api("/api/instances", { method: "POST", body: { address: input.value } }));
    if (r) { await loadIdentities(); route(); }
  } }, inst ? "Update address" : "Save address");
  const steps = h("ol", { class: "setup-steps" },
    h("li", { class: inst ? "done" : "current" }, h("h3", null, "Your Canvas address"),
      h("p", { class: "muted small" }, "Paste your Canvas home page or any course/page link. Institution-owned domains work; the original link is kept as the sign-in destination."),
      h("div", { class: "row" }, input, addBtn),
      inst ? h("div", { class: "small", style: "margin-top:6px" }, "Canvas origin: ", h("code", null, inst.origin)) : null),
    h("li", { class: signedIn ? "done" : inst ? "current" : "" }, h("h3", null, "Sign in through your institution"),
      h("p", { class: "muted small" }, "A separate browser window opens on your institution's normal login page (SSO and MFA work as usual). Type your credentials there; Canvas Hub never sees your password and needs no API token. Keep that window open (minimised is fine) while collecting: closing it can end your Canvas session."),
      inst ? h("div", { class: "row" },
        h("button", { class: signedIn ? "" : "primary", onclick: () => openLogin(inst.id) }, signedIn ? "Open browser window" : "Open sign-in window"),
        h("button", { class: "ghost", onclick: () => refreshSession(true, true).then(route) }, "Check again"),
        sess ? h("span", { class: "small muted" }, sess.message || "") : null) : null,
      signedIn ? h("div", { class: "banner ok", style: "margin-top:10px" }, h("div", { class: "b-body" },
        h("div", { class: "b-title" }, `Verified: ${sess.display_name || sess.canvas_user_id}`),
        h("div", { class: "small" }, `Canvas confirmed this account (user ${sess.canvas_user_id}). Time zone: ${sess.time_zone || "not reported — dates without an explicit offset will stay unresolved"}.`,
          sess.entry_course_accessible === false ? " The course in your link is not accessible to this account." : ""))) : null),
    h("li", { class: signedIn ? "current" : "" }, h("h3", null, "Collect a baseline"),
      h("p", { class: "muted small" }, "Starts from Courses → All Courses and follows each course's own navigation and links. Reading only: nothing is submitted, posted, or sent. Items that complete a module requirement when viewed are listed but not opened."),
      signedIn && S.accountId ? h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => startJob("baseline") }, "Start baseline collection"),
        h("a", { href: "#/collect", class: "btn" }, "Adjust scope first")) : null));
  mount(main, pageHead("Set up Canvas Hub", "Three steps. Everything stays on this computer."), h("div", { class: "card" }, steps));
};

// ---------- overview ----------
VIEWS.overview = async (main) => {
  const now = new Date();
  const end = new Date(now.getTime() + 14 * 86400000);
  const [fresh, dl, ch, cov, disc, jobs] = await Promise.all([
    api(A("/freshness")),
    api(A("/deadlines") + qs({ start: now.toISOString(), end: end.toISOString(), include_calendar: false })),
    api(A("/changes") + qs({ limit: 200, include_first_observed: false })),
    api(A("/coverage")),
    api(A("/discrepancies")),
    api(A("/jobs") + qs({ limit: 3 })),
  ]);
  const dash = await api(A("/dashboard")).catch(() => null);
  const gaps = cov.records.filter((r) => ["partial", "blocked"].includes(r.latest.status));
  const active = jobs.jobs.find((j) => ["running", "queued", "needs_login", "paused", "interrupted"].includes(j.state));
  const recent = ch.events.slice(-8).reverse();
  const head = pageHead("Overview", `Information as of ${fresh.newest_item_observation ? fmtDate(fresh.newest_item_observation) : "—"} · times shown in ${tz()}`);
  const stats = h("div", { class: "grid cols-4" },
    h("div", { class: "card stat" }, h("span", { class: "label" }, "Due in 14 days"), h("span", { class: "value" }, dl.deadlines.length),
      h("span", { class: "hint" }, `${dl.deadlines.filter((d) => d.submission && !d.submission.submitted_at && !d.submission.excused && d.submission.workflow_state !== "graded").length} not yet submitted`)),
    h("div", { class: "card stat" }, h("span", { class: "label" }, "Last thorough run"), h("span", { class: "value", style: "font-size:26px" }, fresh.last_thorough_job ? ago(fresh.last_thorough_job.finished_at) : "never"),
      h("span", { class: "hint" }, fresh.last_thorough_job ? JOB_LABEL[fresh.last_thorough_job.state] : "Run a baseline to start")),
    h("div", { class: "card stat" }, h("span", { class: "label" }, "Stored items"), h("span", { class: "value" }, fresh.items || 0),
      h("span", { class: "hint" }, fresh.items_not_currently_observed ? `${fresh.items_not_currently_observed} no longer listed` : "all currently observed")),
    h("div", { class: "card stat" }, h("span", { class: "label" }, "Coverage gaps"), h("span", { class: "value" }, gaps.length),
      h("span", { class: "hint" }, h("a", { href: "#/coverage" }, "See what was not read"))));
  const banners = [];
  if (active) banners.push(h("div", { class: `banner ${active.state === "needs_login" ? "bad" : "info"}` }, h("div", { class: "b-body" },
    h("div", { class: "b-title" }, `Job ${active.id} · ${JOB_LABEL[active.state]}`), h("div", { class: "small" }, active.message || "")),
    h("a", { class: "btn", href: "#/collect" }, "Open")));
  if (disc.discrepancies.length) banners.push(h("div", { class: "banner warn" }, h("div", { class: "b-body" },
    h("div", { class: "b-title" }, `${disc.discrepancies.length} date conflict${disc.discrepancies.length > 1 ? "s" : ""} between announcements and assignments`),
    disc.discrepancies.slice(0, 3).map((d) => h("div", { class: "small" }, h("a", { href: `#/item/${d.item_id}` }, d.item_title), " mentions ",
      (d.detail.announcement_mentions || []).join(", "), " for ", h("a", { href: `#/item/${d.other_item_id}` }, d.other_title), ` (due ${d.detail.assignment_due_local})`)))));
  const dlCard = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Upcoming"), h("a", { href: "#/deadlines", class: "small" }, "All deadlines")),
    dl.deadlines.length ? h("ul", { class: "list" }, dl.deadlines.slice(0, 8).map((d) => { const p = dateParts(d.date.iso); return h("li", null,
      h("div", { class: "spread" }, h("div", null, h("span", { class: "li-date" }, `${p.dow} ${p.day}`), h("a", { class: "li-title", href: `#/item/${d.item_id}` }, d.title)), submissionPill(d.submission)),
      h("div", { class: "li-meta", style: "padding-left:58px" }, h("span", null, fmtTime(d.date.iso)), h("span", null, until(d.date.iso)), h("span", null, d.course_title || ""))); })) :
      empty("Nothing due in the next 14 days", "Based on the deadlines collected so far."));
  const chCard = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Recent changes"), h("a", { href: "#/changes", class: "small" }, "All changes")),
    recent.length ? h("ul", { class: "list" }, recent.map(eventLi)) : empty("No changes since the baseline", "Changes appear after a later check finds edits."));
  mount(main, head, banners, stats, dash && dash.courses.length > 1 ? h("div", { style: "margin-top:14px" }, C.dashboardView({ title: "All courses", ...dash })) : null,
    h("div", { class: "grid cols-2", style: "margin-top:14px" }, dlCard, chCard));
};

function summarize(v) {
  if (v === null || v === undefined) return "none";
  if (typeof v === "object" && "raw" in v) return v.iso ? fmtDate(v.iso) : v.raw;
  if (typeof v === "string") { const t = v.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim(); return t.length > 160 ? t.slice(0, 160) + "…" : t; }
  const s = JSON.stringify(v); return s.length > 160 ? s.slice(0, 160) + "…" : s;
}
function eventLi(e) {
  return h("li", null,
    h("div", { class: "row" }, pill(EVENT_CLASS[e.kind] || "muted", EVENT_LABEL[e.kind] || e.kind),
      h("a", { class: "li-title", href: `#/item/${e.item_id}` }, e.title || "(untitled)"),
      e.field ? h("span", { class: "muted small" }, e.field) : null),
    e.kind === "field_changed" ? h("div", { class: "small", style: "margin-top:2px" }, h("span", { class: "muted" }, summarize(e.old)), " → ", summarize(e.new)) : null,
    h("div", { class: "li-meta" }, h("span", null, TYPE_LABEL[e.item_type] || e.item_type), e.course_title ? h("span", null, e.course_title) : null,
      h("span", { title: e.observed_at }, "observed " + ago(e.observed_at))));
}

// ---------- deadlines ----------
VIEWS.deadlines = async (main, _p, params) => {
  const range = params.get("range") || "14";
  const start = new Date(Date.now() - (range === "past" ? 30 : 1) * 86400000);
  const end = new Date(Date.now() + (range === "past" ? 0 : Number(range)) * 86400000 + 1);
  const cal = params.get("cal") !== "0";
  const data = await api(A("/deadlines") + qs({ start: start.toISOString(), end: end.toISOString(), include_calendar: cal }));
  const chips = h("div", { class: "chips" }, [["7", "7 days"], ["14", "14 days"], ["30", "30 days"], ["120", "Term"], ["past", "Past 30 days"]].map(([k, l]) =>
    h("button", { class: "chip" + (k === range ? " on" : ""), onclick: () => { location.hash = `#/deadlines?range=${k}&cal=${cal ? 1 : 0}`; } }, l)),
    h("label", { class: "check small", style: "margin-left:8px" }, h("input", { type: "checkbox", checked: cal ? true : null, onchange: (e) => { location.hash = `#/deadlines?range=${range}&cal=${e.target.checked ? 1 : 0}`; } }), "Include calendar events"));
  const groups = new Map();
  for (const d of data.deadlines) { const k = dayKey(d.date.iso); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(d); }
  const body = groups.size ? h("div", { class: "agenda" }, [...groups.entries()].map(([k, list]) => {
    const p = dateParts(list[0].date.iso);
    return h("div", { class: "day-group" + (p.today ? " today" : "") },
    h("div", { class: "day-date" }, h("span", { class: "dow" }, p.dow), h("span", { class: "dnum" }, p.day),
      h("span", { class: "mon" }, p.mon), h("span", { class: "rel" }, p.rel)),
    h("div", { class: "day-items" }, list.map((d) => {
      const doneLocal = d.done_locally;
      return h("div", { class: "deadline" + (doneLocal ? " done-local" : "") },
        h("div", { class: "time" }, fmtTime(d.date.iso), h("div", null, C.countdown(d.date.iso))),
        h("div", null, h("div", { class: "row" }, h("a", { class: "strong", href: `#/item/${d.item_id}` }, d.title),
          d.item_type !== "assignment" ? pill("muted plain", TYPE_LABEL[d.item_type]) : null, d.presence !== "observed" ? pill("bad", "no longer listed") : null),
          h("div", { class: "li-meta" }, h("span", null, d.course_title || ""), d.points_possible != null ? h("span", null, `${d.points_possible} pts`) : null,
            d.location_name ? h("span", null, d.location_name) : null,
            h("span", { title: "When the due date was last read from Canvas" }, "date read " + ago(d.date_observed_at)),
            d.source_url ? h("a", { href: d.source_url, target: "_blank", rel: "noopener noreferrer" }, "Open in Canvas") : null)),
        h("div", { class: "row", style: "justify-content:flex-end" }, submissionPill(d.submission),
          d.item_type !== "calendar_event" ? C.remindButton({ title: `Due: ${d.title}`, due: d.date.iso, item_id: d.item_id }) : null,
          d.item_type !== "calendar_event" ? localTaskToggle(d) : null));
    })));
  })) : [empty("Nothing in this range", "No collected deadlines fall in this window.")];
  const unresolved = data.unresolved_dates.length ? h("div", { class: "card", style: "margin-top:14px" }, h("h2", null, "Dates that could not be placed"),
    h("p", { class: "muted small" }, "These dates were shown without a year or time zone. They are kept exactly as written rather than guessed."),
    h("ul", { class: "list" }, data.unresolved_dates.map((d) => h("li", null, h("a", { href: `#/item/${d.item_id}` }, d.title), " — ", d.date.raw)))) : null;
  const found = await api(A("/date-candidates/scan"), { method: "POST" }).catch(() => ({ candidates: [] }));
  const sugg = found.candidates.filter((c) => c.status === "suggested" && new Date(c.starts_at) > Date.now() - 86400000);
  const conf = found.candidates.filter((c) => c.status === "confirmed" && new Date(c.starts_at) > Date.now() - 86400000);
  const fromText = sugg.length || conf.length ? h("div", { class: "card", style: "margin-top:14px" },
    h("div", { class: "card-head" }, h("h2", null, "Dates found in course pages"), h("span", { class: "sub" }, "exams and other dates that are not Canvas deadlines")),
    sugg.length ? h("ul", { class: "list" }, sugg.map((c) => h("li", null, h("div", { class: "spread" },
      h("div", null, h("span", { class: "strong" }, c.label), h("div", { class: "small" }, fmtDate(c.starts_at, { weekday: true }), c.ends_at ? `–${c.ends_at.slice(11, 16)}` : "", c.location ? ` · ${c.location}` : "", " · ", C.countdown(c.starts_at)),
        h("div", { class: "tiny muted" }, c.course_title || "", " · from ", h("a", { href: `#/item/${c.item_id}` }, c.item_title || "page"), ": “", c.snippet, "”")),
      h("div", { class: "row" }, h("button", { class: "sm primary", onclick: async () => { if (await guard(() => api(A(`/date-candidates/${c.id}/confirm`), { method: "POST", body: {} }))) { toast("Added to your planner"); route(); } } }, "Confirm"),
        h("button", { class: "sm ghost", onclick: async () => { if (await guard(() => api(A(`/date-candidates/${c.id}/dismiss`), { method: "POST" }))) route(); } }, "Not a date")))))) : null,
    conf.length ? h("div", { class: "small", style: "margin-top:8px" }, h("strong", null, "Confirmed: "), conf.map((c, i) => [i ? " · " : "", `${c.label} (${fmtDate(c.starts_at, { weekday: true })})`])) : null) : null;
  mount(main, pageHead("Deadlines", `Times in ${data.time_zone}. Submission states are as Canvas reported them when last read.`), chips,
    h("div", { style: "margin-top:18px" }, body), fromText, unresolved,
    h("p", { class: "muted small", style: "margin-top:16px" }, "The ✓ toggle is a private reminder stored locally. It never submits anything or changes Canvas."));
};

function localTaskToggle(d) {
  const t = (d.local_tasks || []).find((x) => !x.local_due);   // dated to-dos (e.g. study blocks) are separate
  const done = t && t.task_state === "done";
  return h("button", { class: "sm " + (done ? "" : "ghost"), title: "Private local marker; not a Canvas submission",
    onclick: async (e) => {
      e.preventDefault();
      if (t) await guard(() => api(A(`/annotations/${t.id}`), { method: "PATCH", body: { task_state: done ? "open" : "done" } }));
      else await guard(() => api(A("/annotations"), { method: "POST", body: { kind: "task", item_id: d.item_id, title: d.title, task_state: "done" } }));
      route();
    } }, done ? "✓ Done (local)" : "Mark done locally");
}

// ---------- courses ----------
VIEWS.courses = async (main) => {
  const d = await api(A("/courses"));
  if (!d.courses.length) { mount(main, pageHead("Courses"), empty("No courses yet", "Run a baseline collection to discover your courses.", h("a", { class: "btn primary", href: "#/collect" }, "Go to collection"))); return; }
  const by = { current: [], future: [], past: [], unknown: [] };
  for (const c of d.courses) (by[c.listing_section] || by.unknown).push(c);
  const sec = (title, list) => list.length ? h("div", { style: "margin-top:16px" }, h("h2", { style: "margin-bottom:8px" }, title),
    h("div", { class: "grid cols-3" }, list.map((c) => {
      const accessible = c.access === "accessible";
      const counts = c.item_counts || {};
      return h("div", { class: "card course-card", onclick: () => { if (accessible) location.hash = `#/course/${c.course_id}`; } },
        h("div", { class: "spread" }, h("span", { class: "strong" }, c.title), accessible ? null : pill("bad", c.access === "invitation_pending" ? "Invitation" : "Not accessible")),
        h("div", { class: "li-meta" }, c.course_code ? h("span", null, c.course_code) : null, c.term ? h("span", null, c.term) : null,
          c.published === false ? h("span", null, "unpublished") : null),
        accessible ? h("div", { class: "li-meta" }, ["assignment", "announcement", "file", "page"].filter((t) => counts[t]).map((t) => h("span", null, `${counts[t]} ${TYPE_LABEL[t].toLowerCase()}${counts[t] === 1 ? "" : "s"}`))) : h("div", { class: "small muted", style: "margin-top:6px" }, "Listed in All Courses but cannot be opened; not collected."),
        accessible && c.content_state && c.content_state !== "has_content" ? h("div", { class: "small", style: "margin-top:6px" },
          pill({ empty: "muted", hidden: "warn", not_checked: "muted" }[c.content_state] || "muted", { empty: "Empty", hidden: "Hidden from students", not_checked: "Not checked" }[c.content_state] || c.content_state),
          h("span", { class: "tiny muted" }, " ", c.content_state_reason || "")) : null,
        (c.possible_duplicates || []).map((o) => { const other = d.courses.find((x) => x.course_id === o);
          return h("div", { class: "dup-note small", onclick: (e) => e.stopPropagation() }, "Possible duplicate of ", h("strong", null, other ? other.title : o), " ",
            h("button", { class: "sm", onclick: async () => { if (await guard(() => api(A(`/courses/${c.course_id}/link`), { method: "PUT", body: { other_course_id: o, relation: "same" } }))) route(); } }, "Same course"), " ",
            h("button", { class: "sm ghost", onclick: async () => { if (await guard(() => api(A(`/courses/${c.course_id}/link`), { method: "PUT", body: { other_course_id: o, relation: "different" } }))) route(); } }, "Different")); }),
        (c.linked_shells || []).length ? h("div", { class: "tiny muted", style: "margin-top:4px" }, "Same course as ", c.linked_shells.map((o) => (d.courses.find((x) => x.course_id === o) || {}).title || o).join(", ")) : null,
        h("div", { class: "tiny muted", style: "margin-top:6px" }, "last read " + ago(c.last_observed_at)));
    }))) : null;
  mount(main, pageHead("Courses", "From Courses → All Courses, including entries not shown on the Dashboard."),
    sec("Current", by.current), sec("Future", by.future), sec("Past", by.past), sec("Other", by.unknown));
};

VIEWS.course = async (main, parts, params) => {
  const cid = parts[0];
  const tab = params.get("tab") || "overview";
  const courses = await api(A("/courses"));
  const c = courses.courses.find((x) => x.course_id === cid);
  if (!c) { mount(main, empty("Course not found", "")); return; }
  const types = ["announcement", "assignment", "quiz", "module", "page", "file", "discussion", "calendar_event", "external_tool", "external_link"].filter((t) => (c.item_counts || {})[t]);
  const tabs = h("div", { class: "tabs" }, ["overview", ...types].map((t) => h("button", { class: t === tab ? "active" : "", onclick: () => { location.hash = `#/course/${cid}?tab=${t}`; } },
    t === "overview" ? "Overview" : `${TYPE_LABEL[t]}s (${c.item_counts[t]})`)));
  const head = pageHead(c.title, [c.course_code, c.term].filter(Boolean).join(" · "),
    c.source_url ? h("a", { class: "btn", href: c.source_url, target: "_blank", rel: "noopener noreferrer" }, "Open in Canvas") : null);
  let body;
  if (tab === "overview") {
    const [cov, items] = await Promise.all([api(A("/coverage") + qs({ course_id: cid })), api(A("/items") + qs({ course_id: cid, type: "syllabus" }))]);
    const pagesHome = await api(A("/items") + qs({ course_id: cid, type: "page", limit: 200 }));
    const syl = items.items[0];
    body = h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", null, "Navigation shown by Canvas"),
        h("div", { style: "margin-top:8px" }, c.navigation ? fieldValue("navigation", { value: c.navigation }) : h("span", { class: "muted" }, "not collected")),
        h("p", { class: "muted small" }, "Sections missing here are not guessed. Content they hide is still collected when Home, the syllabus, modules or pages link to it."),
        syl ? h("p", null, h("a", { href: `#/item/${syl.id}` }, "Read the syllabus")) : null,
        pagesHome.items.length ? h("p", { class: "small" }, `${pagesHome.items.length} page(s) collected`) : null),
      h("div", { class: "card" }, h("h2", null, "Coverage for this course"), coverageTable(cov.records)),
      await gradingCard(cid));
  } else {
    const list = await api(A("/items") + qs({ course_id: cid, type: tab, limit: 200 }));
    body = h("div", { class: "card" }, itemTable(list.items));
  }
  mount(main, head, tabs, body);
};

// Local grading rules: what Canvas leaves out (real group weights, pass mark). Never sent to Canvas.
async function gradingCard(cid) {
  const card = h("div", { class: "card grading-card", style: "grid-column:1/-1" });
  const draw = (rules) => {
    const rows = Object.entries(rules.groups || {}).map(([name, g]) => ({ name, weight: g.weight ?? "", drop: g.drop_lowest || "" }));
    if (!rows.length) rows.push({ name: "", weight: "", drop: "" });
    const body = h("tbody");
    const addRow = (r) => {
      const tr = h("tr", null,
        h("td", null, h("input", { type: "text", class: "g-name", value: r.name, placeholder: "Assignment group, e.g. Homework", "aria-label": "Group name" })),
        h("td", { class: "num" }, h("input", { class: "num-in g-weight", type: "number", min: 0, max: 100, step: "any", value: r.weight, "aria-label": "Weight (%)" })),
        h("td", { class: "num" }, h("input", { class: "num-in g-drop", type: "number", min: 0, step: 1, value: r.drop, "aria-label": "Drop lowest" })),
        h("td", null, h("button", { class: "ghost sm", "aria-label": "Remove this group", onclick: () => { tr.remove(); total(); } }, "×")));
      tr.addEventListener("input", () => total());
      body.append(tr);
    };
    rows.forEach(addRow);
    const sum = h("span", { class: "small", "aria-live": "polite" });
    const total = () => {
      const t = [...body.querySelectorAll(".g-weight")].reduce((s, i) => s + (Number(i.value) || 0), 0);
      sum.textContent = `Weights add up to ${Math.round(t * 100) / 100}%`;
      sum.className = Math.abs(t - 100) < 0.01 || t === 0 ? "small muted" : "small bad-text";
    };
    total();
    const thr = h("input", { class: "num-in", type: "number", min: 0, step: "any", value: rules.threshold ?? "", "aria-label": "Pass mark" });
    const src = h("input", { type: "text", value: rules.source || "", placeholder: "e.g. syllabus p.2", "aria-label": "Source of these rules" });
    const items = Object.entries(rules.items || {});
    mount(card, h("div", { class: "card-head" }, h("h2", null, "Grading rules"), h("span", { class: "sub" }, "kept on this computer · used by score models and grade projections")),
      h("p", { class: "small muted" }, "Canvas often reports 0 for every group weight. Enter the syllabus weights here (or ask the assistant to read them); nothing is sent to Canvas."),
      h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Group"), h("th", { class: "num" }, "Weight %"), h("th", { class: "num" }, "Drop lowest"), h("th", null, ""))), body)),
      h("div", { class: "row", style: "margin-top:8px" }, h("button", { class: "sm", onclick: () => { addRow({ name: "", weight: "", drop: "" }); total(); } }, "+ Add group"), sum),
      h("div", { class: "row", style: "margin-top:10px" }, h("label", { class: "small" }, "Pass mark ", thr), h("label", { class: "small grow" }, "Source ", src)),
      items.length ? h("details", { style: "margin-top:10px" }, h("summary", { class: "small" }, `${items.length} assignment rule(s) set by the assistant`),
        h("ul", { class: "small" }, items.map(([id, r]) => h("li", null, h("code", null, id), " ", Object.entries(r).map(([k, v]) => `${k}: ${v}`).join(" · "))))) : null,
      h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "primary", onclick: async () => {
        const groups = {};
        for (const tr of body.querySelectorAll("tr")) {
          const name = tr.querySelector(".g-name").value.trim();
          if (!name) continue;
          const w = tr.querySelector(".g-weight").value, d = tr.querySelector(".g-drop").value;
          groups[name] = { ...(w !== "" ? { weight: Number(w) } : {}), ...(d !== "" && Number(d) ? { drop_lowest: Number(d) } : {}) };
        }
        const next = { ...rules, groups, threshold: thr.value === "" ? undefined : Number(thr.value), source: src.value.trim() || undefined };
        delete next.updated_at;
        const r = await guard(() => api(A(`/courses/${cid}/grading`), { method: "PUT", body: next }));
        if (r) { toast("Grading rules saved"); draw(r); }
      } }, "Save rules"), rules.updated_at ? h("span", { class: "tiny muted" }, `saved ${ago(rules.updated_at)}`) : null));
  };
  try { draw(await api(A(`/courses/${cid}/grading`))); } catch (e) { mount(card, h("p", { class: "muted small" }, "Grading rules unavailable: " + e.message)); }
  return card;
}

function itemTable(items) {
  if (!items.length) return empty("Nothing here", "");
  return h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Title"), h("th", null, "Type"), h("th", null, "Last read"), h("th", null, ""))),
    h("tbody", null, items.map((i) => h("tr", { class: "clickable", onclick: () => { location.hash = `#/item/${i.id}`; } },
      h("td", null, i.title || "(untitled)"), h("td", { class: "muted" }, TYPE_LABEL[i.item_type]), h("td", { class: "nowrap small muted" }, ago(i.last_observed_at)),
      h("td", null, i.presence !== "observed" ? pill("bad", "no longer listed") : null))))));
}

function coverageTable(records) {
  if (!records.length) return h("p", { class: "muted" }, "No coverage recorded yet.");
  return h("table", { class: "t" }, h("tbody", null, records.map((r) => h("tr", null,
    h("td", null, CAT_LABEL[r.category] || r.category), h("td", null, pill(STATUS_CLASS[r.latest.status], r.latest.status.replace("_", " "))),
    h("td", { class: "small muted" }, r.latest.reason || "", r.last_complete_at ? h("div", { class: "tiny" }, "last complete " + ago(r.last_complete_at)) : null)))));
}

// ---------- item ----------
VIEWS.item = async (main, parts) => {
  const id = parts[0];
  const [it, hist] = await Promise.all([api(A(`/items/${id}`)), api(A(`/items/${id}/history`))]);
  const head = pageHead(it.title || "(untitled)", null,
    it.source_url ? h("a", { class: "btn", href: it.source_url, target: "_blank", rel: "noopener noreferrer" }, "Open in Canvas") : null);
  const meta = h("div", { class: "row", style: "margin:-8px 0 14px" }, pill("muted plain", TYPE_LABEL[it.item_type]),
    it.course_canvas_id ? h("a", { href: `#/course/${it.course_canvas_id}`, class: "small" }, it.course_title || "Course " + it.course_canvas_id) : null,
    h("span", { class: "small muted" }, `first seen ${fmtDate(it.first_observed_at)} · last read ${ago(it.last_observed_at)}`));
  const banners = [];
  if (it.presence !== "observed") banners.push(h("div", { class: "banner bad" }, h("div", { class: "b-body" }, h("div", { class: "b-title" },
    it.presence === "source_unavailable" ? "Canvas no longer serves this item" : "Not in the latest complete list"),
    h("div", { class: "small" }, it.presence_detail || "", ". The last known content below is kept as evidence; this does not prove it was deleted."))));
  if (it.freshness.state === "mixed") banners.push(h("div", { class: "banner info" }, h("div", { class: "b-body" }, h("div", { class: "b-title" }, "Fields were read at different times"),
    h("div", { class: "small" }, `Oldest field read ${ago(it.freshness.oldest_field_observed_at)}, newest ${ago(it.freshness.newest_field_observed_at)}. Each field shows when it was last read.`))));
  if (it.freshness.state === "not_collected") banners.push(h("div", { class: "banner warn" }, h("div", { class: "b-body" }, h("div", { class: "b-title" }, "Content not read"),
    h("div", { class: "small" }, "Canvas Hub has seen a link to this item but has not read its content (it may be locked, outside scope, or gated by a view-to-complete requirement)."))));
  for (const d of it.discrepancies) banners.push(h("div", { class: "banner warn" }, h("div", { class: "b-body" }, h("div", { class: "b-title" }, "Date conflict"),
    h("div", { class: "small" }, h("a", { href: `#/item/${d.item_id}` }, d.item_title), " mentions ", (d.detail.announcement_mentions || []).join(", "), "; ",
      h("a", { href: `#/item/${d.other_item_id}` }, d.other_title), ` is due ${d.detail.assignment_due_local} in Canvas. Both are kept; check with your instructor.`))));
  const fields = it.fields.filter((f) => !["title"].includes(f.field));
  const bodyF = fields.filter((f) => f.kind === "html" && f.value);
  const otherF = fields.filter((f) => !(f.kind === "html" && f.value));
  const cards = [];
  for (const f of bodyF) cards.push(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, f.field === "message" ? "Message" : f.field === "body" ? "Content" : f.field[0].toUpperCase() + f.field.slice(1)),
    h("span", { class: "sub" }, "read " + ago(f.last_observed_at))), fieldValue(f.field, f, it)));
  if (it.item_type === "external_link") {
    const content = (it.fields.find((f) => f.field === "content") || {}).value;
    const by = (it.fields.find((f) => f.field === "provided_by") || {}).value;
    const readAt = (it.fields.find((f) => f.field === "read_at") || {}).value;
    if (content) cards.unshift(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Content read by a connected agent"),
      h("span", { class: "sub" }, `${by || "agent"} · ${readAt ? fmtDate(readAt) : ""}`)),
      h("div", { class: "banner warn", style: "margin-bottom:10px" }, h("div", { class: "b-body small" }, "Provided by a connected agent, not reported by Canvas. Check the original site for anything important.")),
      h("pre", { class: "text" }, content)));
    const url = (it.fields.find((f) => f.field === "url") || {}).value || it.source_url;
    if (url) cards.unshift(h("div", { class: "card" }, h("div", { class: "spread" },
      h("div", { class: "small" }, "Canvas Hub doesn't open external sites itself. A connected agent with its own access can read this page for you."),
      h("button", { onclick: async () => { const r = await guard(() => api(A("/requests"), { method: "POST", body: { url, title: it.title || undefined, course_id: it.course_canvas_id || undefined } }));
        if (r) { toast("Request left for connected agents"); location.hash = "#/requests"; } } }, content ? "Ask an agent to re-read" : "Ask a connected agent to read this"))));
  }
  if (it.submission_item_id) cards.push(h("div", { class: "card" }, h("a", { href: `#/item/${it.submission_item_id}` }, "View your submission and feedback →")));
  if (it.resources.length) cards.push(h("div", { class: "card" }, h("h2", null, "Document"), it.resources.map((r) => resourceBlock(r))));
  cards.push(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Details"), h("span", { class: "sub" }, "value · last read")),
    h("div", { class: "kv" }, otherF.map((f) => [h("div", { class: "k" }, f.field.replace(/_/g, " "), f.volatile ? h("div", { class: "tiny muted" }, "not tracked for changes") : null),
      h("div", null, fieldValue(f.field, f, it)), h("div", { class: "t", title: `changed ${f.last_changed_at}` }, ago(f.last_observed_at))]))));
  const linksOut = it.links_out.filter((l) => l.relation !== "module_contains" || true);
  if (linksOut.length || it.links_in.length) cards.push(h("div", { class: "card" }, h("h2", null, "Connections"),
    linksOut.length ? h("ul", { class: "list" }, linksOut.map((l) => h("li", null, h("span", { class: "muted small" }, relLabel(l.relation), " → "),
      h("a", { href: `#/item/${l.item_id}` }, l.title || l.label || TYPE_LABEL[l.item_type]), " ", h("span", { class: "muted small" }, TYPE_LABEL[l.item_type]),
      l.collected ? null : pill("muted", "not read")))) : null,
    it.links_in.length ? h("div", { style: "margin-top:8px" }, h("div", { class: "small muted" }, "Referenced from"), h("ul", { class: "list" }, it.links_in.map((l) => h("li", null,
      h("a", { href: `#/item/${l.item_id}` }, l.title || TYPE_LABEL[l.item_type]), " ", h("span", { class: "muted small" }, relLabel(l.relation)))))) : null));
  cards.push(notesCard(it));
  cards.push(historyCard(hist));
  mount(main, head, meta, banners, h("div", { class: "stack" }, cards));
};

const relLabel = (r) => ({ body_link: "links to", module_item_content: "opens", module_contains: "contains", attachment: "attachment",
  folder_contains: "contains", submission_for: "submission for", home_page: "home page", calendar_for: "calendar entry for" }[r] || r);

function resourceBlock(r) {
  const st = r.extraction_status;
  const cls = st === "ok" || st === "ocr" ? "ok" : ["needs_ocr", "image_only", "unsupported_media", "unsupported_type", "empty", "ocr_low_confidence"].includes(st) ? "warn" : st === "failed" ? "bad" : "muted";
  const conf = r.ocr_confidence != null ? ` · ${Math.round(r.ocr_confidence)}% confidence` : "";
  const label = { ok: "Text extracted", ocr: `Text from OCR${conf}`, ocr_low_confidence: `OCR, low confidence${conf}`,
    needs_ocr: "Scanned — needs OCR", image_only: "Image only", unsupported_media: "Audio/video (not transcribed)",
    unsupported_type: "No text extractor", failed: "Extraction failed", empty: "No text found", tool_unavailable: "Extractor not installed", not_attempted: "Not extracted" }[st] || st;
  const out = h("div", null);
  const textBox = h("div");
  mount(out, h("div", { class: "spread" }, h("div", null, h("span", { class: "strong" }, r.filename || r.resource_id.slice(0, 12)), " ",
    h("span", { class: "muted small" }, `${Math.round((r.size || 0) / 1024)} KB`)), h("div", { class: "row" }, pill(cls, label, r.extraction_detail || ""),
    h("a", { class: "btn sm", href: A(`/resources/${r.resource_id}/original`) }, "Download original"),
    r.extracted_chars ? h("button", { class: "sm", onclick: async () => {
      const t = await guard(() => api(A(`/resources/${r.resource_id}/text`) + qs({ limit: 60000 })));
      if (t) mount(textBox, h("pre", { class: "text" }, t.text), t.next_offset ? h("div", { class: "muted small" }, `Showing first ${t.text.length} of ${t.total_chars} characters.`) : null);
    } }, "Show extracted text") : null)),
    r.extraction_detail ? h("div", { class: "small muted" }, r.extraction_detail) : null,
    h("div", { class: "tiny muted" }, "The extractor reads text only; diagrams, images and formulas may need the original."), textBox);
  return out;
}

function historyCard(hist) {
  const evs = hist.events.filter((e) => e.kind !== "first_observed" || true);
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "History"), h("span", { class: "sub" }, `${hist.revisions.length} revision(s) · ${hist.observations.length} observation(s)`)),
    evs.length ? h("div", { class: "timeline" }, evs.map((e) => h("div", { class: "tl-item" },
      h("div", { class: "row" }, pill(EVENT_CLASS[e.kind] || "muted", EVENT_LABEL[e.kind] || e.kind), e.field ? h("span", { class: "strong" }, e.field) : null,
        h("span", { class: "muted small" }, fmtDate(e.observed_at)), e.baseline ? pill("muted plain", "baseline") : null),
      e.kind === "field_changed" ? C.diffPair(summarizeLong(e.old), summarizeLong(e.new)) : null,
      e.kind === "field_added" ? h("div", { class: "small muted" }, "This field was read for the first time; it is not an edit.") : null,
      e.kind === "first_observed" ? h("div", { class: "small muted" }, "First time Canvas Hub saw this item. That is not necessarily when it was published.") : null))) : h("p", { class: "muted" }, "No history yet."));
}
function summarizeLong(v) {
  if (v && typeof v === "object" && "raw" in v) return v.iso ? `${fmtDate(v.iso)} (${v.raw})` : v.raw;
  if (typeof v === "string") return v.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 1200) || "(empty)";
  return v === null || v === undefined ? "none" : JSON.stringify(v).slice(0, 1200);
}

function notesCard(it) {
  const ta = h("textarea", { placeholder: "Private note (stored only on this computer)" });
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Your notes"), h("span", { class: "sub" }, "never overwritten by refresh · never sent to Canvas")),
    it.annotations.length ? h("ul", { class: "list" }, it.annotations.map((a) => h("li", null, h("div", { class: "spread" },
      h("div", null, a.kind === "task" ? pill(a.task_state === "done" ? "ok" : "muted", a.task_state === "done" ? "Done (local)" : "To do") : null, " ", a.title || ""),
      h("button", { class: "ghost sm", onclick: async () => { await guard(() => api(A(`/annotations/${a.id}`), { method: "DELETE" })); route(); } }, "Delete")),
      a.body ? h("div", { style: "white-space:pre-wrap" }, a.body) : null, h("div", { class: "li-meta" }, "updated " + ago(a.updated_at))))) : null,
    ta, h("div", { class: "row", style: "margin-top:8px" },
      h("button", { class: "primary sm", onclick: async () => { if (!ta.value.trim()) return; await guard(() => api(A("/annotations"), { method: "POST", body: { kind: "note", item_id: it.id, body: ta.value } })); route(); } }, "Save note"),
      h("button", { class: "sm", onclick: async () => { await guard(() => api(A("/annotations"), { method: "POST", body: { kind: "task", item_id: it.id, title: it.title, body: ta.value } })); route(); } }, "Add as to-do")));
}

// ---------- search ----------
VIEWS.search = async (main, _p, params) => {
  const q = params.get("q") || "";
  const course = params.get("course") || "";
  const docs = params.get("docs") !== "0";
  const courses = (await api(A("/courses"))).courses.filter((c) => c.access === "accessible");
  const input = h("input", { type: "search", value: q, placeholder: "Search titles, bodies, comments and documents…", style: "flex:1;min-width:240px", "aria-label": "Search" });
  const csel = h("select", { "aria-label": "Course" }, h("option", { value: "" }, "All courses"), courses.map((c) => h("option", { value: c.course_id, selected: c.course_id === course ? true : null }, c.title)));
  const dchk = h("input", { type: "checkbox", checked: docs ? true : null });
  const go = () => { location.hash = `#/search${qs({ q: input.value, course: csel.value, docs: dchk.checked ? 1 : 0 })}`; };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  const form = h("div", { class: "card" }, h("div", { class: "row" }, input, csel, h("label", { class: "check small" }, dchk, "Documents"), h("button", { class: "primary", onclick: go }, "Search")),
    h("div", { class: "tiny muted", style: "margin-top:6px" }, 'Use "quotes" for phrases and a trailing * for prefixes. All words must match.'));
  const results = h("div", { style: "margin-top:14px" });
  mount(main, pageHead("Search", "Exact, local full-text search over everything collected. No AI service involved."), form, results);
  input.focus();
  if (!q) return;
  let cursor = null;
  const list = h("ul", { class: "list card" });
  const more = h("div", { style: "margin-top:10px" });
  mount(results, list, more);
  const load = async () => {
    const r = await guard(() => api(A("/search") + qs({ q, course_id: course, include_documents: docs, cursor, limit: 20 })), results);
    if (!r) return;
    if (!r.results.length && !cursor) { mount(results, empty("No matches", "Try fewer words or check the coverage page for content that was not collected.")); return; }
    for (const x of r.results) list.append(h("li", null,
      h("div", { class: "row" }, h("a", { class: "li-title", href: `#/item/${x.item_id}` }, x.title || "(untitled)"), pill("muted plain", x.resource_id ? "Document" : TYPE_LABEL[x.item_type]),
        x.document && x.document.extraction_status !== "ok" ? pill("warn", x.document.extraction_status) : null),
      h("div", { class: "small", style: "margin-top:2px" }, highlight(x.snippet || x.title_snippet || "")),
      h("div", { class: "li-meta" }, x.course_title ? h("span", null, x.course_title) : null, h("span", null, "read " + ago(x.last_observed_at)),
        x.source_url ? h("a", { href: x.source_url, target: "_blank", rel: "noopener noreferrer" }, "Canvas") : null,
        (x.limitations || []).map((l) => h("span", { class: "muted" }, "⚠ " + l)))));
    cursor = r.next_cursor;
    mount(more, cursor ? h("button", { onclick: load }, "More results") : null);
  };
  load();
};
function highlight(s) {
  const out = []; const re = /\[\[(.+?)\]\]/g; let last = 0; let m;
  while ((m = re.exec(s))) { out.push(s.slice(last, m.index)); out.push(h("mark", null, m[1])); last = re.lastIndex; }
  out.push(s.slice(last));
  return out;
}

// ---------- changes ----------
VIEWS.changes = async (main, _p, params) => {
  const baseline = params.get("baseline") === "1";
  const first = params.get("first") !== "0";
  const r = await api(A("/changes") + qs({ include_baseline: baseline, include_first_observed: first, limit: 500 }));
  const evs = r.events.slice().reverse();
  const toggles = h("div", { class: "row" },
    h("label", { class: "check small" }, h("input", { type: "checkbox", checked: first ? true : null, onchange: (e) => { location.hash = `#/changes${qs({ baseline: baseline ? 1 : 0, first: e.target.checked ? 1 : 0 })}`; } }), "Show newly observed items"),
    h("label", { class: "check small" }, h("input", { type: "checkbox", checked: baseline ? true : null, onchange: (e) => { location.hash = `#/changes${qs({ baseline: e.target.checked ? 1 : 0, first: first ? 1 : 0 })}`; } }), "Include first baseline"));
  const groups = new Map();
  for (const e of evs) { const k = dayKey(e.recorded_at || e.observed_at); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(e); }
  const sentWrites = ((await api(A("/writes")).catch(() => ({ writes: [] }))).writes || []).filter((w) => ["sent", "failed"].includes(w.status)).slice(0, 10);
  mount(main, pageHead("Changes", "Edits, newly read fields and items that stopped appearing. Reading something for the first time is shown separately from an actual change."),
    sentWrites.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "What you sent to Canvas"), h("span", { class: "sub" }, "confirmed writes, newest first")), C.writesLog(sentWrites)) : null,
    h("div", { class: "card" }, toggles,
      h("div", { class: "legend", style: "margin-top:8px" }, Object.entries(EVENT_LABEL).map(([k, l]) => h("span", null, pill(EVENT_CLASS[k], l))))),
    groups.size ? [...groups.entries()].map(([k, list]) => h("div", { class: "card" }, h("div", { class: "day-label" }, fmtDate(list[0].recorded_at || list[0].observed_at, { weekday: true, time: false })),
      h("ul", { class: "list" }, list.map(eventLi)))) : h("div", { class: "card" }, empty("No changes", baseline ? "Nothing recorded yet." : "No changes since the first baseline. Tick “Include first baseline” to see everything first observed.")));
};

// ---------- coverage ----------
VIEWS.coverage = async (main) => {
  const cov = await api(A("/coverage"));
  const courses = (await api(A("/courses"))).courses;
  const titles = Object.fromEntries(courses.map((c) => [c.course_id, c.title]));
  const cats = ["home", "announcements", "assignments", "quizzes", "grades", "modules", "pages", "files", "discussions", "external_tools", "linked_content"];
  const byCourse = new Map();
  for (const r of cov.records) { const k = r.course_id || ""; if (!byCourse.has(k)) byCourse.set(k, {}); byCourse.get(k)[r.category] = r; }
  const detail = h("div", { class: "card", style: "margin-top:14px" }, h("p", { class: "muted" }, "Select a cell to see exactly what was read and why anything is missing."));
  const showDetail = (r, td) => {
    document.querySelectorAll(".matrix td.sel").forEach((x) => x.classList.remove("sel"));
    if (td) td.classList.add("sel");
    const l = r.latest;
    mount(detail, h("div", { class: "card-head" }, h("h2", null, `${CAT_LABEL[r.category]} · ${r.course_title || titles[r.course_id] || "Account"}`), pill(STATUS_CLASS[l.status], l.status.replace("_", " "))),
      l.reason ? h("p", null, l.reason) : null,
      h("div", { class: "kv" },
        ["List / index", l.index_depth], ["Item bodies", l.body_depth], ["Attachments", l.attachment_depth],
        ["Counted", l.expected_count != null ? `${l.collected_count} of ${l.expected_count}` : l.collected_count != null ? String(l.collected_count) : "—"],
        ["Run", `job ${l.job_id} (${l.mode}) · ${fmtDate(l.observed_at)}`],
        ["Last complete", r.last_complete_at ? fmtDate(r.last_complete_at) : "never"],
        ["Last thorough", r.last_thorough ? `${r.last_thorough.status} · ${fmtDate(r.last_thorough.observed_at)}` : "none"]).map(([k, v]) => [h("div", { class: "k" }, k), h("div", null, v), h("div", { class: "t" })]),
      Object.keys(l.detail || {}).length ? h("details", { class: "raw", style: "margin-top:8px" }, h("summary", null, "Details"), h("pre", { class: "text tiny" }, JSON.stringify(l.detail, null, 2))) : null);
  };
  const rows = [...byCourse.entries()].filter(([k]) => k);
  const matrix = rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "matrix" },
    h("thead", null, h("tr", null, h("th"), cats.map((c) => h("th", { class: "rot" }, CAT_LABEL[c])))),
    h("tbody", null, rows.map(([cid, recs]) => h("tr", null, h("th", { style: "max-width:280px" }, titles[cid] || cid),
      cats.map((c) => {
        const r = recs[c];
        const st = r ? r.latest.status : "none";
        const sym = { complete: "✓", partial: "!", blocked: "×", not_exposed: "–", not_checked: "?", none: "" }[st];
        const td = h("td", { class: "cell " + st, title: r ? `${CAT_LABEL[c]}: ${st}${r.latest.reason ? " — " + r.latest.reason : ""}` : "not recorded" }, sym);
        if (r) td.addEventListener("click", () => showDetail(r, td));
        return td;
    })))))) : empty("No coverage yet", "Run a collection.");
  const acct = byCourse.get("") || {};
  const acctCard = h("div", { class: "card" }, h("h2", null, "Account-wide"), h("table", { class: "t" }, h("tbody", null, Object.values(acct).map((r) => h("tr", { class: "clickable", onclick: () => showDetail(r) },
    h("td", null, CAT_LABEL[r.category]), h("td", null, pill(STATUS_CLASS[r.latest.status], r.latest.status.replace("_", " "))), h("td", { class: "small muted" }, r.latest.reason || (r.latest.detail.start ? `${fmtDate(r.latest.detail.start, { time: false })} – ${fmtDate(r.latest.detail.end, { time: false })}` : "")))))));
  const legend = h("div", { class: "legend" }, [["complete", "Complete"], ["partial", "Partial"], ["blocked", "Blocked"], ["not_exposed", "Not in navigation"], ["none", "Not recorded"]].map(([k, l]) =>
    h("span", null, h("span", { class: "swatch " + k }), l)));
  const pending = cov.unfinished_jobs.length ? h("div", { class: "card" }, h("h2", null, "Unfinished work"), cov.unfinished_jobs.map((j) => h("div", null,
    h("div", { class: "row" }, h("span", { class: "strong" }, `Job ${j.id}`), pill(JOB_CLASS[j.state], JOB_LABEL[j.state]), h("span", { class: "small muted" }, j.message || "")),
    h("div", { class: "small muted" }, `${j.pending_tasks.length} destination(s) pending`)))) : null;
  const fails = cov.latest_job_failures.length ? h("div", { class: "card" }, h("h2", null, "Problems in the latest run"), h("ul", { class: "list" }, cov.latest_job_failures.map((f) =>
    h("li", null, h("div", { class: "row" }, pill(f.state === "blocked" ? "bad" : "warn", f.state), h("code", null, f.task_key)), h("div", { class: "small muted" }, f.last_error))))) : null;
  mount(main, pageHead("Coverage", "What has been read, how deeply, and what is missing. Metadata coverage is not the same as reading document contents."),
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Per course"), legend), matrix), detail, h("div", { class: "grid cols-2", style: "margin-top:14px" }, acctCard, h("div", null, pending, fails)));
};

// ---------- notes ----------
VIEWS.notes = async (main, _p, params) => {
  const filter = params.get("show") || "open";
  const q = filter === "all" || filter === "board" ? {} : filter === "notes" ? { kind: "note" } : { kind: "task", task_state: filter === "open" ? "active" : filter };
  const [r, files] = await Promise.all([api(A("/annotations") + qs(q)), api(A("/files")).catch(() => ({ files: [] }))]);
  const title = h("input", { type: "text", placeholder: "To-do title", "aria-label": "To-do title", style: "flex:1" });
  const due = h("input", { type: "datetime-local", "aria-label": "Due (optional)" });
  const chips = h("div", { class: "chips" }, [["open", "Open to-dos"], ["board", "Board"], ["done", "Done"], ["notes", "Notes"], ["all", "Everything"]].map(([k, l]) =>
    h("button", { class: "chip" + (k === filter ? " on" : ""), onclick: () => { location.hash = `#/notes?show=${k}`; } }, l)));
  const patch = (a, body) => guard(() => api(A(`/annotations/${a.id}`), { method: "PATCH", body }));
  const STATES = [["open", "To do"], ["in_progress", "In progress"], ["blocked", "Blocked"], ["done", "Done"]];
  // In place: click the title or text to edit; "- [ ]" lines in a body are checkboxes.
  const editable = (a, field, multiline) => {
    const show = () => {
      const val = a[field] || "";
      if (field === "body" && /^\s*[-*] \[[ xX]\]/m.test(val)) {
        const lines = val.split("\n");
        return h("div", { class: "note-body", title: "Click a line's text to edit" }, lines.map((ln, i) => {
          const m = ln.match(/^(\s*[-*] \[)([ xX])(\]\s?)(.*)$/);
          if (!m) return h("div", { class: "small", onclick: () => edit() }, ln || "\u00a0");
          return h("label", { class: "check small" }, h("input", { type: "checkbox", checked: m[2] !== " " ? true : null, onchange: async (e) => {
            lines[i] = m[1] + (e.target.checked ? "x" : " ") + m[3] + m[4]; const r2 = await patch(a, { body: lines.join("\n") }); if (r2) a.body = r2.body; } }),
            h("span", { onclick: (e) => { e.preventDefault(); edit(); } }, m[4]));
        }));
      }
      return h(field === "title" ? "span" : "div", { class: field === "title" ? "strong editable" : "small editable note-body", tabindex: "0", title: "Click to edit",
        onclick: () => edit(), onkeydown: (e) => { if (e.key === "Enter" && field === "title") edit(); } }, val || (field === "title" ? "(untitled)" : "Add details…"));
    };
    const box = h("span", { class: field === "body" ? "edit-box block" : "edit-box" }, show());
    const edit = () => {
      const inp = h(multiline ? "textarea" : "input", { class: multiline ? "edit-area" : "edit-in", "aria-label": `Edit ${field}` });
      inp.value = a[field] || "";
      const save = async () => { if (inp.value !== (a[field] || "")) { const r2 = await patch(a, { [field]: inp.value }); if (r2) a[field] = r2[field]; } mount(box, show()); };
      inp.addEventListener("blur", save);
      inp.addEventListener("keydown", (e) => { if (e.key === "Escape") mount(box, show()); if (e.key === "Enter" && (!multiline || e.metaKey || e.ctrlKey)) { e.preventDefault(); inp.blur(); } });
      mount(box, inp); inp.focus();
    };
    return box;
  };
  const list = r.annotations.length ? h("ul", { class: "list" }, r.annotations.map((a) => h("li", null, h("div", { class: "spread" },
    h("div", { class: "row" }, a.kind === "task" ? h("input", { type: "checkbox", checked: a.task_state === "done" ? true : null, "aria-label": "Done (local)",
      onchange: async (e) => { await patch(a, { task_state: e.target.checked ? "done" : "open" }); route(); } }) : pill("muted plain", "Note"),
      editable(a, "title", false),
      a.item_id ? h("a", { href: `#/item/${a.item_id}`, class: "small" }, a.item_title || "linked item") : null),
    h("div", { class: "row" }, a.local_due ? h("span", { class: "small muted" }, "due " + fmtDate(a.local_due), " · ", C.countdown(a.local_due)) : null,
      a.kind === "task" ? h("select", { class: "sm-select", "aria-label": "State", onchange: async (e) => { await patch(a, { task_state: e.target.value }); route(); } },
        STATES.map(([v, l]) => h("option", { value: v, selected: a.task_state === v ? true : null }, l))) : null,
      a.kind === "task" ? C.remindButton({ title: a.title || "To-do", due: a.local_due, annotation_id: a.id }) : null,
      h("button", { class: "ghost sm", onclick: async () => { await guard(() => api(A(`/annotations/${a.id}`), { method: "DELETE" })); route(); } }, "Delete"))),
    editable(a, "body", true)))) : empty("Nothing here", "");
  mount(main, pageHead("Notes & tasks", "Your own notes and to-dos. Stored locally, kept across every refresh, and never sent to Canvas. Click any text to edit it."),
    h("div", { class: "card" }, h("div", { class: "row" }, title, due, h("button", { class: "primary", onclick: async () => {
      if (!title.value.trim()) return;
      const body = { kind: "task", title: title.value };
      if (due.value) body.local_due = new Date(due.value).toISOString();
      await guard(() => api(A("/annotations"), { method: "POST", body })); route();
    } }, "Add to-do"))),
    h("div", { style: "margin:14px 0 8px" }, chips),
    filter === "board" ? C.kanbanView({ title: "To-do board" }) : h("div", { class: "card" }, list),
    h("div", { style: "margin-top:16px" }, C.filesCard(files.files)));
};

// ---------- collection ----------
VIEWS.collect = async (main) => {
  const [acct, jobs] = await Promise.all([api(A("")), api(A("/jobs") + qs({ limit: 15 }))]);
  const scope = acct.scope;
  const courses = (await api(A("/courses"))).courses.filter((c) => c.access === "accessible");
  const inst = currentInstance();
  const signedIn = inst && inst.session && inst.session.status === "signed_in";
  const hasThorough = jobs.jobs.some((j) => ["baseline", "thorough"].includes(j.mode) && ["completed", "completed_with_gaps"].includes(j.state));

  const modes = h("div", { class: "grid cols-3" },
    modeCard("Baseline", "Thorough first collection of everything in scope, including document downloads.", "baseline", !hasThorough),
    modeCard("Thorough refresh", "Re-reads lists, bodies and documents. Needed to confirm unchanged content and to check items that disappeared.", "thorough", hasThorough),
    modeCard("Quick check", "Re-reads lists, dates, submission states and grades. Bodies are re-read only if Canvas shows a newer update time.", "quick", false));
  const signBanner = !signedIn ? h("div", { class: "banner warn" }, h("div", { class: "b-body" }, h("div", { class: "b-title" }, "Not signed in"),
    h("div", { class: "small" }, "Jobs can be queued now; they start after you sign in and resume automatically if a session expires.")),
    inst ? h("button", { class: "primary", onclick: () => openLogin(inst.id) }, "Sign in") : null) : null;

  const jobsCard = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Jobs"), h("span", { class: "sub" }, "refreshes while a job is active")));
  const jobsList = h("div");
  jobsCard.append(jobsList);
  const renderJobs = (list) => mount(jobsList, list.length ? h("ul", { class: "list" }, list.map(jobLi)) : empty("No jobs yet", "Start a baseline above."));
  renderJobs(jobs.jobs);
  if (jobs.jobs.some((j) => ["running", "queued", "cancel_requested"].includes(j.state))) {
    S.jobPoll = setInterval(async () => {
      const j = await api(A("/jobs") + qs({ limit: 15 })).catch(() => null);
      if (!j) return;
      renderJobs(j.jobs);
      if (!j.jobs.some((x) => ["running", "queued", "cancel_requested"].includes(x.state))) { clearInterval(S.jobPoll); refreshSession(true); }
    }, 2000);
  }
  const rs = await api(A("/refresh-settings")).catch(() => null);
  const auto = rs ? h("div", { class: "card", style: "margin-top:14px" }, h("div", { class: "card-head" }, h("h2", null, "Automatic refresh"),
    pill(rs.minutes ? "info" : "muted", rs.minutes ? `Every ${rs.minutes} min` : "Off")),
    h("p", { class: "small muted" }, "While Canvas Hub is running and you are signed in, run a quick check on this schedule. It never opens a sign-in window, and stops when you quit the app (nothing is added to your system's scheduler). New grades, edited due dates and announcements then appear under the 🔔 bell."),
    h("div", { class: "row" }, h("select", { "aria-label": "Refresh interval", onchange: async (e) => {
      const r = await guard(() => api(A("/refresh-settings"), { method: "PUT", body: { minutes: Number(e.target.value) } }));
      if (r) { toast(r.minutes ? `Quick check every ${r.minutes} minutes` : "Automatic refresh off"); route(); } } },
      [[0, "Off"], [30, "Every 30 minutes"], [60, "Every hour"], [120, "Every 2 hours"], [240, "Every 4 hours"]].map(([v, l]) => h("option", { value: v, selected: rs.minutes === v ? true : null }, l))),
      h("span", { class: "tiny muted" }, rs.last_completed_at ? `Last completed ${ago(rs.last_completed_at)}` : "Runs after your first collection"),
      !rs.runs_here ? h("span", { class: "tiny warn-text" }, "This process does not run collection jobs.") : null)) : null;
  const inboxOff = !scope.categories.includes("inbox") ? h("div", { class: "banner info", style: "margin-top:14px" }, h("div", { class: "b-body" },
    h("div", { class: "b-title" }, "Your Canvas Inbox is not collected"),
    h("div", { class: "small" }, "Messages from instructors and groups stay outside Canvas Hub until you include the Inbox. Messages are read without marking them as read, and nothing is ever sent from here unless you confirm a write.")),
    h("button", { onclick: async () => { const r = await guard(() => api(A("/scope"), { method: "PUT", body: { categories: [...scope.categories, "inbox"] } })); if (r) { toast("Inbox included from the next collection"); route(); } } }, "Include Inbox")) : null;
  mount(main, pageHead("Collection", "You start the first collection. After that, Canvas Hub can re-check on a schedule while it is running."), signBanner, modes,
    inboxOff, auto, h("div", { style: "margin-top:14px" }, jobsCard), scopeEditor(scope, courses));
}

function modeCard(title, text, mode, primary) {
  return h("div", { class: "card" }, h("h2", null, title), h("p", { class: "muted small" }, text),
    h("button", { class: primary ? "primary" : "", onclick: () => startJob(mode) }, `Start ${title.toLowerCase()}`));
}

function jobLi(j) {
  const t = j.tasks || {};
  const total = Object.values(t).reduce((a, b) => a + b, 0);
  const done = (t.done || 0) + (t.failed || 0) + (t.blocked || 0) + (t.skipped || 0);
  const pct = total ? Math.round((done / total) * 100) : 0;
  const act = (a) => async () => { await guard(() => api(A(`/jobs/${j.id}/${a}`), { method: "POST" })); route(); };
  return h("li", null, h("div", { class: "spread" },
    h("div", { class: "row" }, h("span", { class: "strong" }, `Job ${j.id}`), h("span", { class: "muted" }, { baseline: "Baseline", thorough: "Thorough refresh", quick: "Quick check", import: "Import" }[j.mode] || j.mode),
      pill(JOB_CLASS[j.state] || "muted", JOB_LABEL[j.state] || j.state)),
    h("div", { class: "row" },
      ["running", "queued"].includes(j.state) ? h("button", { class: "sm", onclick: act("pause") }, "Pause") : null,
      j.resumable ? h("button", { class: "sm primary", onclick: act("resume") }, "Resume") : null,
      j.cancellable ? h("button", { class: "sm danger", onclick: act("cancel") }, "Cancel") : null)),
    h("div", { class: "small", style: "margin-top:4px" }, j.message || ""),
    total && j.kind === "collect" ? h("div", { style: "margin-top:6px" }, h("div", { class: "progress" }, h("span", { style: `width:${pct}%` })),
      h("div", { class: "li-meta" }, h("span", null, `${t.done || 0} done`), t.pending ? h("span", null, `${t.pending} pending`) : null,
        t.failed ? h("span", null, `${t.failed} failed`) : null, t.blocked ? h("span", null, `${t.blocked} blocked`) : null,
        j.stats && j.stats.current_task && j.state === "running" ? h("span", { class: "mono" }, j.stats.current_task) : null,
        j.stats && j.stats.view_completion_skipped ? h("span", null, `${j.stats.view_completion_skipped} view-to-complete item(s) not opened`) : null)) : null,
    h("div", { class: "li-meta" }, h("span", null, "created " + fmtDate(j.created_at)), j.finished_at ? h("span", null, "finished " + ago(j.finished_at)) : null,
      j.stats && j.stats.events ? h("span", null, `${j.stats.events} change events`) : null, j.error ? h("span", { class: "muted" }, j.error) : null));
}

function scopeEditor(scope, courses) {
  const sel = Array.isArray(scope.courses) ? "list" : scope.courses;
  const s = JSON.parse(JSON.stringify(scope));
  const courseMode = h("select", { onchange: (e) => { s.courses = e.target.value === "list" ? courses.map((c) => c.course_id) : e.target.value; renderList(); } },
    [["current", "Current courses (default)"], ["all", "All accessible courses, including past"], ["list", "Selected courses"]].map(([v, l]) => h("option", { value: v, selected: v === sel ? true : null }, l)));
  const list = h("div", { class: "chips", style: "margin-top:8px" });
  const renderList = () => mount(list, Array.isArray(s.courses) ? courses.map((c) => h("button", { class: "chip" + (s.courses.includes(c.course_id) ? " on" : ""),
    onclick: (e) => { const on = s.courses.includes(c.course_id); s.courses = on ? s.courses.filter((x) => x !== c.course_id) : [...s.courses, c.course_id]; e.target.classList.toggle("on", !on); } }, c.title)) : null);
  renderList();
  const catBoxes = Object.entries(CAT_LABEL).filter(([k]) => k !== "courses").map(([k, l]) => h("label", { class: "check small", style: "min-width:180px" },
    h("input", { type: "checkbox", checked: s.categories.includes(k) ? true : null, onchange: (e) => { s.categories = e.target.checked ? [...s.categories, k] : s.categories.filter((x) => x !== k); } }),
    l, k === "inbox" ? h("span", { class: "muted tiny" }, " (opt-in; reading may affect read state)") : k === "groups" ? h("span", { class: "muted tiny" }, " (opt-in)") : null));
  const num = (k, label) => h("label", { class: "field" }, h("span", null, label), h("input", { type: "number", min: 0, max: 3650, value: s[k], onchange: (e) => { s[k] = Number(e.target.value); } }));
  const viewC = h("input", { type: "checkbox", checked: s.allow_view_completion ? true : null, onchange: (e) => {
    if (e.target.checked && !confirm("Opening a 'must view' module item marks that requirement complete in Canvas (it may count toward assessed progress). Allow Canvas Hub to open such items?")) { e.target.checked = false; return; }
    s.allow_view_completion = e.target.checked; } });
  return h("div", { class: "card", style: "margin-top:14px" }, h("div", { class: "card-head" }, h("h2", null, "Scope"), h("span", { class: "sub" }, "remembered for this account")),
    h("div", { class: "stack" },
      h("label", { class: "field" }, h("span", null, "Courses"), courseMode), list,
      h("div", null, h("div", { class: "small strong", style: "margin-bottom:4px" }, "Content"), h("div", { class: "row", style: "gap:6px 16px" }, catBoxes)),
      h("div", { class: "grid cols-4" }, num("calendar_days_back", "Calendar: days back"), num("calendar_days_ahead", "Calendar: days ahead"), num("max_file_mb", "Max file size (MB)"),
        h("label", { class: "field" }, h("span", null, "Discussion replies"), h("select", { onchange: (e) => { s.discussion_replies = e.target.value; } },
          [["staff_only", "Staff replies only"], ["counts_only", "Counts only"]].map(([v, l]) => h("option", { value: v, selected: v === s.discussion_replies ? true : null }, l))))),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: s.download_files ? true : null, onchange: (e) => { s.download_files = e.target.checked; } }), "Download files and extract their text"),
      h("label", { class: "check" }, viewC, "Allow opening view-to-complete module items", h("span", { class: "muted small" }, " (off by default)")),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: async () => { const r = await guard(() => api(A("/scope"), { method: "PUT", body: s })); if (r) { toast("Scope saved"); await loadIdentities(); } } }, "Save scope"))));
}

// ---------- settings ----------
// Optional OCR (app-wide): text from photos and scanned PDFs, with a confidence flag per document.
function ocrCard(s) {
  const on = h("input", { type: "checkbox", checked: s.enabled ? true : null, disabled: s.tesseract ? null : true });
  const lang = h("input", { type: "text", value: s.lang || "eng", "aria-label": "OCR languages", style: "width:140px" });
  const result = h("div", { class: "small", "aria-live": "polite" });
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Text from scans and photos (OCR)"),
    pill(s.enabled && s.tesseract ? "ok" : "muted", s.enabled && s.tesseract ? "On" : "Off")),
    h("p", { class: "small muted" }, `Reads text from photographed forms and scanned PDFs on this computer with tesseract. Each document gets a confidence score; below ${s.low_confidence_below}% it is flagged, because handwriting and formulas are often misread. Nothing is uploaded.`),
    s.tesseract ? h("p", { class: "small" }, `Found ${s.version || "tesseract"}${s.pdf ? " and pdftoppm (scanned PDFs)" : " — scanned PDFs also need pdftoppm (brew install poppler)"}.`)
      : h("div", { class: "banner warn" }, h("div", { class: "b-body" }, h("div", { class: "b-title" }, "tesseract is not installed"), h("div", { class: "small" }, s.install_hint))),
    h("label", { class: "check" }, on, h("strong", null, "Read text from scans and photos")),
    h("div", { class: "row", style: "margin-top:6px" }, h("label", { class: "small" }, "Languages ", lang), h("span", { class: "tiny muted" }, "e.g. eng, or eng+chi_tra (language packs must be installed)")),
    h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "primary", onclick: async () => {
      const r = await guard(() => api("/api/ocr", { method: "PUT", body: { enabled: !!on.checked, lang: lang.value.trim() || "eng" } }));
      if (r) { toast(r.enabled ? "OCR is on" : "OCR is off"); route(); } } }, "Save"),
      s.enabled && s.tesseract && s.pending ? h("button", { onclick: async (e) => {
        e.target.disabled = true; result.textContent = "Reading…";
        const r = await guard(() => api("/api/ocr/run", { method: "POST", body: { limit: 10 } }));
        e.target.disabled = false;
        if (r) mount(result, h("ul", { class: "list" }, r.processed.map((x) => h("li", null, x.filename || x.resource_id.slice(0, 12), " — ",
          pill(x.status === "ocr" ? "ok" : "warn", x.status === "ocr" ? "text read" : x.status === "ocr_low_confidence" ? "low confidence" : x.status),
          x.confidence != null ? ` ${Math.round(x.confidence)}%` : ""))), h("p", { class: "tiny muted" }, `${r.pending} still to read`));
      } }, `Read ${Math.min(10, s.pending)} of ${s.pending} stored document${s.pending === 1 ? "" : "s"}`) : null,
      s.enabled && s.tesseract && s.failed ? h("button", { class: "ghost", onclick: async () => {
        const r = await guard(() => api("/api/ocr/run", { method: "POST", body: { limit: 10, retry_failed: true } }));
        if (r) { toast(`${r.failed} still could not be read`); route(); } } }, `Retry ${s.failed} that could not be read`) : null),
    result);
}

VIEWS.settings = async (main) => {
  const [backups, info] = await Promise.all([api("/api/backups"), api("/api/info")]);
  const inst = currentInstance();
  const cards = [];
  cards.push(h("div", { class: "card" }, h("h2", null, "Backups"), h("p", { class: "muted small" }, `A backup contains the database and all stored documents. Browser sign-in data is never included. Folder: ${backups.directory}`),
    h("div", { class: "row" }, h("button", { class: "primary", onclick: async () => { const r = await guard(() => api("/api/backups", { method: "POST" })); if (r) { toast(`Backup created: ${r.name}`); route(); } } }, "Create backup"),
      S.accountId ? h("button", { onclick: async () => { const r = await guard(() => api(A("/export"), { method: "POST" })); if (r) { toast(`Export created: ${r.name}`); route(); } } }, "Export this account (portable)") : null),
    backups.files.length ? h("table", { class: "t", style: "margin-top:10px" }, h("tbody", null, backups.files.map((f) => h("tr", null, h("td", { class: "mono small wrap-anywhere" }, f.name), h("td", { class: "small muted nowrap" }, `${Math.round(f.size / 1024)} KB`),
      h("td", { class: "nowrap" }, h("a", { class: "btn sm", href: `/api/backups/${encodeURIComponent(f.name)}/download` }, "Download"), " ",
        f.kind === "backup" ? h("button", { class: "sm", onclick: async () => { const r = await guard(() => api(`/api/backups/${encodeURIComponent(f.name)}/verify`, { method: "POST" })); if (r) toast(`OK: ${r.counts.items} items, ${r.resource_files} files`); } }, "Verify") : null, " ",
        f.kind === "backup" ? h("button", { class: "sm danger", onclick: async () => {
          if (!confirm("Restore this backup? Current data will be moved aside (not deleted).")) return;
          const r = await guard(() => api(`/api/backups/${encodeURIComponent(f.name)}/restore`, { method: "POST" }));
          if (r) { toast("Restored"); await loadIdentities(); route(); } } }, "Restore") : null))))) : null));
  const file = h("input", { type: "file", accept: ".zip", "aria-label": "Export archive to import (.zip)" });
  cards.push(h("div", { class: "card" }, h("h2", null, "Import"), h("p", { class: "muted small" }, "Import a portable export created by Canvas Hub. Data is attached to the account named inside the export and never merged into a different account."),
    h("div", { class: "row" }, file, h("button", { onclick: async () => {
      if (!file.files[0]) return;
      const buf = await file.files[0].arrayBuffer();
      const r = await guard(() => api("/api/import-archive", { method: "POST", body: buf, raw: true, contentType: "application/zip" }));
      if (r) { toast(`Imported: ${r.stats.items_new} new items, ${r.annotations_added} notes`); await loadIdentities(); }
    } }, "Import"))));
  if (inst) cards.push(h("div", { class: "card" }, h("h2", null, "Sign-in session"), h("p", { class: "muted small" }, "The app keeps its own browser profile for this Canvas. Signing out or clearing it never deletes course data."),
    h("div", { class: "row" }, h("button", { onclick: async () => { await guard(() => api(`/api/instances/${inst.id}/logout`, { method: "POST", body: {} })); await refreshSession(false); toast("Signed out locally"); } }, "Sign out"),
      h("button", { class: "danger", onclick: async () => { if (!confirm("Delete the saved browser profile (cookies, saved sign-in) for this Canvas?")) return; await guard(() => api(`/api/instances/${inst.id}/clear-session`, { method: "POST" })); await refreshSession(false); toast("Saved sign-in cleared"); } }, "Clear saved sign-in"))));
  if (S.accountId) {
    const conf = h("input", { type: "text", placeholder: "Type DELETE" });
    cards.push(h("div", { class: "card" }, h("h2", null, "Delete stored course data"), h("p", { class: "muted small" }, "Removes this account's collected items, history, documents and notes from this computer. Your browser sign-in is unaffected. Make a backup first if unsure."),
      h("div", { class: "row" }, conf, h("button", { class: "danger", onclick: async () => {
        const r = await guard(() => api(A("/delete-data"), { method: "POST", body: { confirm: conf.value } }));
        if (r) { toast("Course data deleted"); S.accountId = null; await loadIdentities(); location.hash = "#/setup"; }
      } }, "Delete data"))));
  }
  if (S.accountId) { const ws = await api(A("/writes")).catch(() => null); if (ws) cards.push(C.writesSettingsCard(ws)); }
  if (S.accountId) { const ch = await api(A("/custom-html")).catch(() => null); if (ch) cards.push(C.customHtmlCard(ch)); }
  const ocrS = await api("/api/ocr").catch(() => null);
  if (ocrS) cards.push(ocrCard(ocrS));
  const acfg = await api("/api/assistant/config").catch(() => null);
  if (acfg) {
    const active = acfg.profiles.find((x) => x.id === acfg.active_profile);
    cards.push(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Assistant")),
      h("p", { class: "muted small" }, acfg.profiles.length ? `${acfg.profiles.length} saved setup${acfg.profiles.length > 1 ? "s" : ""}; in use: ${active ? `${active.name} · ${active.model || "default model"}` : "none"}.` : "Not set up yet."),
      h("a", { class: "btn sm", href: "#/assistant/settings" }, "Open assistant settings")));
  }
  cards.push(mcpCard(info));
  cards.push(h("div", { class: "card" }, h("h2", null, "About"), h("div", { class: "kv" },
    [["Version", info.version], ["Data folder", info.data_home], ["Collector", info.collection_available ? "available in this process" : "not available"],
      ["Codex integration", "Direct Codex connection reserved for Codex to build (CODEX_HANDOFF.md). Codex can already use the MCP server above."]].map(([k, v]) => [h("div", { class: "k" }, k), h("div", { class: "wrap-anywhere" }, v), h("div", { class: "t" })]))));
  mount(main, pageHead("Data & settings"), h("div", { class: "stack" }, cards));
};

// ---------- assistant ----------
const STEP_LABEL = {
  search: (a) => `Searched “${a.query}”`, get_deadlines: () => "Checked deadlines", get_item: (a) => `Read item ${a.item_id}`,
  get_item_history: (a) => `Read history of item ${a.item_id}`, list_items: () => "Listed items", get_announcements: () => "Read announcements",
  get_changes: () => "Checked recent changes", get_coverage: () => "Checked coverage", get_freshness: () => "Checked freshness",
  read_document: () => "Read a document", get_signin_status: () => "Checked Canvas sign-in", list_jobs: () => "Listed jobs",
  get_job: (a) => `Checked job ${a.job_id}`, list_courses: () => "Listed courses", list_accounts: () => "Listed accounts",
  list_notes: () => "Read your notes", create_note: (a) => `Created a local ${a.kind === "task" ? "to-do" : "note"}${a.title ? ` “${a.title}”` : ""}`,
  update_note: () => "Updated a local note", delete_note: () => "Deleted a local note",
  start_collection: (a) => `Started a ${a.mode || "quick"} check of Canvas`, wait_for_job: () => "Waited for the Canvas check",
  cancel_job: () => "Cancelled a job", update_scope: () => "Changed the collection scope",
  get_timetable: () => "Built your week", list_class_times: () => "Read your class times",
  add_class_time: (a) => `Saved class time “${a.title}”`, remove_class_time: () => "Removed a class time",
  list_view_requests: () => "Checked agent requests", request_external_view: (a) => `Asked a connected agent to read ${a.title || a.url}`,
};

function mdInline(text) {
  const out = [];
  const re = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\))/g;
  let last = 0; let m;
  while ((m = re.exec(text))) {
    out.push(text.slice(last, m.index));
    if (m[2]) out.push(h("strong", null, m[2]));
    else if (m[3]) out.push(h("code", null, m[3]));
    else out.push(h("a", { href: m[5], target: "_blank", rel: "noopener noreferrer" }, m[4]));
    last = re.lastIndex;
  }
  out.push(text.slice(last));
  return out;
}
function renderMarkdown(src) {
  // Minimal, DOM-built markdown (no HTML from the model is ever interpreted).
  const root = h("div", { class: "md" });
  let list = null;
  for (const raw of (src || "").split("\n")) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/); const num = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const head = line.match(/^(#{1,3})\s+(.*)$/);
    if (bullet || num) {
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) { list = h(tag); root.append(list); }
      list.append(h("li", null, mdInline((bullet || num)[1])));
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    if (head) root.append(h(head[1].length === 1 ? "h3" : "h4", null, mdInline(head[2])));
    else root.append(h("p", null, mdInline(line)));
  }
  return root;
}

function stepRow(call, result) {
  const label = (STEP_LABEL[call.name] || ((a) => call.name))(call.input || {});
  const bad = call.input_error || (result && result.is_error);
  return h("details", { class: "step" + (bad ? " bad" : "") },
    h("summary", null, h("span", { class: "step-dot" }), label, result ? null : h("span", { class: "muted tiny" }, " …")),
    call.input && Object.keys(call.input).length ? h("pre", { class: "text tiny" }, JSON.stringify(call.input, null, 2)) : null,
    call.input_error ? h("div", { class: "small", style: "color:var(--bad)" }, call.input_error) : null,
    result ? h("pre", { class: "text tiny" }, result.preview) : null);
}

function renderMessages(list, msgs, ctx = {}) {
  const results = {};
  for (const m of msgs) if (m.role === "tool") for (const r of m.results) results[r.id] = r;
  const out = [];
  for (const m of msgs) {
    if (m.role === "user") out.push(h("div", { class: "msg user" }, h("div", { class: "bubble" }, m.text),
      m.setup ? h("div", { class: "msg-meta tiny muted" }, `${m.setup}${m.reasoning ? ` · reasoning ${m.reasoning}` : ""}`) : null));
    else if (m.role === "assistant") {
      const parts = [];
      if (m.text) parts.push(ctx.typed ? ctx.typed(m) : renderMarkdown(m.text));
      const plain = (m.tool_calls || []).filter((c) => !(results[c.id] && results[c.id].display));
      if (plain.length) parts.push(h("div", { class: "steps" }, plain.map((c) => stepRow(c, results[c.id]))));
      for (const c of m.tool_calls || []) if (results[c.id] && results[c.id].display) parts.push(componentView(results[c.id].display, ctx, c.id));
      if (m.stop === "refusal") parts.push(h("div", { class: "small muted" }, "The model declined to continue."));
      if (parts.length) out.push(h("div", { class: "msg assistant" }, parts));
    }
  }
  mount(list, out);
}

let assistantPoll = null;
VIEWS.assistant = async (main, parts, params) => {
  clearInterval(assistantPoll);
  const cfg = await api("/api/assistant/config");
  const onSettings = parts[0] === "settings";
  if (onSettings || !cfg.configured) main.classList.remove("fill");       // only the chat itself fills the window
  const head = pageHead("Assistant", "Ask about your courses. Answers come from your Canvas Hub data; with permission it can check Canvas for updates and keep your notes.");
  // Focus mode: the window holds just the chat list, the chat, these tabs and the composer (remembered in this browser).
  const focusBtn = h("button", { class: "sm ghost focus-toggle", title: "Show only the chat (Esc to leave)",
    onclick: () => setChatFocus(!document.body.classList.contains("chat-focus")) });
  const tabs = h("div", { class: "tabs" },
    h("button", { class: onSettings ? "" : "active", onclick: () => { location.hash = "#/assistant"; } }, "Chat"),
    h("button", { class: onSettings ? "active" : "", onclick: () => { location.hash = "#/assistant/settings"; } },
      "Settings", cfg.profiles.length ? h("span", { class: "tab-count" }, String(cfg.profiles.length)) : null),
    h("span", { class: "grow" }), focusBtn);
  setChatFocus(store.get("chatFocus") === "1", [focusBtn]);
  if (onSettings) return assistantSettingsView(main, head, tabs, cfg, params);
  if (!cfg.configured) {
    mount(main, head, tabs, empty("Set up the assistant first", "Add an API provider or connect your Codex CLI login. You can save several setups and switch between them.",
      h("a", { class: "btn primary", href: "#/assistant/settings" }, "Open settings")));
    return;
  }
  const convId = parts[0] ? Number(parts[0]) : null;
  const convs = (await api(A("/assistant/conversations"))).conversations;

  const convBox = h("div", { class: "conv-scroll" });
  const showConvs = (list) => mount(convBox, list.map((c) => h("a", { class: "conv" + (c.id === current ? " on" : ""), href: `#/assistant/${c.id}` },
    h("span", { class: "conv-title" }, c.title || "Untitled"), h("span", { class: "tiny muted" }, ago(c.updated_at)))));
  const reloadConvs = async () => { const d = await api(A("/assistant/conversations")).catch(() => null); if (d) showConvs(d.conversations); };
  const sideList = h("div", { class: "conv-list" },
    h("a", { class: "btn sm" + (convId ? "" : " primary"), href: "#/assistant" }, "+ New chat"), convBox);
  const log = h("div", { class: "chat-log" });
  const status = h("div", { class: "chat-status" });
  const scroller = h("div", { class: "chat-scroll" });
  const input = h("textarea", { rows: 2, placeholder: "Ask about deadlines, announcements, materials, grades…  (Enter to send, Shift+Enter for a new line)" });
  const sendBtn = h("button", { class: "primary" }, "Send");
  const stopBtn = h("button", { class: "danger", hidden: true }, "Stop");
  let current = convId; let lastSeq = 0; let all = []; let stick = true; let wasBusy = false;
  showConvs(convs);
  const listTop = main.querySelector(".conv-scroll")?.scrollTop || 0;      // the chat list being replaced, if any

  // ---- replies appear word by word: from the live stream, or all at once for providers that do not stream ----
  const calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const typers = new Map();        // message seq, or "live" for the reply being written -> { el, target, shown, drawn }
  let openedAt = null;             // messages up to this seq were there when the chat opened: shown at once
  let tick = null;
  const follow = () => { if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 90) scroller.scrollTop = scroller.scrollHeight; };
  const draw = (t) => { if (t.drawn !== t.shown) { mount(t.el, renderMarkdown(t.target.slice(0, t.shown))); t.drawn = t.shown; } };
  const step = () => {
    tick = null;
    let more = false;
    for (const t of typers.values()) {
      if (t.shown >= t.target.length) { draw(t); continue; }
      const left = t.target.length - t.shown;
      let n = t.shown + Math.max(3, Math.ceil(left / 20));             // a few words a frame; faster when behind
      const gap = t.target.slice(n).search(/\s/);                     // and always end on a whole word
      t.shown = gap < 0 ? t.target.length : n + gap;
      draw(t);
      if (t.shown < t.target.length) more = true;
    }
    follow();
    if (more) tick = setTimeout(step, 30);
  };
  const typer = (key, text, animate) => {
    let t = typers.get(key);
    if (!t) { t = { el: h("div", { class: "typed" }), target: "", shown: 0, drawn: -1 }; typers.set(key, t); }
    if (!text.startsWith(t.target.slice(0, t.shown))) t.shown = 0;   // a different text: start over
    t.target = text;
    if (!animate || calm) t.shown = text.length;
    if (t.shown < text.length) { if (!tick) tick = setTimeout(step, 30); } else draw(t);
    return t.el;
  };
  const typed = (m) => {
    const fresh = openedAt !== null && m.seq > openedAt;
    const live = typers.get("live");
    if (fresh && !typers.has(m.seq) && live && m.text.startsWith(live.target.slice(0, live.shown))) {
      typers.set(m.seq, live);       // the saved reply takes over from the draft without starting again
      typers.delete("live");
    }
    return typer(m.seq, m.text, fresh);
  };
  const chatCtx = { send: (t) => send(t), typed };   // one per chat view: components can tell a new view from a re-render
  let refreshing = false;
  const refresh = async () => {
    if (!current || refreshing) return;
    refreshing = true;
    const d = await api(A(`/assistant/conversations/${current}`) + qs({ after: 0 })).catch(() => null);
    refreshing = false;
    if (!d) return;
    if (openedAt === null) openedAt = d.messages.length ? d.messages[d.messages.length - 1].seq : 0;
    // Follow new output only while the reader is at the bottom of the chat.
    const atBottom = stick || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
    const prevTop = scroller.scrollTop;
    all = d.messages;
    renderMessages(log, all, chatCtx);
    if (d.draft && d.draft.text) {
      log.append(h("div", { class: "msg assistant live", "aria-busy": "true" }, typer("live", d.draft.text, true)));
    } else typers.delete("live");
    const st = d.conversation.run_state;
    const busy = st === "running" || st === "cancelling";
    sendBtn.disabled = busy; stopBtn.hidden = !busy;
    mount(status, busy ? h("span", { class: "working" }, st === "cancelling" ? "Stopping…" : "Working…")
      : st === "error" ? h("div", { class: "banner bad", style: "margin:0" }, h("div", { class: "b-body" }, h("div", { class: "b-title" }, "The assistant stopped"), h("div", { class: "small" }, d.conversation.run_error || ""))) : null);
    const seq = all.length ? all[all.length - 1].seq : 0;
    scroller.scrollTop = atBottom && (seq !== lastSeq || stick || busy) ? scroller.scrollHeight : prevTop;
    lastSeq = seq; stick = false;
    if (wasBusy && !busy) reloadConvs();
    wasBusy = busy;
    if (!busy) clearInterval(assistantPoll);
  };
  const send = async (text) => {
    const t = (text ?? input.value).trim();
    if (!t) return;
    sendBtn.disabled = true;
    const r = await guard(() => api(A("/assistant/messages"), { method: "POST", body: { text: t, conversation_id: current, profile_id: setupSel.value || cfg.active_profile,
      reasoning: reasoningSel.hidden ? "" : reasoningSel.value } }));
    if (!r) { sendBtn.disabled = false; return; }
    input.value = "";
    const emptyEl = document.getElementById("chat-empty");
    if (emptyEl) emptyEl.remove();
    if (current !== r.conversation_id) { current = r.conversation_id; history.replaceState(null, "", `#/assistant/${current}`); reloadConvs(); }
    if (openedAt === null) openedAt = 0;      // a new chat: everything from here on is typed out
    stick = true;
    clearInterval(assistantPoll);
    assistantPoll = setInterval(refresh, 400);   // often enough for the reply to flow while it is written
    refresh();
  };
  sendBtn.addEventListener("click", () => send());
  stopBtn.addEventListener("click", async () => { await guard(() => api(A(`/assistant/conversations/${current}/stop`), { method: "POST" })); refresh(); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });

  const suggestions = h("div", { class: "chips suggest" }, ["What's due in the next 7 days?", "Plan my study time this week",
    "Anything new since yesterday?", "Check Canvas for new announcements", "Show my scores"].map((s) => h("button", { class: "chip", onclick: () => send(s) }, s)));
  // Setup and reasoning are chosen right here for each message. The reasoning
  // level is remembered per setup in this browser only; it is not a saved setting.
  const usable = cfg.profiles.filter((x) => x.configured);
  const setupSel = h("select", { class: "chat-pick", title: "Saved API setup (provider and model) for the next message" },
    usable.map((x) => h("option", { value: x.id, selected: x.id === cfg.active_profile ? true : null }, `${x.name} · ${x.model || "default model"}`)));
  const reasoningSel = h("select", { class: "chat-pick", title: "Reasoning effort for the next message" });
  const fillReasoning = () => {
    const prof = usable.find((x) => x.id === setupSel.value);
    const levels = (prof && prof.reasoning_levels) || [];
    const saved = store.get(`reasoning.${setupSel.value}`) || "";
    mount(reasoningSel, ["", ...levels].map((v) => h("option", { value: v, selected: v === saved ? true : null }, `Reasoning: ${v || "default"}`)));
    reasoningSel.hidden = !levels.length;
  };
  fillReasoning();
  setupSel.addEventListener("change", () => {
    fillReasoning();
    // Also make it the default for new chats; each message names its setup anyway.
    api(`/api/assistant/profiles/${encodeURIComponent(setupSel.value)}/activate`, { method: "POST" }).catch(() => null);
  });
  reasoningSel.addEventListener("change", () => store.set(`reasoning.${setupSel.value}`, reasoningSel.value));
  const perms = cfg.permissions;
  const footer = h("div", { class: "chat-foot tiny muted" },
    h("span", null, `Can ${["read your data", perms.notes ? "edit your notes" : null, perms.collect ? "check Canvas" : null, perms.settings ? "change scope" : null].filter(Boolean).join(", ")}. Questions and the data it reads go to the chosen provider.`));
  scroller.append(log, status);
  if (!current) scroller.append(h("div", { class: "chat-empty", id: "chat-empty" }, h("div", { class: "e-title" }, "What would you like to know?"), suggestions));
  const chat = h("div", { class: "chat" }, scroller,
    h("div", { class: "composer" }, input,
      h("div", { class: "composer-bar" }, setupSel, reasoningSel, h("span", { class: "grow" }), stopBtn, sendBtn)), footer);
  const layout = h("div", { class: "assistant-layout" }, sideList, chat);
  main.classList.add("fill");
  mount(main, head, tabs, layout);
  // The chat reaches from where it starts on screen to the bottom of the window, so the message box stays put.
  // Scrolling the page moves the heading out of view and the chat grows into the space, up to the tabs, which
  // stick below whatever stays at the top of the window. The space it can still grow into is kept below it
  // as a margin, so the page is exactly that much taller and growing never changes how far it scrolls.
  const fit = () => {
    if (!layout.isConnected) {
      window.removeEventListener("resize", fit); window.removeEventListener("scroll", fit);
      return;
    }
    // What stays at the top of the window: the sticky bars across it (not the full-height sidebar beside it).
    // Their size, not where they are now, so this never depends on how far the page is scrolled.
    const pinned = [$("#sidebar"), $("#topbar")].filter((el) => el && getComputedStyle(el).position === "sticky"
      && el.offsetHeight < window.innerHeight / 2);
    const stickTop = Math.max(0, ...pinned.map((el) => el.offsetHeight));
    tabs.style.top = `${stickTop}px`;
    const docTop = layout.getBoundingClientRect().top + window.scrollY;
    const settled = document.body.classList.contains("chat-focus") ? docTop      // nothing above it scrolls away
      : stickTop + tabs.offsetHeight + parseFloat(getComputedStyle(tabs).marginBottom);
    const full = Math.max(160, window.innerHeight - settled - 16);
    const top = Math.max(layout.getBoundingClientRect().top, settled);
    const shown = Math.min(full, Math.max(160, window.innerHeight - top - 16));
    const atEnd = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 4;
    layout.style.height = `${shown}px`;
    layout.style.marginBottom = `${full - shown}px`;
    if (atEnd) scroller.scrollTop = scroller.scrollHeight;      // keep the latest message in view as it shrinks
  };
  fit();
  if (main.dataset.keepScroll) { window.scrollTo(0, Number(main.dataset.keepScroll)); fit(); }   // switched chats
  convBox.scrollTop = listTop;
  window.addEventListener("resize", fit);
  window.addEventListener("scroll", fit, { passive: true });
  if (current) { await refresh(); assistantPoll = setInterval(refresh, 400); }
  input.focus({ preventScroll: true });
};

function assistantSettingsView(main, head, tabs, cfg, params) {
  const sel = params.get("p") || cfg.active_profile || "new";
  const prof = cfg.profiles.find((x) => x.id === sel) || null;
  const list = h("div", { class: "card setup-list" },
    h("div", { class: "card-head" }, h("h2", null, "Saved setups"), h("a", { class: "btn sm", href: "#/assistant/settings?p=new" }, "+ Add setup")),
    cfg.profiles.length ? cfg.profiles.map((x) => h("div", { class: "setup-row" + (x.id === sel ? " on" : "") },
      h("a", { class: "setup-main", href: `#/assistant/settings?p=${encodeURIComponent(x.id)}` },
        h("span", { class: "strong" }, x.name), h("span", { class: "tiny muted" }, [x.model || "default model", x.api_key_saved ? "key saved" : null].filter(Boolean).join(" · "))),
      x.id === cfg.active_profile ? pill("ok", "In use") : h("button", { class: "sm ghost", disabled: x.configured ? null : true, onclick: async () => {
        if (await guard(() => api(`/api/assistant/profiles/${encodeURIComponent(x.id)}/activate`, { method: "POST" }))) { toast(`Now using ${x.name}`); route(); }
      } }, "Use"))) : h("p", { class: "muted small" }, "No setups yet. Add an API provider or connect your Codex CLI login."));
  mount(main, head, tabs, h("div", { class: "settings-layout" }, list,
    h("div", { class: "stack" }, assistantSettingsCard(cfg, prof), assistantPermissionsCard(cfg))));
}

function assistantPermissionsCard(cfg) {
  const pNotes = h("input", { type: "checkbox", checked: cfg.permissions.notes ? true : null });
  const pCollect = h("input", { type: "checkbox", checked: cfg.permissions.collect ? true : null });
  const pSettings = h("input", { type: "checkbox", checked: cfg.permissions.settings ? true : null });
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", null, "The assistant may"), h("span", { class: "sub" }, "applies to every setup")),
    h("div", { class: "perm-list" },
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: true, disabled: true }), "Read your stored Canvas data (always)"),
      h("label", { class: "check" }, pNotes, "Create and edit your local notes and to-dos (never sent to Canvas)"),
      h("label", { class: "check" }, pCollect, "Check Canvas for updates using the app's signed-in browser (read-only; same safety rules as Quick check)"),
      h("label", { class: "check" }, pSettings, "Change which courses and sections are collected")),
    h("div", { class: "row", style: "margin-top:14px" }, h("button", { class: "primary", onclick: async () => {
      const r = await guard(() => api("/api/assistant/config", { method: "PUT", body: { permissions: { notes: pNotes.checked, collect: pCollect.checked, settings: pSettings.checked } } }));
      if (r) toast("Permissions saved");
    } }, "Save permissions")));
}

function assistantSettingsCard(cfg, prof) {
  const cur = prof || { id: "new", name: "", preset: null, provider: null, base_url: "", model: "", extra_params: {} };
  const isNew = !prof;
  const name = h("input", { type: "text", value: cur.name || "", placeholder: "e.g. DeepSeek max, Claude for long questions", maxlength: 60 });
  const presetSel = h("select", { "aria-label": "Provider" }, h("option", { value: "" }, "Choose a provider…"),
    cfg.presets.map((p) => h("option", { value: p.id, disabled: p.disabled ? true : null, selected: p.id === cur.preset ? true : null }, p.label)));
  const base = h("input", { type: "url", value: cur.base_url || "", placeholder: "https://…/v1" });
  const model = h("input", { type: "text", value: cur.model || "", placeholder: "model name" });
  const key = h("input", { type: "password", autocomplete: "off", placeholder: cur.api_key_saved ? `saved (${cur.api_key_hint}) — type to replace` : "paste your API key" });
  const keyField = h("label", { class: "field" }, h("span", null, "API key"), key);
  const baseField = h("label", { class: "field" }, h("span", null, "Base URL"), base);
  const extra = h("textarea", { rows: 2, class: "mono", placeholder: "optional, e.g. {\"temperature\": 0.3}" });
  extra.value = cur.extra_params && Object.keys(cur.extra_params).length ? JSON.stringify(cur.extra_params) : "";
  const extraField = h("label", { class: "field", style: "grid-column:1/-1" },
    h("span", null, "Extra request settings (JSON, optional — sent as-is to the provider)"), extra);
  const hint = h("div", { class: "tiny muted" });
  const sync = () => {
    const p = cfg.presets.find((x) => x.id === presetSel.value);
    const isAnthropic = p && p.provider === "anthropic";
    const isCodex = p && p.provider === "codex";
    extraField.hidden = !p || isAnthropic || isCodex;
    baseField.hidden = keyField.hidden = !!isCodex;
    model.placeholder = isCodex ? "Codex default (optional)" : "model name";
    baseField.querySelector("span").textContent = isAnthropic ? "Base URL (leave empty for the Claude API)" : "Base URL";
    if (p && presetSel.value !== cur.preset) { base.value = p.base_url; if (p.model) model.value = p.model; else if (cur.provider !== p.provider) model.value = ""; }
    mount(hint, p ? (isCodex ? "Install Codex CLI 0.163.0 or newer and run `codex login` on this computer, then Save & test. Canvas Hub uses that login without storing its credentials. Leave Model empty to use your Codex default. Questions and selected course data are sent to Codex." : isAnthropic ? "Without a pasted key, ANTHROPIC_API_KEY or an `ant auth login` profile is used." : p.id === "ollama" ? "Runs on your own computer; no key and nothing leaves it." : `Key format: ${p.key_hint || "provider API key"}. For OpenAI-compatible providers OPENAI_API_KEY is used if no key is saved.`) : "");
  };
  presetSel.addEventListener("change", sync); sync();
  const result = h("div", { class: "small" });
  const save = async () => {
    if (!presetSel.value) { toast("Choose a provider first", true); return null; }
    let extraParams = {};
    const isCodex = presetSel.value === "codex";
    if (!isCodex && extra.value.trim()) {
      try { extraParams = JSON.parse(extra.value); } catch { toast("Extra request settings must be valid JSON", true); return null; }
    }
    const body = { profile_id: cur.id, name: name.value, preset: presetSel.value, model: model.value };
    if (!isCodex) Object.assign(body, { base_url: base.value, extra_params: extraParams, ...(key.value ? { api_key: key.value } : {}) });
    const r = await guard(() => api("/api/assistant/config", { method: "PUT", body }));
    if (r) { key.value = ""; cur.id = r.saved_profile; toast("Setup saved"); return r; }   // later saves edit this setup
    return null;
  };
  // One save or test at a time: overlapping clicks would each send profile_id "new" and create duplicates.
  const saveBtn = h("button", { class: "primary" }, "Save");
  const testBtn = h("button", null, "Save & test");
  let busy = false;
  const exclusive = (fn) => async () => {
    if (busy) return;
    busy = true; saveBtn.disabled = testBtn.disabled = true;
    try { await fn(); } finally { busy = false; saveBtn.disabled = testBtn.disabled = false; }
  };
  saveBtn.addEventListener("click", exclusive(async () => { const r = await save(); if (r && card.isConnected) goTo(r.saved_profile); }));
  testBtn.addEventListener("click", exclusive(async () => {
    const r = await save(); if (!r) return;
    mount(result, h("span", { class: "muted" }, "Testing…"));
    const t = await guard(() => api("/api/assistant/test", { method: "POST", body: { profile_id: r.saved_profile } }));
    if (t) mount(result, t.ok ? pill("ok", `Connected · ${t.model || ""}`) : h("span", { style: "color:var(--bad)" }, t.message));
    // Show the new setup's own page, unless the user has moved on meanwhile.
    if (isNew) setTimeout(() => { if (card.isConnected) goTo(r.saved_profile); }, t && t.ok ? 1200 : 4000);
  }));
  const goTo = (pid) => { const h2 = `#/assistant/settings?p=${encodeURIComponent(pid)}`; if (location.hash === h2) route(); else location.hash = h2; };
  const card = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", null, isNew ? "New setup" : cur.name),
      !isNew && cur.id === cfg.active_profile ? pill("ok", "In use") : h("span", { class: "sub" }, "provider settings stored only on this computer")),
    h("div", { class: "banner info" }, h("div", { class: "b-body small" }, cfg.privacy, " Keys are kept in a private file outside your course data, so they are never included in backups or exports.")),
    h("div", { class: "grid cols-2" }, h("label", { class: "field", style: "grid-column:1/-1" }, h("span", null, "Name"), name),
      h("label", { class: "field" }, h("span", null, "Provider"), presetSel), h("label", { class: "field" }, h("span", null, "Model"), model),
      baseField, keyField, extraField),
    hint,
    h("div", { class: "tiny muted" }, "Reasoning effort is not part of a setup: choose it next to the message box for each message."),
    h("div", { class: "row", style: "margin-top:14px" },
      saveBtn, testBtn,
      !isNew && cur.id !== cfg.active_profile ? h("button", { class: "ghost", disabled: cur.configured ? null : true, onclick: async () => {
        if (await guard(() => api(`/api/assistant/profiles/${encodeURIComponent(cur.id)}/activate`, { method: "POST" }))) { toast(`Now using ${cur.name}`); route(); }
      } }, "Use this setup") : null,
      !isNew && cur.api_key_saved ? h("button", { class: "ghost", onclick: async () => {
        if (await guard(() => api("/api/assistant/config", { method: "PUT", body: { profile_id: cur.id, clear_key: true } }))) { toast("Saved key removed"); route(); }
      } }, "Remove saved key") : null,
      !isNew ? h("button", { class: "danger", onclick: async () => {
        if (!confirm(`Delete the setup “${cur.name}” and its saved key?`)) return;
        if (await guard(() => api(`/api/assistant/profiles/${encodeURIComponent(cur.id)}`, { method: "DELETE" }))) {
          toast("Setup deleted");
          if (location.hash === "#/assistant/settings") route(); else location.hash = "#/assistant/settings";   // same hash fires no hashchange
        }
      } }, "Delete setup") : null,
      result));
  return card;
}

function mcpCard(info) {
  const cmd = (info.mcp_command || ["canvas-hub", "mcp"]).join(" ");
  const line = (flags) => `${cmd}${flags}`;
  const copy = (t) => h("button", { class: "sm", onclick: async () => { try { await navigator.clipboard.writeText(t); toast("Copied"); } catch { toast("Copy failed — select the text instead", true); } } }, "Copy");
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Connect other agents (MCP)"), h("span", { class: "sub" }, "Codex, Claude Code, Claude Desktop, …")),
    h("p", { class: "muted small" }, "Any MCP-capable agent can read your Canvas Hub data through a local MCP server. Register this command as a stdio MCP server in that agent's settings. It is read-only unless you add flags."),
    h("div", { class: "kv" },
      [["Read-only", ""], ["+ notes & to-dos", " --allow-notes"], ["+ notes & Canvas checks", " --allow-writes"]].map(([label, flags]) => [
        h("div", { class: "k" }, label), h("div", null, h("code", { class: "wrap-anywhere" }, line(flags))), h("div", { class: "t" }, copy(line(flags)))])),
    h("p", { class: "tiny muted", style: "margin-top:8px" }, "Canvas checks requested over MCP run in this app, which must be open and signed in. Browser sessions and credentials are never exposed to agents."));
}

// ---------- timetable (shared by Planner and assistant components) ----------
const TT_START = 8, TT_END = 23, TT_HOUR = 42;
function minutesOf(iso) { const t = iso.slice(11, 16); return Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5)); }
const KIND_LABEL = { class: "Class", calendar: "Event", deadline: "Due", task: "To-do", study: "Suggested study" };

async function addStudyTask(e) {
  const r = await guard(() => api(A("/annotations"), { method: "POST", body: { kind: "task", title: e.title,
    item_id: e.for_item_id || undefined, local_due: new Date(e.start).toISOString() } }));
  if (r) toast("Added to your to-dos (local only)");
  return r;
}

function ttEvent(e, opts) {
  const s = minutesOf(e.start), en = Math.max(minutesOf(e.end), s + (e.kind === "deadline" ? 28 : 30));
  const total = (TT_END - TT_START) * TT_HOUR;
  const height = Math.max(26, (Math.min(en, TT_END * 60) - Math.max(s, TT_START * 60)) * TT_HOUR / 60);
  // Items outside the visible hours are pinned to the top/bottom edge (their time is still shown).
  const top = Math.min(Math.max(0, (s - TT_START * 60) * TT_HOUR / 60), total - height);
  const el = h("div", { class: `tt-ev ${e.kind}${e.done ? " done" : ""}`, style: `top:${top}px;height:${height}px`,
    title: `${KIND_LABEL[e.kind]}: ${e.title}${e.location ? " · " + e.location : ""}` },
    h("div", { class: "tt-time" }, e.start.slice(11, 16), e.kind !== "deadline" && e.kind !== "task" ? "–" + e.end.slice(11, 16) : ""),
    e.item_id ? h("a", { href: `#/item/${e.item_id}`, class: "tt-title" }, e.title) : h("div", { class: "tt-title" }, e.title),
    e.location ? h("div", { class: "tt-loc" }, e.location) : null,
    e.kind === "study" ? h("button", { class: "sm", onclick: async (ev) => { ev.preventDefault(); if (await addStudyTask(e)) { el.classList.remove("study"); el.classList.add("task"); ev.target.remove(); if (opts.onChange) opts.onChange(); } } }, "+ Add") : null);
  return el;
}

function renderTimetable(week, opts = {}) {
  const hours = [];
  for (let hh = TT_START; hh < TT_END; hh++) hours.push(h("div", { class: "tt-hour", style: `top:${(hh - TT_START) * TT_HOUR}px` }, `${String(hh).padStart(2, "0")}:00`));
  const today = dayKey(new Date().toISOString());
  const grid = h("div", { class: "tt-grid" },
    h("div", { class: "tt-axis" }, h("div", { class: "tt-dayhead" }), h("div", { class: "tt-col-body", style: `height:${(TT_END - TT_START) * TT_HOUR}px` }, hours)),
    week.days.map((d) => h("div", { class: "tt-col" + (d.date === today ? " today" : "") },
      h("div", { class: "tt-dayhead" }, h("span", { class: "dow" }, d.weekday.slice(0, 3)), h("span", { class: "dnum" }, String(Number(d.date.slice(8, 10))))),
      h("div", { class: "tt-col-body", style: `height:${(TT_END - TT_START) * TT_HOUR}px` }, d.events.map((e) => ttEvent(e, opts))))));
  const list = h("div", { class: "tt-list" }, week.days.filter((d) => d.events.length).map((d) => h("div", { class: "tt-list-day" },
    h("div", { class: "day-label" }, `${d.weekday} ${Number(d.date.slice(8, 10))}`),
    d.events.map((e) => h("div", { class: `tt-list-ev ${e.kind}${e.done ? " done" : ""}` },
      h("span", { class: "mono small" }, e.start.slice(11, 16)), pill(e.kind === "deadline" ? "bad plain" : e.kind === "study" ? "warn plain" : "muted plain", KIND_LABEL[e.kind]),
      e.item_id ? h("a", { href: `#/item/${e.item_id}` }, e.title) : h("span", null, e.title),
      e.kind === "study" ? h("button", { class: "sm", onclick: async (ev) => { if (await addStudyTask(e)) ev.target.remove(); } }, "+ Add") : null)))));
  if (!week.days.some((d) => d.events.length)) list.append(h("div", { class: "muted small" }, "Nothing scheduled this week."));
  const legend = h("div", { class: "legend tt-legend" }, ["class", "calendar", "deadline", "task", "study"].map((k) =>
    h("span", null, h("span", { class: "swatch tt-sw " + k }), KIND_LABEL[k])));
  return h("div", { class: "timetable" }, legend, grid, list);
}

VIEWS.planner = async (main, _p, params) => {
  const weekOf = params.get("week") || "";
  const suggest = params.get("suggest") === "1";
  const [week, classes, courses] = await Promise.all([
    api(A("/planner/week") + qs({ week_of: weekOf, suggest: suggest ? "true" : "", hours: 3 })),
    api(A("/planner/classes")), api(A("/courses"))]);
  const shift = (days) => { const d = new Date(week.week_start + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); };
  const go = (w, sg = suggest) => { location.hash = `#/planner${qs({ week: w, suggest: sg ? 1 : "" })}`; };
  const nav = h("div", { class: "row" },
    h("button", { class: "sm", onclick: () => go(shift(-7)) }, "‹ Previous"),
    h("button", { class: "sm", onclick: () => go("") }, "This week"),
    h("button", { class: "sm", onclick: () => go(shift(7)) }, "Next ›"),
    h("label", { class: "check small", style: "margin-left:10px" }, h("input", { type: "checkbox", checked: suggest ? true : null, onchange: (e) => go(weekOf, e.target.checked) }), "Suggest study blocks"),
    h("a", { class: "btn sm", href: A("/planner.ics") + qs({ weeks: 8 }) }, "Export .ics"),
    h("label", { class: "btn sm", title: "Weekly repeating events become class times" }, "Import timetable .ics", h("input", { type: "file", accept: ".ics,text/calendar", onchange: async (e) => {
      const f = e.target.files[0]; if (!f) return;
      const r = await guard(() => api(A("/planner/import-ics"), { method: "POST", raw: true, body: f, contentType: "text/calendar" }));
      if (r) {
        const skipped = r.skipped || [];
        const summary = `Found ${r.found} weekly class time(s), added ${r.added}`;
        toast(summary + (skipped.length ? `. Skipped ${skipped.length} event(s): ` + skipped.slice(0, 3).map((s) => `${s.title}: ${s.reason}`).join("; ") : ""), skipped.length > 0);
        route();
      } } })));
  const accessible = courses.courses.filter((c) => c.access === "accessible");
  const f = { title: h("input", { type: "text", placeholder: "e.g. MATH1023 Lecture" }),
    course: h("select", null, h("option", { value: "" }, "Course (optional)"), accessible.map((c) => h("option", { value: c.course_id }, c.title))),
    weekday: h("select", null, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d, i) => h("option", { value: i }, d))),
    start: h("input", { type: "text", placeholder: "14:00", size: 6 }), end: h("input", { type: "text", placeholder: "15:20", size: 6 }),
    location: h("input", { type: "text", placeholder: "Room (optional)" }) };
  f.course.addEventListener("change", () => { if (!f.title.value && f.course.value) f.title.value = (accessible.find((c) => c.course_id === f.course.value) || {}).course_code || ""; });
  const classCard = h("div", { class: "card", style: "margin-top:16px" },
    h("div", { class: "card-head" }, h("h2", null, "Your class times"), h("span", { class: "sub" }, "saved locally · the assistant can add these from your syllabus")),
    classes.class_times.length ? h("table", { class: "t" }, h("tbody", null, classes.class_times.map((c) => h("tr", null,
      h("td", null, h("span", { class: "strong" }, c.title)), h("td", { class: "nowrap" }, `${c.weekday_name} ${c.start_time}–${c.end_time}`),
      h("td", { class: "muted small" }, c.location || ""), h("td", { class: "nowrap" }, h("button", { class: "ghost sm", onclick: async () => { await guard(() => api(A(`/planner/classes/${c.id}`), { method: "DELETE" })); route(); } }, "Remove")))))) : h("p", { class: "muted small" }, "No class times yet. Add them here, or ask the assistant to read them from your syllabus."),
    h("div", { class: "row", style: "margin-top:10px" }, f.course, f.title, f.weekday, f.start, f.end, f.location,
      h("button", { class: "primary sm", onclick: async () => {
        const r = await guard(() => api(A("/planner/classes"), { method: "POST", body: { title: f.title.value, weekday: Number(f.weekday.value),
          start: f.start.value.trim(), end: f.end.value.trim(), location: f.location.value || undefined, course_id: f.course.value || undefined } }));
        if (r) { toast("Class time saved"); route(); } } }, "Add")));
  mount(main, pageHead("Planner", `Week of ${fmtDate(week.week_start + "T12:00:00", { time: false })} · ${week.time_zone}. Local plan only — nothing is written to Canvas.`, nav),
    h("div", { class: "card" }, renderTimetable(week, { onChange: () => {} })), classCard);
};

// ---------- external-site requests ----------
VIEWS.requests = async (main) => {
  const [reqs, courses, agents] = await Promise.all([api(A("/requests")), api(A("/courses")), api("/api/agents").catch(() => ({ agents: [] }))]);
  const fulfillers = agents.agents.filter((x) => x.can_fulfil);
  const recent = fulfillers.filter((x) => Date.now() - new Date(x.last_seen) < 24 * 3600e3);
  const agentCard = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Connected agents"),
      pill(recent.length ? "ok" : "warn", recent.length ? `${recent.length} can read sites` : "None ready")),
    agents.agents.length ? h("ul", { class: "list" }, agents.agents.map((x) => h("li", { class: "spread" },
      h("div", null, h("span", { class: "strong" }, x.name), h("span", { class: "tiny muted" }, ` · last seen ${ago(x.last_seen)}`)),
      pill(x.can_fulfil ? "ok" : "muted", x.can_fulfil ? "can fulfil requests" : "read-only")))) : null,
    !recent.length ? h("p", { class: "small" }, "Requests wait until an agent that has its own browser access checks in. Start Canvas Hub's MCP server for it with ",
      h("code", null, "canvas-hub mcp --allow-fulfil"), ", then ask it to “serve Canvas Hub view requests”. It can wait for new requests with ",
      h("code", null, "wait_for_view_requests"), ".") : null);
  const accessible = courses.courses.filter((c) => c.access === "accessible");
  const url = h("input", { type: "url", placeholder: "https://course-website.example.edu/…", style: "flex:1;min-width:240px" });
  const title = h("input", { type: "text", placeholder: "What is it? e.g. COMP1023 course website", style: "width:100%" });
  const course = h("select", null, h("option", { value: "" }, "Course (optional)"), accessible.map((c) => h("option", { value: c.course_id }, c.title)));
  const note = h("textarea", { rows: 2, placeholder: "Instructions for the agent, e.g. “Use Chrome profile ngan. Read the Schedule and Assignments pages.”" });
  const STATUS = { open: ["info", "Waiting for an agent"], claimed: ["warn", "Being read"], done: ["ok", "Done"], declined: ["bad", "Declined"], cancelled: ["muted", "Cancelled"] };
  mount(main, pageHead("Agent requests", "Ask a connected agent to read sites Canvas Hub can't open itself, such as a course website outside Canvas."),
    h("div", { class: "banner info" }, h("div", { class: "b-body small" },
      "Canvas Hub never opens these sites or touches your other browser profiles. An agent you connect over MCP (for example Codex or Claude with your browser) can pick up a request, read the site with its own access, and send back the text. It is stored as ",
      h("strong", null, "agent-provided"), " content, separate from what Canvas reported, and becomes searchable. The agent needs ",
      h("code", null, "canvas-hub mcp --allow-fulfil"), ".")),
    agentCard,
    h("div", { class: "card" }, h("h2", { style: "margin-bottom:10px" }, "New request"),
      h("div", { class: "stack" }, h("div", { class: "row" }, url, course), title, note,
        h("div", null, h("button", { class: "primary", onclick: async () => {
          const r = await guard(() => api(A("/requests"), { method: "POST", body: { url: url.value, title: title.value || undefined,
            note: note.value || undefined, course_id: course.value || undefined } }));
          if (r) { toast("Request left for connected agents"); route(); } } }, "Leave request")))),
    h("div", { class: "card" }, h("h2", { style: "margin-bottom:6px" }, "Requests"),
      reqs.requests.length ? h("ul", { class: "list" }, reqs.requests.map((r) => h("li", null,
        h("div", { class: "spread" }, h("div", { class: "row" }, pill(STATUS[r.status][0], STATUS[r.status][1]), h("span", { class: "strong" }, r.title || r.url)),
          ["open", "claimed"].includes(r.status) ? h("button", { class: "ghost sm", onclick: async () => { await guard(() => api(A(`/requests/${r.id}/cancel`), { method: "POST" })); route(); } }, "Cancel") : null),
        h("div", { class: "li-meta" }, h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, r.url),
          r.claimed_by ? h("span", null, `agent: ${r.claimed_by}`) : null, h("span", null, `asked by ${r.requested_by} ${ago(r.created_at)}`)),
        r.note ? h("div", { class: "small", style: "margin-top:4px" }, r.note) : null,
        r.result_item_id ? h("div", { class: "small", style: "margin-top:4px" }, h("a", { href: `#/item/${r.result_item_id}` }, "Read the content the agent sent →")) : null,
        r.status === "declined" && r.result_note ? h("div", { class: "small", style: "margin-top:4px;color:var(--bad)" }, r.result_note) : null))) : empty("No requests yet", "Requests you or the assistant leave appear here.")));
};

// ---------- assistant components (components.js) ----------
const C = makeComponents({ h, mount, api, A, toast, guard, fmtDate, until, ago, pill, submissionPill, TYPE_LABEL, renderTimetable,
  fieldValue, store, route: () => route() });
const componentView = (d, ctx, key) => C.componentView(d, ctx, key);
VIEWS.trackers = (main) => C.trackersView(main, pageHead, empty);

// ---------- boot ----------
async function boot() {
  try {
    S.info = await api("/api/info");
    await loadIdentities();
  } catch (e) {
    mount($("#main"), errorBox(e));
    return;
  }
  renderFoot();
  if (!S.accountId && !location.hash.startsWith("#/settings")) location.hash = "#/setup";
  await route();
  refreshSession(true).then(scheduleSessionPoll);
}
boot();
