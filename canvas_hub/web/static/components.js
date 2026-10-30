// Canvas Hub — interactive components for assistant answers and the Trackers page.
//
// Every component is wrapped in a frame with a toolbar: copy as Markdown / CSV /
// JSON, download as a file, and full screen. Score models and checklists keep
// their state on the server (with undo) and recompute live in the page using the
// same rules as canvas_hub/scoring.py. Clicking an item opens a side panel with
// its full content.

export function makeComponents(env) {
  const { h, mount, api, A, toast, guard, fmtDate, until, ago, pill, submissionPill, TYPE_LABEL, renderTimetable,
    fieldValue, store } = env;

  // ---------- small helpers ----------
  const fmtN = (n) => (n == null || Number.isNaN(n) ? "–" : Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100));
  const esc = (s) => String(s ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ");
  const csvCell = (s) => { const t = String(s ?? ""); return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t; };
  const toCSV = (cols, rows) => [cols.map(csvCell).join(","), ...rows.map((r) => r.map(csvCell).join(","))].join("\n");
  const toMD = (cols, rows) => [`| ${cols.map(esc).join(" | ")} |`, `| ${cols.map(() => "---").join(" | ")} |`,
    ...rows.map((r) => `| ${r.map(esc).join(" | ")} |`)].join("\n");
  const slug = (s) => (String(s || "canvas-hub").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "canvas-hub").slice(0, 60);
  const num = (v) => { if (typeof v === "number") return v; const n = Number(String(v ?? "").replace(/[,%\s]/g, "")); return Number.isFinite(n) ? n : null; };

  // ---------- clipboard with fallback ----------
  async function copyText(text, btn) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch { ok = false; }
    if (ok) {
      if (btn) { const old = btn.textContent; btn.textContent = "Copied ✓"; btn.classList.add("copied"); setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1500); }
      else toast("Copied");
      return;
    }
    // Clipboard permission denied: show the text selected so ⌘C / Ctrl+C works.
    const ta = h("textarea", { class: "mono copy-fallback", readonly: true, "aria-label": "Text to copy" });
    ta.value = text;
    const close = h("button", { class: "sm" }, "Close");
    const dlg = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "Copy text" },
      h("div", { class: "modal-box" }, h("div", { class: "strong" }, "Press ⌘C (or Ctrl+C) to copy"),
        h("div", { class: "tiny muted" }, "The browser did not allow automatic copying; the text is selected for you."), ta,
        h("div", { class: "row", style: "justify-content:flex-end" }, close)));
    close.addEventListener("click", () => dlg.remove());
    dlg.addEventListener("keydown", (e) => { if (e.key === "Escape") dlg.remove(); });
    document.body.append(dlg);
    ta.focus(); ta.select();
  }
  function download(text, filename, type = "text/plain") {
    const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
    const a = h("a", { href: url, download: filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  // One listener closes any open copy menu when clicking elsewhere.
  document.addEventListener("click", (e) => {
    for (const m of document.querySelectorAll(".cmp-menu:not([hidden])")) if (!m.parentElement.contains(e.target)) m.hidden = true;
  });

  // ---------- frame: title + universal toolbar ----------
  // getData() -> { title, markdown, csv?, json, filename? , text? }
  function frame(kind, title, body, getData, { extra = [], sticky = false, full: startFull = false, onFull = null } = {}) {
    const wrap = h("section", { class: `cmp cmp-${kind}${startFull ? " fullscreen" : ""}`, "aria-label": title || kind });
    const menu = h("div", { class: "cmp-menu", hidden: true, role: "menu" });
    const copyBtn = h("button", { class: "sm ghost", "aria-haspopup": "menu", title: "Copy or download" }, "Copy ▾");
    const full = h("button", { class: "sm ghost", title: "Full screen", "aria-label": "Full screen" }, "⤢");
    const fill = () => {
      const d = getData();
      const opts = [["Copy as Markdown", () => copyText(d.markdown, copyBtn)]];
      if (d.csv != null) opts.push(["Copy as CSV", () => copyText(d.csv, copyBtn)]);
      opts.push(["Copy as JSON", () => copyText(JSON.stringify(d.json, null, 2), copyBtn)]);
      if (d.text != null) opts.unshift(["Copy text", () => copyText(d.text, copyBtn)]);
      const base = d.filename || slug(d.title || title);
      opts.push(["Download .md", () => download(d.markdown, `${base}.md`, "text/markdown")]);
      if (d.csv != null) opts.push(["Download .csv", () => download(d.csv, `${base}.csv`, "text/csv")]);
      opts.push(["Download .json", () => download(JSON.stringify(d.json, null, 2), `${base}.json`, "application/json")]);
      mount(menu, opts.map(([l, fn]) => h("button", { class: "cmp-menu-item", role: "menuitem", onclick: () => { menu.hidden = true; fn(); } }, l)));
    };
    copyBtn.addEventListener("click", (e) => { e.stopPropagation(); if (menu.hidden) fill(); menu.hidden = !menu.hidden; });
    const setFull = (on) => {
      wrap.classList.toggle("fullscreen", on);
      full.textContent = on ? "✕" : "⤢";
      full.setAttribute("aria-label", on ? "Exit full screen" : "Full screen");
    };
    setFull(startFull);
    full.addEventListener("click", () => { const on = !wrap.classList.contains("fullscreen"); setFull(on); if (onFull) onFull(on); });
    wrap.addEventListener("keydown", (e) => { if (e.key === "Escape" && wrap.classList.contains("fullscreen")) full.click(); });
    const bar = h("div", { class: "cmp-bar" + (sticky ? " sticky" : "") },
      h("div", { class: "cmp-head" }, title || ""), h("div", { class: "cmp-tools" }, ...extra, h("div", { class: "cmp-copy" }, copyBtn, menu), full));
    wrap.append(bar, body);
    return wrap;
  }

  // ---------- live countdowns ----------
  let tickQueued = false;
  function countdown(iso) {
    if (!tickQueued) { tickQueued = true; requestAnimationFrame(() => { tickQueued = false; tickCountdowns(); }); }
    return h("span", { class: "countdown", "data-due": iso, role: "timer", "aria-live": "off" }, cdText(iso));
  }
  function cdText(iso) {
    const ms = new Date(iso) - Date.now();
    if (Number.isNaN(ms)) return "";
    const a = Math.abs(ms), m = Math.round(a / 60000), hh = Math.floor(m / 60), d = Math.floor(hh / 24);
    const s = d >= 2 ? `${d} d ${hh % 24} h` : hh >= 1 ? `${hh} h ${m % 60} m` : `${m} m`;
    return ms < 0 ? `overdue ${s}` : `in ${s}`;
  }
  function tickCountdowns() {
    for (const el of document.querySelectorAll(".countdown[data-due]")) {
      const ms = new Date(el.dataset.due) - Date.now();
      el.textContent = cdText(el.dataset.due);
      el.classList.toggle("overdue", ms < 0);
      el.classList.toggle("urgent", ms >= 0 && ms < 24 * 3600e3);
      el.classList.toggle("soon", ms >= 24 * 3600e3 && ms < 72 * 3600e3);
    }
  }
  setInterval(tickCountdowns, 30000);
  const afterRender = () => requestAnimationFrame(tickCountdowns);

  // ---------- detail drawer ----------
  let drawer = null;
  async function openDrawer(itemId) {
    if (!itemId) return;
    if (!drawer) {
      drawer = h("aside", { class: "drawer", role: "dialog", "aria-modal": "false", "aria-label": "Item details", tabindex: "-1" });
      document.body.append(drawer);
      document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.classList.contains("open")) closeDrawer(); });
    }
    mount(drawer, h("div", { class: "muted small", style: "padding:20px" }, "Loading…"));
    drawer.classList.add("open");
    let it;
    try { it = await api(A(`/items/${itemId}`)); } catch (e) { mount(drawer, h("div", { style: "padding:20px" }, String(e.message || e))); return; }
    const fields = it.fields.filter((f) => f.field !== "title");
    const body = fields.filter((f) => f.kind === "html" && f.value);
    const other = fields.filter((f) => !(f.kind === "html" && f.value));
    const md = [`# ${it.title || "(untitled)"}`, "", ...other.map((f) => `- **${f.field}**: ${typeof f.value === "object" ? JSON.stringify(f.value) : f.value}`)].join("\n");
    mount(drawer,
      h("div", { class: "drawer-head" }, h("div", null, h("div", { class: "tiny muted" }, TYPE_LABEL[it.item_type] || it.item_type, it.course_title ? ` · ${it.course_title}` : ""),
        h("h2", null, it.title || "(untitled)")),
        h("button", { class: "ghost sm", "aria-label": "Close", onclick: closeDrawer }, "✕")),
      h("div", { class: "row small", style: "padding:0 20px 10px" },
        h("a", { href: `#/item/${it.id}`, onclick: closeDrawer }, "Open full page"),
        it.source_url ? h("a", { href: it.source_url, target: "_blank", rel: "noopener noreferrer" }, "Open in Canvas") : null,
        h("button", { class: "sm ghost", onclick: (e) => copyText(md, e.target) }, "Copy fields"),
        h("span", { class: "muted" }, `first seen ${fmtDate(it.first_observed_at)} · last read ${ago(it.last_observed_at)}`)),
      h("div", { class: "drawer-body" },
        body.map((f) => h("div", { class: "drawer-sec" }, h("div", { class: "drawer-k" }, f.field, h("span", { class: "tiny muted" }, ` · read ${ago(f.last_observed_at)}`)), fieldValue(f.field, f, it))),
        other.length ? h("dl", { class: "drawer-fields" }, other.map((f) => [h("dt", null, f.field), h("dd", null, fieldValue(f.field, f, it),
          h("div", { class: "tiny muted" }, `read ${ago(f.last_observed_at)}`))])) : null));
    drawer.focus();
  }
  function closeDrawer() { if (drawer) drawer.classList.remove("open"); }
  const itemLink = (id, label) => id ? h("a", { href: `#/item/${id}`, class: "cmp-link", onclick: (e) => { if (e.metaKey || e.ctrlKey) return; e.preventDefault(); openDrawer(id); } }, label) : label;

  // ---------- deadlines / items / timetable / choices (export added) ----------
  function deadlinesView(d) {
    const rows = d.deadlines.map((x) => [x.title, x.course || "", x.due_local, (x.submission || {}).workflow_state || ""]);
    const body = h("div", { class: "cmp-deadlines" }, d.deadlines.length ? d.deadlines.map((x) => h("div", { class: "cmp-card" },
      h("div", { class: "spread" }, h("div", null, h("div", { class: "cmp-when" }, fmtDate(x.due_local, { weekday: true }), " · ", countdown(x.due_local)),
        itemLink(x.item_id, h("span", { class: "cmp-title" }, x.title)), h("div", { class: "tiny muted" }, x.course || "")),
        submissionPill(x.submission ? { ...x.submission, state: x.submission.state } : null)),
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { class: "sm", onclick: async (ev) => { const r = await guard(() => api(A("/annotations"), { method: "POST", body: { kind: "task", item_id: x.item_id, title: x.title } })); if (r) { ev.target.textContent = "✓ Added"; ev.target.disabled = true; } } }, "+ To-do"),
        x.source_url ? h("a", { class: "btn sm", href: x.source_url, target: "_blank", rel: "noopener noreferrer" }, "Open in Canvas") : null))) : h("div", { class: "muted small" }, "No deadlines in that range."));
    afterRender();
    return frame("deadlines", "Deadlines", body, () => ({ title: "Deadlines", markdown: toMD(["Title", "Course", "Due", "Status"], rows),
      csv: toCSV(["Title", "Course", "Due", "Status"], rows), json: d.deadlines }));
  }
  function itemsView(d) {
    const rows = d.items.map((x) => [x.title || "", TYPE_LABEL[x.type] || x.type, x.course || "", x.due || "", x.source_url || ""]);
    const body = h("div", { class: "cmp-deadlines" }, d.items.map((x) => h("div", { class: "cmp-card" },
      h("div", { class: "tiny muted" }, TYPE_LABEL[x.type] || x.type, x.course ? " · " + x.course : "", x.due ? " · due " + x.due : ""),
      itemLink(x.item_id, h("span", { class: "cmp-title" }, x.title || "(untitled)")),
      x.excerpt ? h("div", { class: "small", style: "margin-top:4px" }, x.excerpt) : null,
      x.source_url ? h("div", { style: "margin-top:6px" }, h("a", { class: "small", href: x.source_url, target: "_blank", rel: "noopener noreferrer" }, "Open in Canvas")) : null)));
    return frame("items", "Items", body, () => ({ title: "Items", markdown: toMD(["Title", "Type", "Course", "Due", "Link"], rows),
      csv: toCSV(["Title", "Type", "Course", "Due", "Link"], rows), json: d.items }));
  }
  function timetableView(d) {
    const evs = d.week.days.flatMap((day) => day.events.map((e) => [day.date, e.kind, e.title, e.start.slice(11, 16), e.end.slice(11, 16), e.location || ""]));
    const cols = ["Date", "Kind", "Title", "Start", "End", "Location"];
    return frame("timetable", `Week of ${d.week.week_start}`, renderTimetable(d.week), () => ({ title: `Week of ${d.week.week_start}`,
      markdown: toMD(cols, evs), csv: toCSV(cols, evs), json: d.week }));
  }

  // ---------- table v2 ----------
  function tableView(d) {
    const cols = d.columns, types = d.column_types || [];
    const isNum = (i) => types[i] === "number" || (types[i] == null && d.rows.length && d.rows.every((r) => r[i] === "" || r[i] == null || num(r[i]) != null));
    const st = { sort: null, dir: 1, filter: "", open: new Set() };
    const filterIn = h("input", { type: "search", class: "cmp-filter", placeholder: "Filter rows", "aria-label": "Filter rows" });
    const host = h("div", { class: "table-wrap" });
    const visible = () => {
      let idx = d.rows.map((_, i) => i);
      if (st.filter) { const q = st.filter.toLowerCase(); idx = idx.filter((i) => d.rows[i].some((c) => String(c ?? "").toLowerCase().includes(q))); }
      if (st.sort != null) {
        const k = st.sort, n = isNum(k);
        idx.sort((a, b) => { const x = d.rows[a][k], y = d.rows[b][k];
          const c = n ? (num(x) ?? -Infinity) - (num(y) ?? -Infinity) : String(x ?? "").localeCompare(String(y ?? ""), undefined, { numeric: true });
          return c * st.dir; });
      }
      return idx;
    };
    const gi = d.group_by ? cols.indexOf(d.group_by) : -1;
    const totalsRow = (idx) => d.totals && d.totals.length ? h("tr", { class: "t-total" }, cols.map((c, i) => h("td", { class: isNum(i) ? "num" : "" },
      i === 0 && !d.totals.includes(c) ? "Total" : d.totals.includes(c) ? fmtN(idx.reduce((s, r) => s + (num(d.rows[r][i]) || 0), 0)) : ""))) : null;
    const rowEls = (i) => {
      const r = d.rows[i];
      const hasDetail = d.details && d.details[i];
      const iid = d.item_ids && d.item_ids[i];
      const toggle = hasDetail ? h("button", { class: "link-btn", "aria-expanded": st.open.has(i) ? "true" : "false", "aria-label": `Details for ${r[0]}`,
        onclick: () => { st.open.has(i) ? st.open.delete(i) : st.open.add(i); draw(); } }, st.open.has(i) ? "▾ " : "▸ ") : null;
      const tr = h("tr", { class: hasDetail ? "expandable" : "" }, cols.map((_, k) => h("td", { class: isNum(k) ? "num" : "" },
        k === 0 ? [toggle, iid ? itemLink(iid, String(r[k] ?? "")) : String(r[k] ?? "")] : String(r[k] ?? ""))));
      return [tr, hasDetail && st.open.has(i) ? h("tr", { class: "t-detail" }, h("td", { colspan: cols.length }, h("div", { class: "pre-wrap small" }, d.details[i]))) : null];
    };
    const draw = () => {
      const idx = visible();
      const head = h("thead", null, h("tr", null, cols.map((c, i) => h("th", { class: isNum(i) ? "num" : "", "aria-sort": st.sort === i ? (st.dir > 0 ? "ascending" : "descending") : "none" },
        h("button", { class: "th-btn", onclick: () => { if (st.sort === i) st.dir = -st.dir; else { st.sort = i; st.dir = 1; } draw(); } },
          c, st.sort === i ? (st.dir > 0 ? " ▲" : " ▼") : "")))));
      let body;
      if (gi >= 0) {
        const groups = new Map();
        for (const i of idx) { const g = String(d.rows[i][gi] ?? ""); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(i); }
        body = [...groups.entries()].map(([g, ix]) => h("tbody", null, h("tr", { class: "t-group" }, h("td", { colspan: cols.length }, `${g || "(none)"} · ${ix.length}`)),
          ix.map(rowEls), totalsRow(ix)));
      } else body = h("tbody", null, idx.map(rowEls));
      mount(host, h("table", { class: "t" }, head, body, gi < 0 && totalsRow(idx) ? h("tfoot", null, totalsRow(idx)) : null),
        idx.length < d.rows.length ? h("div", { class: "tiny muted", style: "padding:4px 2px" }, `${idx.length} of ${d.rows.length} rows`) : null);
    };
    filterIn.addEventListener("input", () => { st.filter = filterIn.value; draw(); });
    draw();
    const data = () => { const idx = visible(); const rows = idx.map((i) => d.rows[i]);
      return { title: d.title || "Table", markdown: (d.title ? `**${d.title}**\n\n` : "") + toMD(cols, rows), csv: toCSV(cols, rows),
        json: rows.map((r) => Object.fromEntries(cols.map((c, i) => [c, r[i]]))) }; };
    return frame("table", d.title, h("div", null, d.rows.length > 5 ? filterIn : null, host), data);
  }

  // ---------- charts ----------
  const SERIES = 6;
  function chartView(d) {
    const kind = d.kind || "bar";
    const series = d.series && d.series.length ? d.series : [{ name: "Value", values: d.values || [] }];
    const labels = d.labels || [];
    const unit = d.unit || "";
    const legend = series.length > 1 ? h("div", { class: "legend" }, series.map((s, i) => h("span", null, h("span", { class: `swatch s${i % SERIES}` }), s.name))) : null;
    let body;
    if (!labels.length) body = h("div", { class: "muted small" }, "No values to show.");
    else if (kind === "stacked") {
      const totals = labels.map((_, i) => series.reduce((s, x) => s + Math.max(0, x.values[i] || 0), 0));
      const max = d.max || Math.max(1, d.threshold || 0, ...totals);
      body = h("div", { class: "bars" }, labels.map((l, i) => h("div", { class: "bar-row" }, h("div", { class: "bar-label" }, l),
        h("div", { class: "bar-track stacked", role: "img", "aria-label": `${l}: ${series.map((s) => `${s.name} ${fmtN(s.values[i])}${unit}`).join(", ")}` },
          series.map((s, k) => h("span", { class: `seg s${k % SERIES}`, title: `${s.name}: ${fmtN(s.values[i])}${unit}`, style: `width:${Math.max(0, (s.values[i] || 0) / max * 100)}%` })),
          d.threshold != null ? h("i", { class: "thr", style: `left:${Math.min(100, d.threshold / max * 100)}%`, title: `threshold ${fmtN(d.threshold)}` }) : null),
        h("div", { class: "bar-val num" }, `${fmtN(totals[i])}${unit}`))));
    } else if (kind === "waterfall") {
      const vals = series[0].values; let run = 0;
      const steps = vals.map((v) => { const from = run; run += v; return [from, run]; });
      const max = d.max || Math.max(1, d.threshold || 0, ...steps.map((s) => Math.max(s[0], s[1])));
      body = h("div", { class: "bars" }, labels.map((l, i) => { const [a, b] = steps[i]; const lo = Math.min(a, b), hi = Math.max(a, b);
        return h("div", { class: "bar-row" }, h("div", { class: "bar-label" }, l),
          h("div", { class: "bar-track", role: "img", "aria-label": `${l}: ${vals[i] >= 0 ? "+" : ""}${fmtN(vals[i])}${unit}, running total ${fmtN(b)}` },
            h("span", { class: `wf ${vals[i] < 0 ? "neg" : "pos"}`, style: `left:${lo / max * 100}%;width:${Math.max(0.5, (hi - lo) / max * 100)}%` }),
            d.threshold != null ? h("i", { class: "thr", style: `left:${Math.min(100, d.threshold / max * 100)}%` }) : null),
          h("div", { class: "bar-val num" }, `${vals[i] >= 0 ? "+" : ""}${fmtN(vals[i])} → ${fmtN(b)}${unit}`)); }));
    } else {
      const max = d.max || Math.max(1, d.threshold || 0, ...series.flatMap((s) => s.values));
      body = h("div", { class: "bars" }, labels.map((l, i) => h("div", { class: "bar-row" }, h("div", { class: "bar-label" }, l),
        h("div", { class: "bar-group" }, series.map((s, k) => h("div", { class: "bar-track", role: "img", "aria-label": `${l}${series.length > 1 ? ` ${s.name}` : ""}: ${fmtN(s.values[i])}${unit}` },
          h("span", { class: series.length > 1 ? `s${k % SERIES}` : "", style: `width:${Math.max(0, Math.min(100, (s.values[i] || 0) / max * 100))}%` }),
          d.threshold != null ? h("i", { class: "thr", style: `left:${Math.min(100, d.threshold / max * 100)}%` }) : null))),
        h("div", { class: "bar-val num" }, series.map((s) => `${fmtN(s.values[i])}${unit}`).join(" / ")))));
    }
    const cols = ["Label", ...series.map((s) => s.name)];
    const rows = labels.map((l, i) => [l, ...series.map((s) => s.values[i])]);
    return frame("chart", d.title, h("div", null, legend, body,
      d.threshold != null ? h("div", { class: "tiny muted", style: "margin-top:6px" }, `Threshold line at ${fmtN(d.threshold)}${unit}`) : null,
      (d.notes || []).length ? h("div", { class: "tiny muted", style: "margin-top:6px" }, d.notes.join(" · ")) : null),
    () => ({ title: d.title || "Chart", markdown: (d.title ? `**${d.title}**\n\n` : "") + toMD(cols, rows), csv: toCSV(cols, rows), json: d }));
  }

  // ---------- gauge ----------
  function gaugeEl(segments, max, threshold, unit, style = "bar") {
    const total = segments.reduce((s, x) => s + x.value, 0);
    const label = `${fmtN(total)} of ${fmtN(max)}${unit}${threshold != null ? `, threshold ${fmtN(threshold)}` : ""}`;
    let vis;
    if (style === "donut") {
      let acc = 0;
      const stops = segments.map((s, i) => { const a = acc / max * 360; acc += s.value; const b = Math.min(360, acc / max * 360); return `var(--s${i % SERIES}) ${a}deg ${b}deg`; });
      stops.push(`var(--surface-2) ${Math.min(360, acc / max * 360)}deg 360deg`);
      vis = h("div", { class: "donut", role: "img", "aria-label": label, style: `background:conic-gradient(${stops.join(",")})` },
        h("div", { class: "donut-hole" }, h("div", { class: "donut-n" }, fmtN(total)), h("div", { class: "tiny muted" }, `of ${fmtN(max)}${unit}`)),
        threshold != null ? h("i", { class: "donut-thr", style: `transform:rotate(${threshold / max * 360}deg)` }) : null);
    } else {
      vis = h("div", { class: "gauge", role: "img", "aria-label": label },
        segments.map((s, i) => h("span", { class: `seg s${i % SERIES}`, title: `${s.label}: ${fmtN(s.value)}${unit}`, style: `width:${Math.max(0, s.value / max * 100)}%` })),
        threshold != null ? h("i", { class: "thr", style: `left:${Math.min(100, threshold / max * 100)}%`, title: `threshold ${fmtN(threshold)}` }) : null);
    }
    const delta = threshold != null ? total - threshold : null;
    return h("div", { class: "gauge-wrap" }, vis,
      h("div", { class: "legend" }, segments.map((s, i) => h("span", null, h("span", { class: `swatch s${i % SERIES}` }), `${s.label} ${fmtN(s.value)}${unit}`))),
      delta != null ? h("div", { class: "small " + (delta >= 0 ? "ok-text" : "bad-text") }, delta >= 0 ? `Threshold reached (+${fmtN(delta)}${unit})` : `${fmtN(-delta)}${unit} to the threshold`) : null);
  }
  function gaugeView(d) {
    const cols = ["Segment", "Value"], rows = d.segments.map((s) => [s.label, s.value]);
    return frame("gauge", d.title, gaugeEl(d.segments, d.max, d.threshold, d.unit || "", d.style),
      () => ({ title: d.title || "Progress", markdown: toMD(cols, rows), csv: toCSV(cols, rows), json: d }));
  }

  // ---------- artifact block ----------
  function highlight(line, lang) {
    if (lang === "json") {
      const out = []; const re = /("(?:[^"\\]|\\.)*")(\s*:)?|(-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|\b(true|false|null)\b/g;
      let last = 0, m;
      while ((m = re.exec(line))) {
        out.push(line.slice(last, m.index));
        if (m[1]) out.push(h("span", { class: m[2] ? "hl-k" : "hl-s" }, m[1]), m[2] || "");
        else if (m[3]) out.push(h("span", { class: "hl-n" }, m[3]));
        else out.push(h("span", { class: "hl-b" }, m[4]));
        last = re.lastIndex;
      }
      out.push(line.slice(last));
      return out;
    }
    if (lang === "markdown" || lang === "md") {
      if (/^#{1,6}\s/.test(line)) return [h("span", { class: "hl-k" }, line)];
      if (/^\s*([-*]|\d+\.)\s/.test(line)) { const m = line.match(/^(\s*(?:[-*]|\d+\.)\s)(.*)$/); return [h("span", { class: "hl-n" }, m[1]), m[2]]; }
    }
    return [line];
  }
  function artifactView(d) {
    const lines = d.content.split("\n");
    const words = (d.content.match(/\S+/g) || []).length;
    const LIMIT = 24;
    let expanded = lines.length <= LIMIT;
    const pre = h("div", { class: "art-body", role: "region", "aria-label": `${d.title} content`, tabindex: "0" });
    const more = h("button", { class: "sm" });
    const copyBtn = h("button", { class: "sm primary", onclick: () => copyText(d.content, copyBtn) }, "Copy");
    const fname = d.filename || `${slug(d.title)}.${{ markdown: "md", md: "md", json: "json", csv: "csv", python: "py", html: "html", javascript: "js", text: "txt" }[d.language] || "txt"}`;
    const dl = h("button", { class: "sm", onclick: () => download(d.content, fname, d.language === "json" ? "application/json" : d.language === "csv" ? "text/csv" : "text/plain") }, "Download");
    const save = h("button", { class: "sm ghost", title: "Keep it in Canvas Hub (Notes & tasks → Files)", onclick: async () => {
      const r = await guard(() => api(A("/files"), { method: "POST", body: { filename: fname, title: d.title, content: d.content } }));
      if (r) { save.textContent = "✓ Saved to files"; save.disabled = true; toast(`Saved as file #${r.id}`); } } }, "Save to files");
    const draw = () => {
      const show = expanded ? lines : lines.slice(0, LIMIT);
      mount(pre, h("ol", { class: "art-lines" }, show.map((l) => h("li", null, h("code", null, ...highlight(l, d.language))))));
      pre.classList.toggle("clipped", !expanded);
      more.hidden = lines.length <= LIMIT;
      more.textContent = expanded ? "Collapse" : `Expand full (${lines.length} lines)`;
    };
    more.addEventListener("click", () => { expanded = !expanded; draw(); });
    draw();
    const body = h("div", null,
      h("div", { class: "art-meta tiny muted" }, `${d.language || "text"} · ${lines.length} lines · ${words.toLocaleString()} words · ${d.content.length.toLocaleString()} characters`),
      pre, h("div", { class: "row", style: "margin-top:8px" }, copyBtn, dl, save, more));
    return frame("artifact", d.title, body, () => ({ title: d.title, filename: fname.replace(/\.[^.]+$/, ""), text: d.content,
      markdown: d.language === "markdown" || d.language === "md" ? d.content : "```" + (d.language || "") + "\n" + d.content + "\n```",
      json: { title: d.title, language: d.language, content: d.content } }));
  }

  // ---------- JSON tree ----------
  function treeNode(key, v, depth) {
    const k = key != null ? h("span", { class: "hl-k" }, `${key}: `) : null;
    if (v === null || typeof v !== "object") {
      const cls = typeof v === "string" ? "hl-s" : typeof v === "number" ? "hl-n" : "hl-b";
      return h("div", { class: "tree-leaf" }, k, h("span", { class: cls }, typeof v === "string" ? JSON.stringify(v) : String(v)));
    }
    const arr = Array.isArray(v), entries = arr ? v.map((x, i) => [i, x]) : Object.entries(v);
    const det = h("details", { class: "tree-node", open: depth < 2 ? true : null },
      h("summary", null, k, h("span", { class: "muted" }, arr ? `[${entries.length}]` : `{${entries.length}}`)));
    det.addEventListener("toggle", () => {
      if (det.open && det.children.length === 1) det.append(...entries.map(([kk, x]) => treeNode(kk, x, depth + 1)));
    }, { once: false });
    if (depth < 2) det.append(...entries.map(([kk, x]) => treeNode(kk, x, depth + 1)));
    return det;
  }
  function jsonView(d) {
    const root = h("div", { class: "tree" }, treeNode(null, d.data, 0));
    const setAll = (open) => { for (let i = 0; i < 6; i++) root.querySelectorAll("details").forEach((x) => { if (x.open !== open) { x.open = open; x.dispatchEvent(new Event("toggle")); } }); };
    return frame("json", d.title, root, () => ({ title: d.title || "Data", markdown: "```json\n" + JSON.stringify(d.data, null, 2) + "\n```", json: d.data }),
      { extra: [h("button", { class: "sm ghost", onclick: () => setAll(true) }, "Expand all"), h("button", { class: "sm ghost", onclick: () => setAll(false) }, "Collapse")] });
  }

  // ---------- diff view ----------
  function wordDiff(a, b) {
    const x = String(a ?? "").split(/(\s+)/), y = String(b ?? "").split(/(\s+)/);
    if (x.length * y.length > 4e6) return null;
    const n = x.length, m = y.length, dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    const out = []; let i = 0, j = 0;
    while (i < n && j < m) {
      if (x[i] === y[j]) { out.push(["=", x[i]]); i++; j++; } else if (dp[i + 1][j] >= dp[i][j + 1]) out.push(["-", x[i++]]); else out.push(["+", y[j++]]);
    }
    while (i < n) out.push(["-", x[i++]]);
    while (j < m) out.push(["+", y[j++]]);
    return out;
  }
  function diffPair(a, b, mode = "split") {
    const ops = wordDiff(plain(a), plain(b));
    if (!ops) return h("div", { class: "dv-cols" }, h("div", { class: "dv-side old" }, plain(a)), h("div", { class: "dv-side new" }, plain(b)));
    if (mode === "inline") return h("div", { class: "dv-inline" }, ops.map(([t, s]) => t === "=" ? s : h(t === "+" ? "ins" : "del", null, s)));
    return h("div", { class: "dv-cols" },
      h("div", { class: "dv-side old", "aria-label": "Before" }, ops.filter((o) => o[0] !== "+").map(([t, s]) => t === "=" ? s : h("del", null, s))),
      h("div", { class: "dv-side new", "aria-label": "After" }, ops.filter((o) => o[0] !== "-").map(([t, s]) => t === "=" ? s : h("ins", null, s))));
  }
  const plain = (v) => v == null ? "" : typeof v === "object" ? (v.local || v.iso || v.raw || JSON.stringify(v)) : String(v);
  function diffView(d) {
    let mode = store.get("diffmode") || "split";
    const host = h("div");
    const draw = () => {
      mount(host, d.changes.length ? d.changes.map((c) => {
        const head = h("div", { class: "dv-head" }, h("span", { class: "strong" }, c.field), c.when ? h("span", { class: "tiny muted" }, ` · ${fmtDate(c.when)}`) : null);
        return h("div", { class: "dv" }, head, diffPair(c.old, c.new, mode));
      }) : h("div", { class: "muted small" }, "No recorded edits."));
    };
    draw();
    const toggle = h("button", { class: "sm ghost", onclick: () => { mode = mode === "split" ? "inline" : "split"; store.set("diffmode", mode); toggle.textContent = mode === "split" ? "Inline" : "Side by side"; draw(); } }, mode === "split" ? "Inline" : "Side by side");
    const rows = d.changes.map((c) => [c.field, plain(c.old), plain(c.new), c.when || ""]);
    return frame("diff", d.title, host, () => ({ title: d.title, markdown: rows.map((r) => `### ${r[0]}${r[3] ? ` (${r[3]})` : ""}\n\n- Before: ${r[1]}\n- After: ${r[2]}`).join("\n\n"),
      csv: toCSV(["Field", "Before", "After", "When"], rows), json: d.changes }), { extra: [toggle] });
  }

  // ---------- score model: live mirror of canvas_hub/scoring.py ----------
  function evaluate(model, state) {
    const st = { include: state.include || {}, values: state.values || {} };
    const lines = model.buckets.map((b) => {
      const r = { id: b.id, label: b.label, max: b.max, status: b.status, unit: b.unit, group: b.group || null, points: 0, lost: 0, included: true, gated: false, note: null };
      if (b.status === "excused") return { ...r, included: false, max: 0 };
      if (b.status === "earned" || b.status === "lost") { r.points = Math.min(b.max, b.earned || 0); r.lost = b.max - r.points; return r; }
      if (b.status === "locked") return { ...r, lost: b.max, included: false, note: "locked" };
      r.included = st.include[b.id] !== false;
      if (!r.included) return { ...r, note: "skipped" };
      let pts;
      if (b.unit === "hours") {
        const hrs = st.values[b.id] ?? b.full_at ?? 0;
        r.value = hrs;
        pts = b.full_at ? b.max * Math.min(1, hrs / b.full_at) : b.max;
        if (b.gating === "min_threshold" && hrs < (b.min_value || 0)) { pts = 0; r.gated = true; r.note = `0 until ${fmtN(b.min_value)} h`; }
        else if (b.gating === "all_or_nothing" && b.full_at && hrs < b.full_at) { pts = 0; r.gated = true; r.note = "all or nothing"; }
      } else {
        const v = Math.min(b.max, st.values[b.id] ?? b.max);
        r.value = v; pts = v;
        if (b.gating === "all_or_nothing" && v < b.max) { pts = 0; r.gated = true; r.note = "all or nothing"; }
        else if (b.gating === "min_threshold" && v < (b.min_value || 0)) { pts = 0; r.gated = true; r.note = `0 below ${fmtN(b.min_value)}`; }
      }
      r.points = Math.round(pts * 1e4) / 1e4;
      return r;
    });
    const sum = (f) => lines.reduce((s, x) => s + f(x), 0);
    const booked = sum((x) => ["earned", "lost"].includes(x.status) ? x.points : 0);
    const projected = sum((x) => x.points);
    const ceiling = booked + sum((x) => x.status === "pending" ? x.max : 0);
    const t = { booked, projected, ceiling, lost: sum((x) => x.lost), remaining: sum((x) => x.status === "pending" ? x.points : 0),
      skipped: sum((x) => x.status === "pending" && !x.included ? x.max : 0), possible: sum((x) => x.max),
      open_count: lines.filter((x) => x.status === "pending" && x.included).length, threshold: model.threshold };
    if (model.threshold != null) Object.assign(t, { margin: projected - model.threshold, passes: projected >= model.threshold - 1e-9,
      skip_budget: Math.max(0, projected - model.threshold), ceiling_passes: ceiling >= model.threshold - 1e-9 });
    const groups = {};
    for (const x of lines) {
      const g = groups[x.group || "Other"] ||= { earned: 0, remaining: 0, lost: 0, skipped: 0 };
      if (x.status === "earned" || x.status === "lost") g.earned += x.points;
      else if (x.status === "pending") { if (x.included) g.remaining += x.points; else g.skipped += x.max; }
      g.lost += x.lost;
    }
    return { lines, totals: t, groups };
  }

  // Shared plumbing for saved components: debounced save with version check, undo, pin.
  // Saves run one at a time. Each carries the edit number it was taken at; when a
  // reply arrives after newer local edits, those edits are kept (only the server's
  // version is taken) and the next queued save sends them.
  function savedComponent(view, render) {
    let v = view, timer = null, saving = Promise.resolve(), isFull = false, edits = 0;
    let seen = view.state;                 // the last state the server confirmed: sent as `base` so it can merge
    let acked = 0;                         // edits the server has confirmed; fewer than `edits` = local changes pending
    const el = h("div", { class: "saved-cmp" });
    const status = h("span", { class: "tiny muted save-state", "aria-live": "polite" });
    const redraw = () => { mount(el, render(v, api_)); };
    const queue = (fn) => (saving = saving.then(fn, fn));
    // Edits made while a save was in flight are re-applied on top of the server's answer (which may hold
    // changes from elsewhere, e.g. a linked to-do finished meanwhile): per key, and per entry for maps.
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const isMap = (x) => x && typeof x === "object" && !Array.isArray(x);
    const rebase = (sentState, local, server) => {
      const out = { ...server };
      for (const k of new Set([...Object.keys(local), ...Object.keys(sentState)])) {
        if (same(local[k], sentState[k])) continue;                 // not touched since the request: server's
        if (isMap(local[k]) && isMap(sentState[k]) && isMap(server[k])) {
          const m = { ...server[k] };
          for (const j of new Set([...Object.keys(local[k]), ...Object.keys(sentState[k])])) {
            if (same(local[k][j], sentState[k][j])) continue;
            if (local[k][j] === undefined) delete m[j]; else m[j] = local[k][j];
          }
          out[k] = m;
        } else if (local[k] === undefined) delete out[k];
        else out[k] = local[k];
      }
      return out;
    };
    let pending = null;
    const persist = () => {
      clearTimeout(timer);
      status.textContent = "Saving…";
      pending = () => queue(async () => {
        const sent = edits, sentState = v.state;
        try {
          const r = await api(A(`/components/${v.id}`), { method: "PATCH", body: { state: sentState, base: seen, expected_version: v.version } });
          seen = r.state;
          if (edits === sent) acked = sent;
          v = edits === sent ? r : { ...r, state: rebase(sentState, v.state, r.state) };
          if (edits === sent) redraw();
          status.textContent = edits === sent ? `Saved ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Saving…";
        } catch (e) {
          if (e.status === 409) { v = await api(A(`/components/${v.id}`)); seen = v.state; acked = edits; redraw(); status.textContent = "Reloaded (changed elsewhere)"; }
          else { status.textContent = "Not saved"; toast(e.message || "Could not save", true); }
        }
      });
      timer = setTimeout(() => { const run = pending; pending = null; if (run) run(); }, 350);
    };
    const flush = () => { clearTimeout(timer); const run = pending; pending = null; if (run) run(); };
    const api_ = {
      setState: (fn) => { edits += 1; v = { ...v, state: fn(structuredClone(v.state)) }; redraw(); persist(); },
      undo: () => { flush(); return queue(async () => {
        const r = await guard(() => api(A(`/components/${v.id}`), { method: "PATCH", body: { undo: true } }));
        if (r) { edits += 1; acked = edits; v = r; seen = r.state; redraw(); status.textContent = "Undone"; }
      }); },
      pin: () => queue(async () => {
        const r = await guard(() => api(A(`/components/${v.id}`), { method: "PATCH", body: { pinned: !v.pinned } }));
        if (r) { v = { ...r, state: v.state }; redraw(); toast(r.pinned ? "Pinned to Trackers" : "Unpinned"); }
      }),
      status,
      get full() { return isFull; },
      onFull: (on) => { isFull = on; },
      get view() { return v; },
    };
    // The chat holds the definition from when it was created, and a cached copy may be shown again after
    // edits elsewhere (Trackers, the assistant): load the latest, unless local changes are still unsaved.
    const reload = () => api(A(`/components/${v.id}`)).then((fresh) => {
      if (edits === acked && !pending && fresh.version !== v.version) { v = fresh; seen = fresh.state; redraw(); }
    }).catch(() => {});
    el.refresh = reload;
    redraw();
    reload();
    return el;
  }
  const savedTools = (v, ctl) => [ctl.status,
    h("button", { class: "sm ghost", title: "Undo the last change", disabled: v.can_undo ? null : true, onclick: () => ctl.undo() }, "↶ Undo"),
    h("button", { class: "sm ghost", "aria-pressed": v.pinned ? "true" : "false", onclick: () => ctl.pin() }, v.pinned ? "★ Pinned" : "☆ Pin")];

  function scoreModelView(view) {
    return savedComponent(view, (v, ctl) => {
      const model = v.spec, state = v.state || {};
      const comp = evaluate(model, state);
      const t = comp.totals, unit = ` ${model.unit_label || "pts"}`;
      const byId = Object.fromEntries(model.buckets.map((b) => [b.id, b]));
      const next = model.buckets.filter((b) => b.status === "pending" && b.due && (state.include || {})[b.id] !== false).sort((a, b) => a.due < b.due ? -1 : 1)[0];
      const passPill = t.threshold == null ? null : t.passes ? pill("ok", `PASS · +${fmtN(t.margin)}`) : pill("bad", `${fmtN(-t.margin)} short`);
      const summary = h("div", { class: "sl-side", "aria-label": "Summary" },
        h("div", { class: "sl-big" }, h("span", { class: "num" }, fmtN(t.projected)), h("span", { class: "muted small" }, t.threshold != null ? ` / ${fmtN(t.threshold)} to pass` : unit)), passPill,
        gaugeEl([{ label: "Booked", value: t.booked }, { label: "Planned", value: t.remaining }], Math.max(t.possible, 1), t.threshold, "", "bar"),
        h("dl", { class: "sl-kv" },
          h("dt", null, "Booked"), h("dd", { class: "num" }, fmtN(t.booked)),
          h("dt", null, "Ceiling"), h("dd", { class: "num" }, fmtN(t.ceiling), t.ceiling_passes === false ? h("span", { class: "bad-text tiny" }, " below pass") : null),
          h("dt", null, "Lost"), h("dd", { class: "num" }, fmtN(t.lost)),
          t.threshold != null ? [h("dt", null, "Skip budget"), h("dd", { class: "num" }, fmtN(t.skip_budget))] : null,
          h("dt", null, "Open lines"), h("dd", { class: "num" }, String(t.open_count)),
          next ? [h("dt", null, "Next due"), h("dd", null, next.label, h("div", null, countdown(next.due)))] : null));
      const scen = model.scenarios && model.scenarios.length ? h("div", { class: "row", role: "radiogroup", "aria-label": "Scenario" },
        h("span", { class: "small muted" }, "Scenario:"), model.scenarios.map((s) => h("button", { class: "chip" + (state.scenario === s.id ? " on" : ""), role: "radio",
          "aria-checked": state.scenario === s.id ? "true" : "false",
          onclick: () => ctl.setState(() => ({ include: { ...s.include }, values: { ...s.values }, scenario: s.id })) }, s.label))) : null;
      const STATUS = { earned: ["ok", "Earned"], pending: ["info", "To do"], lost: ["bad", "Lost"], locked: ["muted", "Locked"], excused: ["muted", "Excused"] };
      const groups = new Map();
      for (const x of comp.lines) { const g = byId[x.id].group || ""; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(x); }
      const rowsFor = (lines) => lines.map((x) => {
        const b = byId[x.id];
        const pending = b.status === "pending";
        const box = h("input", { type: "checkbox", checked: x.included && b.status !== "locked" ? true : null, disabled: pending ? null : true,
          "aria-label": `Include ${b.label}`, onchange: (e) => ctl.setState((s) => ({ ...s, scenario: null, include: { ...(s.include || {}), [b.id]: e.target.checked } })) });
        const input = pending && x.included ? h("input", { type: "number", class: "num-in", min: 0, step: b.unit === "hours" ? 0.5 : 1, max: b.unit === "hours" ? null : b.max,
          value: x.value ?? "", "aria-label": `${b.unit === "hours" ? "Hours" : "Points"} for ${b.label}`,
          onchange: (e) => { const n = e.target.value === "" ? null : Number(e.target.value); ctl.setState((s) => { const vals = { ...(s.values || {}) }; if (n == null || Number.isNaN(n)) delete vals[b.id]; else vals[b.id] = n; return { ...s, scenario: null, values: vals }; }); } }) : null;
        return h("tr", { class: (x.included ? "" : "off ") + (x.gated ? "gated" : "") },
          h("td", null, box),
          h("td", null, itemLink(b.item_id, b.label), h("span", { class: "st-inline" }, pill(STATUS[b.status][0], STATUS[b.status][1])), b.due ? h("div", { class: "tiny" }, fmtDate(b.due, { weekday: true }), " · ", countdown(b.due)) : null,
            b.note ? h("div", { class: "tiny muted" }, b.note) : null),
          h("td", null, pill(STATUS[b.status][0], STATUS[b.status][1]), b.gating !== "none" ? h("div", { class: "tiny muted" }, b.gating === "all_or_nothing" ? "all or nothing" : `min ${fmtN(b.min_value)}${b.unit === "hours" ? " h" : ""}`) : null),
          h("td", { class: "num" }, input || (b.unit === "hours" && x.value != null ? `${fmtN(x.value)} h` : "")),
          h("td", { class: "num" }, h("span", { class: "strong" }, fmtN(x.points)), ` / ${fmtN(b.max)}`, x.note && x.note !== "skipped" ? h("div", { class: "tiny bad-text" }, x.note) : null));
      });
      const table = h("div", { class: "table-wrap" }, h("table", { class: "t score-t" },
        h("thead", null, h("tr", null, h("th", { "aria-label": "Include" }, "✓"), h("th", null, "Line"), h("th", null, "Status"), h("th", { class: "num" }, "Expected"), h("th", { class: "num" }, "Points"))),
        [...groups.entries()].map(([g, lines]) => h("tbody", null, groups.size > 1 ? h("tr", { class: "t-group" }, h("td", { colspan: 5 },
          `${g || "Other"} · ${fmtN(lines.reduce((s, x) => s + x.points, 0))} / ${fmtN(lines.reduce((s, x) => s + x.max, 0))}`)) : null, rowsFor(lines)))));
      const gnames = Object.keys(comp.groups);
      const gchart = gnames.length > 1 ? chartView({ title: "By category", kind: "stacked", labels: gnames, threshold: null,
        series: [{ name: "Earned", values: gnames.map((g) => comp.groups[g].earned) }, { name: "Planned", values: gnames.map((g) => comp.groups[g].remaining) },
          { name: "Lost", values: gnames.map((g) => comp.groups[g].lost) }, { name: "Skipped", values: gnames.map((g) => comp.groups[g].skipped) }] }) : null;
      const cols = ["Line", "Status", "Included", "Points", "Max", "Note"];
      const rows = comp.lines.map((x) => [x.label, x.status, x.included ? "yes" : "no", x.points, byId[x.id].max, x.note || ""]);
      const data = () => ({ title: model.title, csv: toCSV(cols, rows), json: { model, state, totals: t },
        markdown: `**${model.title}** — projected ${fmtN(t.projected)}${t.threshold != null ? ` / ${fmtN(t.threshold)} (${t.passes ? "pass" : "below"}, margin ${fmtN(t.margin)})` : ""}, booked ${fmtN(t.booked)}, ceiling ${fmtN(t.ceiling)}, lost ${fmtN(t.lost)}\n\n${toMD(cols, rows)}` });
      return frame("score", model.title, h("div", { class: "sidebar-layout" }, summary, h("div", { class: "sl-main" }, scen, table, gchart)), data,
        { extra: savedTools(v, ctl), full: ctl.full, onFull: ctl.onFull });
    });
  }

  function checklistView(view) {
    return savedComponent(view, (v, ctl) => {
      const spec = v.spec, state = v.state || {}, done = state.done || {}, collapsed = state.collapsed || {};
      const all = spec.sections.flatMap((s) => s.rows);
      const pts = all.reduce((s, r) => s + (r.points || 0), 0), dpts = all.reduce((s, r) => s + (done[r.id] ? r.points || 0 : 0), 0);
      const dc = all.filter((r) => done[r.id]).length;
      const progress = (a, b) => h("div", { class: "progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": b, "aria-valuenow": a },
        h("span", { style: `width:${b ? a / b * 100 : 0}%` }));
      const sections = spec.sections.map((s) => {
        const sp = s.rows.reduce((t, r) => t + (r.points || 0), 0), sd = s.rows.reduce((t, r) => t + (done[r.id] ? r.points || 0 : 0), 0);
        const sc = s.rows.filter((r) => done[r.id]).length;
        const det = h("details", { class: "cl-sec", open: collapsed[s.id] ? null : true },
          h("summary", null, h("span", { class: "strong" }, s.title || "Items"), h("span", { class: "tiny muted" }, ` ${sc}/${s.rows.length}${sp ? ` · ${fmtN(sd)}/${fmtN(sp)} pts` : ""}`), progress(sc, s.rows.length)),
          h("ul", { class: "cl-rows" }, s.rows.map((r) => h("li", { class: done[r.id] ? "done" : "" },
            h("label", { class: "check" }, h("input", { type: "checkbox", checked: done[r.id] ? true : null,
              onchange: (e) => ctl.setState((x) => { const d2 = { ...(x.done || {}) }; if (e.target.checked) d2[r.id] = true; else delete d2[r.id]; return { ...x, done: d2 }; }) }),
            h("span", null, r.label)),
            h("span", { class: "cl-meta" }, r.points != null ? h("span", { class: "num tiny" }, `${fmtN(r.points)} pts`) : null,
              r.due ? countdown(r.due.length === 10 ? `${r.due}T23:59:00` : r.due) : null,
              r.item_id ? itemLink(r.item_id, "details") : null),
            r.note ? h("div", { class: "tiny muted cl-note" }, r.note) : null))));
        det.addEventListener("toggle", () => { const want = !det.open; if (!!collapsed[s.id] !== want) ctl.setState((x) => ({ ...x, collapsed: { ...(x.collapsed || {}), [s.id]: want } })); });
        return det;
      });
      const cols = ["Section", "Item", "Done", "Points", "Due"];
      const rows = spec.sections.flatMap((s) => s.rows.map((r) => [s.title, r.label, done[r.id] ? "yes" : "no", r.points ?? "", r.due || ""]));
      const data = () => ({ title: spec.title, csv: toCSV(cols, rows), json: { spec, state },
        markdown: `**${spec.title}** — ${dc}/${all.length} done${pts ? `, ${fmtN(dpts)}/${fmtN(pts)} pts` : ""}\n\n` + spec.sections.map((s) => `${s.title ? `### ${s.title}\n` : ""}${s.rows.map((r) => `- [${done[r.id] ? "x" : " "}] ${r.label}${r.points != null ? ` (${fmtN(r.points)} pts)` : ""}${r.due ? ` — due ${r.due}` : ""}`).join("\n")}`).join("\n\n") });
      afterRender();
      return frame("checklist", spec.title, h("div", null,
        h("div", { class: "cl-top" }, h("span", { class: "small" }, `${dc}/${all.length} done${pts ? ` · ${fmtN(dpts)}/${fmtN(pts)} pts` : ""}${spec.sync_todos ? " · linked to your to-dos" : ""}`), progress(dc, all.length)),
        sections), data, { extra: savedTools(v, ctl), full: ctl.full, onFull: ctl.onFull });
    });
  }

  // ---------- confirmed writes to Canvas ----------
  const WSTATUS = { prepared: ["warn", "Not sent — waiting for you"], sending: ["info", "Sending…"], sent: ["ok", "Sent to Canvas"],
    failed: ["bad", "Failed"], cancelled: ["muted", "Cancelled"], expired: ["muted", "Expired"] };
  function writePreviewView(initial) {
    const host = h("div");
    const draw = (w) => {
      const pv = w.preview;
      const st = WSTATUS[w.status] || ["muted", w.status];
      const confirmRow = h("div", { class: "write-confirm", hidden: true },
        h("div", { class: "strong" }, `${pv.action} in ${pv.course || "course " + w.course_id}?`),
        h("div", { class: "small muted" }, "This sends exactly what is shown above, from your signed-in Canvas session. It cannot be undone from Canvas Hub."),
        h("div", { class: "row", style: "margin-top:8px" },
          h("button", { class: "danger", onclick: async (e) => {
            e.target.disabled = true; e.target.textContent = "Sending…";
            const r = await guard(() => api(A(`/writes/${w.id}/confirm`), { method: "POST", body: { preview_hash: pv.hash } }));
            if (r) {
              draw(r);
              if (r.status === "sending") { toast("Still sending — the browser is busy; this updates when Canvas answers"); follow(w.id); }
              else toast(r.status === "sent" ? "Sent to Canvas" : `Not sent: ${r.error || r.status}`, r.status !== "sent");
            }
            else { e.target.disabled = false; e.target.textContent = "Yes, send it"; }
          } }, "Yes, send it"),
          h("button", { class: "ghost", onclick: () => { confirmRow.hidden = true; actions.hidden = false; } }, "Back")));
      const actions = h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "primary", onclick: () => { confirmRow.hidden = false; actions.hidden = true; } }, "Send to Canvas…"),
        h("button", { class: "ghost", onclick: async () => { const r = await guard(() => api(A(`/writes/${w.id}/cancel`), { method: "POST" })); if (r) draw(r); } }, "Cancel"),
        h("span", { class: "tiny muted" }, `Expires ${fmtDate(w.expires_at)}`));
      const fields = [
        ["Action", pv.action], ["Course", pv.course || w.course_id],
        pv.target ? ["To", w.target_item_id ? itemLink(w.target_item_id, pv.target) : pv.target] : null,
        pv.recipients ? ["Recipients", pv.recipients.map((r) => `${r.name || "user"} (${r.id})`).join(", ")] : null,
        pv.subject ? ["Subject", pv.subject] : null,
        pv.file ? ["File", `${pv.file.filename} · ${(pv.file.size / 1024).toFixed(1)} KB · sha256 ${pv.file.sha256.slice(0, 12)}…`] : null,
        pv.canvas_url ? ["In Canvas", h("a", { href: pv.canvas_url, target: "_blank", rel: "noopener noreferrer" }, pv.canvas_url)] : null,
      ].filter(Boolean);
      const res = w.result ? Object.entries(w.result).filter(([, v]) => v != null).map(([k, v]) => `${k}: ${v}`).join(" · ") : null;
      mount(host, frame("write", "Prepared for Canvas", h("div", { class: "write-card" + (w.status === "prepared" ? " pending" : "") },
        h("div", { class: "row" }, pill(st[0], st[1]), h("span", { class: "tiny muted" }, `prepared by ${w.prepared_by} ${ago(w.created_at)}`)),
        h("dl", { class: "write-fields" }, fields.map(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
        pv.body != null ? h("div", null, h("div", { class: "tiny muted" }, "Exact text that will be sent:"), h("pre", { class: "write-body" }, pv.body)) : null,
        pv.comment ? h("div", null, h("div", { class: "tiny muted" }, "Comment with the file:"), h("pre", { class: "write-body" }, pv.comment)) : null,
        (w.warnings || []).map((x) => h("div", { class: "small warn-text" }, "⚠ ", x)),
        w.status === "prepared" ? [actions, confirmRow] : null,
        w.status === "sent" ? h("div", { class: "small ok-text" }, `Sent ${fmtDate(w.finished_at)}${res ? ` · ${res}` : ""}`) : null,
        w.status === "failed" ? h("div", { class: "small bad-text" }, `Not sent: ${w.error}`) : null),
      () => ({ title: "Prepared write", markdown: `**${pv.action}** — ${pv.course || ""} — ${pv.target || pv.subject || ""}

${pv.body || ""}`, json: w })));
    };
    // A send that outlasts the request stays "sending" on the server until the browser finishes; poll for it.
    const follow = (id) => { const t = setInterval(async () => {
      if (!host.isConnected) { clearInterval(t); return; }
      const r = await api(A(`/writes/${id}`)).catch(() => null);
      if (r && r.status !== "sending") { clearInterval(t); draw(r); toast(r.status === "sent" ? "Sent to Canvas" : `Not sent: ${r.error || r.status}`, r.status !== "sent"); }
    }, 3000); };
    draw(initial);
    if (initial.status === "sending") follow(initial.id);
    api(A(`/writes/${initial.id}`)).then(draw).catch(() => {});
    return host;
  }
  function writesSettingsCard(data) {
    const s = data.settings;
    const LABEL = { submission_text: "Submit text to assignments", submission_file: "Upload files to assignments",
      discussion_reply: "Reply in discussions", message: "Send inbox messages" };
    const on = h("input", { type: "checkbox", checked: s.enabled ? true : null });
    const kinds = Object.keys(LABEL).map((k) => [k, h("input", { type: "checkbox", checked: s.kinds.includes(k) ? true : null })]);
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Writing to Canvas"), pill(s.enabled ? "warn" : "muted", s.enabled ? "On — with confirmation" : "Off")),
      h("p", { class: "small muted" }, "Off by default. When on, the assistant can prepare a submission, discussion reply or inbox message. Nothing is sent until you read the exact preview and press Send. Every write is logged below."),
      h("label", { class: "check" }, on, h("strong", null, "Allow confirmed writes for this account")),
      h("div", { class: "perm-list", style: "margin:6px 0 0 24px" }, kinds.map(([k, el]) => h("label", { class: "check small" }, el, LABEL[k]))),
      h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "primary", onclick: async () => {
        const r = await guard(() => api(A("/write-settings"), { method: "PUT", body: { enabled: on.checked, kinds: kinds.filter(([, el]) => el.checked).map(([k]) => k) } }));
        if (r) { toast(r.enabled ? "Confirmed writes are on" : "Writes are off"); env.route(); } } }, "Save")),
      data.writes.length ? h("div", { style: "margin-top:12px" }, h("div", { class: "small strong" }, "Write log"), writesLog(data.writes)) : null);
  }
  function writesLog(list) {
    return h("ul", { class: "list" }, list.map((w) => { const st = WSTATUS[w.status] || ["muted", w.status];
      return h("li", { class: "spread" }, h("div", null, h("span", { class: "strong" }, w.preview.action), " · ", w.target_title || w.preview.subject || "",
        h("div", { class: "tiny muted" }, `${w.preview.course || w.course_id} · ${fmtDate(w.finished_at || w.created_at)}${w.error ? ` · ${w.error}` : ""}`)), pill(st[0], st[1])); }));
  }
  function filesCard(files) {
    const input = h("input", { type: "file", "aria-label": "Add a file" });
    input.addEventListener("change", async () => {
      const f = input.files[0]; if (!f) return;
      const r = await guard(() => api(A("/files"), { method: "POST", raw: true, body: f, contentType: f.type || "application/octet-stream", headers: { "X-Filename": encodeURIComponent(f.name) } }));
      if (r) { toast(`Saved ${r.filename}`); env.route(); }
    });
    return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Files"), h("label", { class: "btn sm" }, "Add a file…", input)),
      h("p", { class: "small muted" }, "Finished work the assistant or you saved: download it, or have the assistant attach it to a prepared submission."),
      files.length ? h("table", { class: "t" }, h("tbody", null, files.map((f) => h("tr", null,
        h("td", null, h("span", { class: "strong" }, f.title), h("div", { class: "tiny muted" }, `${f.filename} · ${(f.size / 1024).toFixed(1)} KB · by ${f.created_by} · ${ago(f.created_at)}`)),
        h("td", { class: "nowrap" }, h("a", { class: "btn sm", href: A(`/files/${f.id}/download`) }, "Download"), " ",
          h("button", { class: "sm ghost", onclick: async () => { if (!confirm(`Delete ${f.filename}?`)) return; if (await guard(() => api(A(`/files/${f.id}`), { method: "DELETE" }))) env.route(); } }, "Delete")))))) :
        h("div", { class: "muted small" }, "No files yet."));
  }

  // ---------- dispatcher with a per-result cache ----------
  // The chat re-renders while the assistant works; keep the same element so
  // sorting, expanded rows and unsaved inputs survive. Callbacks read the chat
  // context through `ref`, which is updated on reuse (the chat view may be new).
  const cache = new Map();
  function componentView(d, ctx = {}, key = null) {
    if (!d) return null;
    if (key && cache.has(key)) {
      const hit = cache.get(key);
      // Shown again in a different chat view: a saved component may have changed meanwhile (not on every re-render).
      if (hit.ref.ctx !== ctx && hit.el.refresh) hit.el.refresh();
      hit.ref.ctx = ctx;
      return hit.el;
    }
    const ref = { ctx };
    let el = null;
    try {
      if (d.component === "deadlines") el = deadlinesView(d);
      else if (d.component === "timetable") el = timetableView(d);
      else if (d.component === "items") el = itemsView(d);
      else if (d.component === "table") el = tableView(d);
      else if (d.component === "chart") el = chartView(d);
      else if (d.component === "gauge") el = gaugeView(d);
      else if (d.component === "artifact") el = artifactView(d);
      else if (d.component === "json") el = jsonView(d);
      else if (d.component === "diff") el = diffView(d);
      else if (d.component === "score_model") el = scoreModelView(d.view);
      else if (d.component === "checklist") el = checklistView(d.view);
      else if (d.component === "write_preview") el = writePreviewView(d.write);
      else if (d.component === "choices") el = h("div", { class: "cmp cmp-choices" }, h("div", { class: "small" }, d.question),
        h("div", { class: "chips", style: "margin-top:6px" }, d.options.map((o) => h("button", { class: "chip", onclick: () => ref.ctx.send && ref.ctx.send(o) }, o))));
    } catch (e) {
      el = h("div", { class: "cmp muted small" }, `Could not show this ${d.component}: ${e.message}`);
    }
    if (key && el) { cache.set(key, { el, ref }); if (cache.size > 300) cache.delete(cache.keys().next().value); }
    return el;
  }

  // ---------- Trackers page ----------
  async function trackersView(main, pageHead, empty) {
    const [pinned, all] = await Promise.all([api(A("/components?pinned=true")), api(A("/components"))]);
    const others = all.components.filter((c) => !c.pinned);
    const cards = pinned.components.map((v) => v.kind === "score_model" ? scoreModelView(v) : checklistView(v));
    mount(main, pageHead("Trackers", "Score models and checklists you pinned. Ticks and values are saved on this computer; the assistant can read them back."),
      cards.length ? h("div", { class: "stack" }, cards) : empty("Nothing pinned yet", "Ask the assistant for a score model or checklist, then press ☆ Pin on it."),
      others.length ? h("div", { class: "card", style: "margin-top:16px" }, h("h2", null, "Other saved components"),
        h("ul", { class: "list" }, others.map((c) => h("li", { class: "spread" }, h("div", null, h("span", { class: "strong" }, c.title), h("span", { class: "tiny muted" }, ` · ${c.kind === "score_model" ? "score model" : "checklist"} · ${ago(c.updated_at)}`)),
          h("div", { class: "row" }, h("button", { class: "sm", onclick: async () => { if (await guard(() => api(A(`/components/${c.id}`), { method: "PATCH", body: { pinned: true } }))) env.route(); } }, "☆ Pin"),
            h("button", { class: "sm ghost", onclick: async () => { if (!confirm(`Delete “${c.title}”?`)) return; if (await guard(() => api(A(`/components/${c.id}`), { method: "DELETE" }))) env.route(); } }, "Delete")))))) : null);
    afterRender();
  }

  return { componentView, openDrawer, closeDrawer, trackersView, countdown, copyText, download, evaluate, diffPair,
    writesSettingsCard, writesLog, filesCard };
}
