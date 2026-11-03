// Small element stub: exercise the real renderer without launching a browser.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { makeComponents } from '../canvas_hub/web/static/components.js';

class Element {
  constructor(tag, attrs, children) {
    this.tag = tag; this.attrs = attrs || {}; this.children = []; this.handlers = {};
    this.hidden = !!this.attrs.hidden;
    const classes = new Set((this.attrs.class || '').split(' '));
    this.classList = { contains: (c) => classes.has(c), add: (c) => classes.add(c), remove: (c) => classes.delete(c),
      toggle: (c, yes) => { (yes ?? !classes.has(c)) ? classes.add(c) : classes.delete(c); } };
    this.append(...children);
  }
  append(...children) { this.children.push(...children.flat(Infinity).filter((x) => x != null && x !== false)); }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  setAttribute(key, value) { this.attrs[key] = value; }
  focus() {}
  get textContent() { return this.children.map((x) => x instanceof Element ? x.textContent : String(x)).join(''); }
  set textContent(text) { this.children = [text]; }
  fire(type) { return (this.attrs['on' + type] || this.handlers[type])?.({ target: this, preventDefault() {}, stopPropagation() {} }); }
}
const h = (tag, attrs, ...children) => new Element(tag, attrs, children);
const mount = (el, ...children) => { el.children = []; el.append(...children); };
const find = (el, predicate) => el instanceof Element ? [ ...(predicate(el) ? [el] : []), ...el.children.flatMap((x) => find(x, predicate)) ] : [];
const byClass = (el, name) => find(el, (x) => x.classList.contains(name));
const button = (el, prefix) => find(el, (x) => x.tag === 'button' && x.textContent.startsWith(prefix))[0];
globalThis.document = { addEventListener() {}, body: h('body', null) };
globalThis.setInterval = () => 0;
globalThis.setTimeout = () => 0;
let copied;
Object.defineProperty(globalThis, 'navigator', { value: { clipboard: { writeText: async (text) => { copied = text; } } } });
const { display, items } = JSON.parse(readFileSync(0, 'utf8'));
const requests = [];
const components = makeComponents({ h, mount, A: (path) => path, TYPE_LABEL: {}, fmtDate: (value) => value || '', ago: () => 'now',
  linkedStudy: async () => null, studyAction: () => null,
  api: async (path, options) => {
    assert.equal(options, undefined); requests.push(path);
    const item = items.find((x) => path === `/items/${x.id}`);
    assert.ok(item);
    return { ...item, item_type: 'assignment', fields: [] };
  } });
const table = components.componentView(display, {}, 'table');
const links = () => find(table, (x) => x.tag === 'a' && x.classList.contains('cmp-link'));
const expected = Object.fromEntries(items.map((item) => [item.title, `#/item/${item.id}`]));
function checkLinks() {
  assert.equal(links().length, 6);
  assert.deepEqual(Object.fromEntries(links().map((link) => [link.textContent, link.attrs.href])), expected);
  assert.equal(byClass(table, 't-group').length, 2);
  for (const row of find(table, (x) => x.tag === 'tr' && x.classList.contains('expandable'))) {
    assert.equal(find(row.children[0], (x) => x.tag === 'a').length, 0, 'the course cell is not linked');
  }
}
checkLinks();
button(table, 'Item').fire('click'); checkLinks();
button(table, 'Item').fire('click'); checkLinks();
button(table, 'Due').fire('click'); checkLinks();
const filter = find(table, (x) => x.tag === 'input')[0];
filter.value = 'Beta'; filter.fire('input');
assert.deepEqual(links().map((link) => [link.textContent, link.attrs.href]), [['Beta task', expected['Beta task']]]);
find(table, (x) => x.tag === 'button' && x.attrs['aria-label'] === 'Details for Beta task')[0].fire('click');
assert.equal(byClass(table, 't-detail')[0].textContent, 'Beta task detail');
button(table, 'Due').fire('click');
assert.equal(byClass(table, 't-detail')[0].textContent, 'Beta task detail');
links()[0].fire('click');
await new Promise(setImmediate);
assert.deepEqual(requests, [`/items/${items.find((x) => x.title === 'Beta task').id}`]);
assert.equal(find(document.body, (x) => x.tag === 'h2')[0].textContent, 'Beta task');
components.closeDrawer();
assert.equal(byClass(document.body, 'open').length, 0);
button(table, 'Copy ▾').fire('click');
button(table, 'Copy as CSV').fire('click');
await Promise.resolve();
assert.equal(copied, 'Course,Item,Due\nGroup B,Beta task,2');
filter.value = ''; filter.fire('input'); checkLinks();
// Legacy raw arrays have content/details but no trusted navigation until the
// server validates them. The client must never fall back to stale parallel IDs.
const old = components.componentView({ component: 'table', columns: ['Course', 'Item'], rows: [['Group', 'Beta task']],
  details: ['Legacy detail'], item_ids: [items[0].id] });
assert.equal(find(old, (x) => x.tag === 'a').length, 0);
assert.ok(old.textContent.includes('Beta task'));
button(old, '▸').fire('click');
assert.equal(byClass(old, 't-detail')[0].textContent, 'Legacy detail');
const changed = JSON.parse(JSON.stringify(display));
delete changed.rows[0].item_id;
changed.link_warnings = ['This link is no longer available.'];
const refreshed = components.componentView(changed, {}, 'table');
assert.notEqual(refreshed, table, 'cached tables must refresh when link validation changes');
assert.equal(find(refreshed, (x) => x.tag === 'a').length, 5);
assert.ok(refreshed.textContent.includes('could not be verified'));
console.log('Table renderer: sorting, grouping, filtering, details, exports, drawer target, legacy fallback and cache refresh passed.');
