// External links: what Canvas Hub found, whether it could read each one, and what to do next.
// Nothing here opens a browser tab or contacts a site until the user presses a button.
export function makeLinks(env) {
  const { h, mount, api, A, ago, toast } = env;
  const changed = () => window.dispatchEvent(new Event("links-updated"));
  const busy = (s) => ["queued", "running", "waiting_login"].includes(s.inspection.state);
  const TONE = { ok: "ok", login_required: "warn", network: "bad", not_found: "bad", forbidden: "bad", site_error: "bad",
    blocked: "warn", empty: "warn", unsupported: "muted", unsafe: "muted", error: "bad" };
  const tone = (i) => i.state === "waiting_login" ? "warn" : i.state !== "idle" ? "info" : i.outcome ? TONE[i.outcome] || "muted" : "accent";
  const METHOD = { basic: "Username and password", sso: "University / single sign-on", form: "The site's own sign-in page" };

  async function act(button, fn) {
    const row = button.closest(".link-row") || button.parentElement;
    row.querySelector(".link-error")?.remove();
    button.disabled = true;
    try { await fn(); changed(); }
    catch (e) { row.append(h("p", { class: "link-error", role: "alert" }, e.message)); }
    finally { if (button.isConnected) button.disabled = false; }
  }

  // The username/password form for sites that use the browser's basic sign-in prompt.
  function credentialsForm(source, path) {
    const host = source.inspection.login_host || source.host;
    const user = h("input", { type: "text", autocomplete: "off", autocapitalize: "none", spellcheck: "false", required: true });
    const pass = h("input", { type: "password", autocomplete: "off", required: true });
    const remember = h("input", { type: "checkbox" });
    const submit = h("button", { class: "primary", type: "submit" }, "Sign in and continue");
    const form = h("form", { class: "link-credentials", "aria-label": `Sign in to ${host}` },
      h("p", { class: "small" }, h("strong", null, "Step 1."), ` Type the username and password you normally use for ${host}. `,
        "They go only from this page to that site, over https; they are not sent to Canvas, to an AI provider, or put in backups."),
      h("label", null, h("span", null, `Username for ${host}`), user),
      h("label", null, h("span", null, "Password"), pass),
      h("label", { class: "check small" }, remember, "Remember on this computer (kept in Canvas Hub's private settings folder)"),
      h("div", { class: "row" }, submit),
      h("p", { class: "small muted" }, h("strong", null, "Step 2."), " Canvas Hub reads the page and saves it. Nothing else to do."));
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      act(submit, async () => {
        const body = { username: user.value, password: pass.value, remember: remember.checked };
        pass.value = "";
        await api(path + "/credentials", { method: "POST", body });
      });
    });
    return form;
  }

  function signInSteps(source) {
    const i = source.inspection;
    const host = i.login_host || source.host;
    return h("ol", { class: "link-steps small" },
      h("li", null, "A separate ", h("strong", null, "Canvas Hub sign-in window"), ` has opened on ${host}. Look for it behind this window if you don't see it.`),
      h("li", null, "Sign in there exactly as you normally do, including any phone or code step. Canvas Hub cannot see what you type."),
      h("li", null, "When the course page appears, Canvas Hub reads it and closes the window by itself. If it doesn't, press ",
        h("strong", null, "I've signed in — continue"), "."));
  }

  // Several links often sit behind one sign-in. Only the first of them asks for it;
  // the others say so, and are read automatically once that sign-in is done.
  const loginKey = (s) => s.inspection.outcome === "login_required" && s.inspection.login_host ? `${s.inspection.login_method}|${s.inspection.login_host}` : null;
  function leaders(sources) {
    const first = new Map();
    for (const s of sources) {
      const key = loginKey(s);
      if (key && (!first.has(key) || (s.inspection.state === "waiting_login" && first.get(key).inspection.state !== "waiting_login"))) first.set(key, s);
    }
    return first;
  }

  function row(source, opts = {}) {
    const i = source.inspection, path = A(`/external-sources/${source.id}`);
    const leader = opts.leaders?.get(loginKey(source));
    const follows = leader && leader.id !== source.id && i.state === "idle" ? leader : null;
    const post = (suffix) => api(path + suffix, { method: "POST", body: {} });
    const button = (label, suffix, cls) => h("button", { class: cls || null, onclick: (e) => act(e.currentTarget, () => post(suffix)) }, label);
    const main = [], side = [];
    main.push(h("div", { class: "link-title" },
      h("a", { href: source.url, target: "_blank", rel: "noopener noreferrer", title: "Open this link in your own browser" }, source.title || source.url),
      h("span", { class: `pill ${tone(i)}`, role: "status" }, i.label)));
    main.push(h("div", { class: "link-meta tiny muted" }, [source.host, opts.showCourse ? source.course_title : null,
      i.checked_at ? `checked ${ago(i.checked_at)}` : null, i.outcome === "login_required" && i.login_method ? METHOD[i.login_method] : null]
      .filter(Boolean).join(" · ")));
    if (follows) main.push(h("p", { class: "link-message small" }, `Needs the same sign-in as “${follows.title}”. Sign in there once and this link is read automatically.`));
    else if (i.message) main.push(h("p", { class: "link-message small" }, i.message));
    else if (!i.outcome && i.state === "idle") main.push(h("p", { class: "link-message small muted" },
      "Found in your course. Inspect it to let Canvas Hub open the page and save what it says."));
    if (source.earlier && !i.item_id) main.push(h("details", { class: "link-earlier" },
      h("summary", { class: "small" }, `Text saved earlier (${ago(source.earlier.read_at)})`), h("pre", { class: "text" }, source.earlier.text)));
    if (i.state === "waiting_login") {
      main.push(signInSteps(source));
      side.push(button("I've signed in — continue", "/continue", "primary"), button("Cancel", "/cancel"));
    } else if (i.state === "queued") {
      side.push(button("Cancel", "/cancel"));
    } else if (i.state === "idle") {
      const wantsLogin = !follows && i.actions[0] === "sign_in", wantsPassword = !follows && i.actions[0] === "credentials";
      if (wantsPassword) main.push(credentialsForm(source, path));
      if (wantsLogin) side.push(button(i.outcome === "blocked" ? "Open the site in a window" : "Open sign-in window", "/sign-in", "primary"));
      if (i.actions.includes("inspect")) side.push(button(!i.outcome ? "Inspect" : i.outcome === "ok" ? "Inspect again" : "Try again", "/inspect",
        !i.outcome && !wantsLogin ? "primary" : null));
      if (i.actions.includes("sign_in") && !wantsLogin && !follows) side.push(button("Sign in to this site", "/sign-in", "ghost sm"));
      if (i.item_id) side.push(h("a", { class: "btn sm", href: `#/item/${i.item_id}` }, "View saved page"));
    }
    return h("article", { class: "link-row", dataset: { source: String(source.id), state: i.state, outcome: i.outcome || "new" } },
      h("div", { class: "link-main" }, main), h("div", { class: "link-actions" }, side));
  }

  const retryable = (s) => s.inspection.state === "idle" && !["ok", "login_required", "unsafe"].includes(s.inspection.outcome);

  // Bring a list up to date without redrawing rows that did not change, so a
  // half-typed sign-in form is never wiped by a background refresh.
  function syncRows(host, rows, sources, opts) {
    const seen = new Set();
    let previous = null;
    for (const source of sources) {
      const lead = opts.leaders?.get(loginKey(source));
      const key = JSON.stringify([source.title, source.url, source.course_title, source.inspection, source.earlier?.read_at, lead && lead.id !== source.id ? [lead.id, lead.title] : null]);
      let entry = rows.get(source.id);
      if (!entry || entry.key !== key) {
        const el = row(source, opts);
        if (entry) entry.el.replaceWith(el);
        else if (previous) previous.after(el);
        else host.prepend(el);
        entry = { key, el };
        rows.set(source.id, entry);
      }
      seen.add(source.id);
      previous = entry.el;
    }
    for (const [id, entry] of rows) if (!seen.has(id)) { entry.el.remove(); rows.delete(id); }
  }

  // Reload while anything is in progress, and whenever an action elsewhere changes a link.
  function live(root, load, draw) {
    let timer = null, stopped = false, primed = null;
    async function refresh() {
      clearTimeout(timer);
      if (stopped || !root.isConnected) { stop(); return; }
      let data;
      try { data = primed || await load(); primed = null; } catch { timer = setTimeout(refresh, 4000); return; }
      if (stopped || !root.isConnected) { stop(); return; }
      draw(data);
      if (data.sources.some(busy)) timer = setTimeout(refresh, 1200);
    }
    function stop() { stopped = true; clearTimeout(timer); window.removeEventListener("links-updated", refresh); }
    window.addEventListener("links-updated", refresh);
    return { refresh, stop, prime: (data) => { primed = data; } };
  }

  function inspectAllButton(scope) {
    const b = h("button", { class: "primary link-inspect-all", hidden: true });
    b.addEventListener("click", () => act(b, async () => {
      const r = await api(A("/link-inspection/inspect"), { method: "POST", body: scope });
      toast(r.queued.length ? `Inspecting ${r.queued.length} link${r.queued.length === 1 ? "" : "s"}…` : "Nothing left to inspect here");
    }));
    b.update = (sources) => {
      const todo = sources.filter(retryable), fresh = todo.filter((x) => !x.inspection.outcome).length;
      b.hidden = !todo.length;
      b.classList.toggle("primary", fresh > 0);
      b.textContent = fresh ? `Inspect all (${todo.length})` : `Try failed links again (${todo.length})`;
    };
    return b;
  }

  // The list on a course page or an item page.
  const attached = new WeakMap();
  async function attach(main, scope) {
    attached.get(main)?.stop();
    const query = new URLSearchParams(scope).toString();
    const path = A("/external-sources") + "?" + query;
    const first = await api(path);
    if (!main.isConnected || !first.sources.length) return;
    const all = inspectAllButton(scope.item_id ? { item_id: Number(scope.item_id) } : { course_id: scope.course_id });
    const list = h("div", { class: "link-list" });
    const section = h("section", { class: "card link-card", "aria-label": "External links" },
      h("div", { class: "card-head" }, h("h2", null, "External links"), h("div", { class: "row" }, all, h("a", { class: "small", href: "#/links" }, "All external links →"))),
      h("p", { class: "small muted" }, "Sites outside Canvas that this ", scope.item_id ? "item" : "course", " links to. Canvas Hub opens one only when you ask."),
      list);
    (main.querySelector(".workspace-content") || main).append(section);
    const rows = new Map();
    const updates = live(section, () => api(path), (data) => { syncRows(list, rows, data.sources, { leaders: leaders(data.sources) }); all.update(data.sources); });
    attached.set(main, updates);
    updates.prime(first);
    await updates.refresh();
  }

  // The page that lists every external link.
  async function page(main, pageHead) {
    const [data, courses] = await Promise.all([api(A("/link-inspection")), api(A("/courses")).catch(() => ({ courses: [] }))]);
    const all = inspectAllButton({});
    const counts = h("div", { class: "row link-counts" });
    const groups = h("div", { class: "stack" });
    const url = h("input", { type: "url", placeholder: "https://course-site.example.edu/…", required: true, "aria-label": "Link address" });
    const title = h("input", { type: "text", placeholder: "Name (optional), e.g. COMP1023 course website", "aria-label": "Link name" });
    const course = h("select", { "aria-label": "Course" }, h("option", { value: "" }, "No course"),
      courses.courses.filter((c) => c.access === "accessible").map((c) => h("option", { value: c.course_id }, c.title)));
    const add = h("button", { type: "submit" }, "Add link");
    const addForm = h("form", { class: "link-add" }, url, title, course, add);
    addForm.addEventListener("submit", (e) => {
      e.preventDefault();
      act(add, async () => {
        await api(A("/external-sources"), { method: "POST", body: { url: url.value, title: title.value, course_id: course.value || null } });
        url.value = title.value = "";
        toast("Link added. Press Inspect to read it.");
      });
    });
    const forget = h("button", { onclick: (e) => act(e.currentTarget, async () => {
      await api(A("/link-inspection/forget-sign-ins"), { method: "POST", body: {} });
      toast("Saved site sign-ins were removed. Saved pages are kept.");
    }) }, "Forget site sign-ins");
    mount(main, pageHead("External links", "Course websites, submission systems and forums that your Canvas courses link to.", all),
      h("div", { class: "banner info" }, h("div", { class: "b-body small" },
        h("strong", null, "How it works. "), "Press ", h("strong", null, "Inspect"), " and Canvas Hub opens the link in its own background browser and saves the text so you can search it and the Assistant can use it. ",
        "If a site needs you to sign in, the row says which kind of sign-in it is and gives you one button to do it. After you sign in, Canvas Hub carries on by itself.")),
      counts, groups,
      h("div", { class: "card link-card" }, h("h2", null, "Add a link yourself"),
        h("p", { class: "small muted" }, "For a site Canvas does not link to. It is treated like any other external link."), addForm),
      h("div", { class: "card link-card" }, h("h2", null, "Site sign-ins"),
        h("p", { class: "small" }, "Sign-ins for external sites live in a browser profile that belongs to Canvas Hub only. It is separate from your Canvas sign-in and from your own browsers, and it is never part of a backup or export."),
        h("div", { class: "row" }, forget)));
    const lists = new Map();
    function draw(d) {
      const s = d.summary;
      mount(counts, [["accent", s.not_inspected, "not inspected yet"], ["warn", s.needs_login, "need sign-in"], ["bad", s.failed, "could not be read"],
        ["ok", s.read, "read and saved"]].filter((x) => x[1]).map(([cls, n, label]) => h("span", { class: `pill ${cls}` }, `${n} ${label}`)));
      if (!d.sources.length) {
        if (!lists.size) mount(groups, h("div", { class: "empty" }, h("div", { class: "e-title" }, "No external links yet"),
          h("div", null, "Links appear here after a collection finds them in your courses. You can also add one below.")));
        return;
      }
      // One list per course, so rows stay under their course heading.
      for (const source of d.sources) {
        const key = source.course_id || "";
        if (lists.has(key)) continue;
        if (!lists.size) mount(groups);
        const host = h("div", { class: "link-list" });
        groups.append(h("section", { class: "card link-card", "aria-label": source.course_title || "Other links" },
          h("div", { class: "card-head" }, h("h2", null, source.course_title || (key ? `Course ${key}` : "Other links")),
            key ? h("a", { class: "small", href: `#/course/${key}` }, "Open course →") : null), host));
        lists.set(key, { host, rows: new Map() });
      }
      const lead = leaders(d.sources);
      for (const [key, entry] of lists) syncRows(entry.host, entry.rows, d.sources.filter((x) => (x.course_id || "") === key), { leaders: lead });
      all.update(d.sources);
    }
    const updates = live(groups, () => api(A("/link-inspection")), draw);
    updates.prime(data);
    await updates.refresh();
  }

  // The notice shown on every page when a collection has found links nobody has looked at yet.
  let noticeEl = null, noticeTimer = null, lastNoticeKey = "";
  async function notice() {
    clearTimeout(noticeTimer);
    let s;
    try { s = await api(A("/link-inspection/summary")); } catch { noticeTimer = setTimeout(notice, 60000); return; }
    env.onSummary?.(s);
    if (!noticeEl) {
      // Placed in the page column: a floating card on wide screens, an ordinary block above the page on narrow ones,
      // so it never sits on top of what the user is trying to press.
      noticeEl = h("aside", { class: "link-notice", "aria-label": "External links", hidden: true });
      (document.getElementById("topbar") || document.body.lastElementChild).after(noticeEl);
    }
    const n = s.notice.count, w = s.waiting_login;
    const key = JSON.stringify([n, s.notice.source_ids, w]);
    if (key !== lastNoticeKey) {
      lastNoticeKey = key;
      if (w) {
        mount(noticeEl, h("p", { role: "status" }, h("strong", null, "Sign-in window open. "), `Finish signing in to ${w.host} for “${w.title}”. Canvas Hub continues by itself.`),
          h("div", { class: "row" }, h("a", { class: "btn primary", href: "#/links" }, "Show the steps")));
      } else if (n) {
        const first = s.notice.examples[0];
        const dismiss = h("button", { onclick: (e) => act(e.currentTarget, () => api(A("/link-inspection/dismiss"), { method: "POST", body: { source_ids: s.notice.source_ids } })) }, "Not now");
        const go = h("button", { class: "primary", onclick: (e) => act(e.currentTarget, async () => {
          // The server takes at most 200 ids per request; the notice can list more.
          const ids = s.notice.source_ids;
          for (let i = 0; i < ids.length; i += 200) {
            await api(A("/link-inspection/inspect"), { method: "POST", body: { source_ids: ids.slice(i, i + 200) } });
          }
          if (!location.hash.startsWith("#/links")) location.hash = "#/links";
        }) }, n === 1 ? "Inspect it" : `Inspect all ${n}`);
        mount(noticeEl, h("p", { role: "status" }, h("strong", null, n === 1 ? "New external link found. " : `${n} new external links found. `),
            first ? `For example “${first.title}”${first.course_title ? ` in ${first.course_title}` : ""}. ` : "",
            "May Canvas Hub open ", n === 1 ? "it" : "them", " and save what ", n === 1 ? "it says" : "they say", "?"),
          h("div", { class: "row" }, go, h("a", { class: "btn", href: "#/links" }, "Review first"), dismiss));
      }
      noticeEl.hidden = !(n || w);
      document.body.classList.toggle("has-link-notice", !noticeEl.hidden);
    }
    noticeTimer = setTimeout(notice, s.active || w ? 2500 : 60000);
  }
  window.addEventListener("links-updated", () => notice());

  return { attach, page, notice };
}
