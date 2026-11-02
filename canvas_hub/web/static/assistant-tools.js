// Request-local tool choices. Selecting a tool never sends a request.
export function makeAssistantToolPicker({h, input, catalog}) {
  const selected = new Set();
  let revision = 0, opened = false, locked = false, destroyed = false;
  const catalogChoices = value => value ? [
    ...value.groups.map(g => ({id:`group:${g.id}`, command:g.id, label:`/${g.id} · ${g.label}`, description:g.description})),
    ...value.tools.map(t => ({id:`tool:${t.name}`, command:t.name, label:t.name, description:t.description}))
  ] : [];
  let choices = catalogChoices(catalog);
  let byCommand = new Map(choices.map(c => [c.command, c]));
  let byId = new Map(choices.map(c => [c.id, c]));
  const id = `assistant-tools-${Math.random().toString(36).slice(2)}`;
  const toggle = h('button', {type:'button', class:'assistant-tool-toggle', 'aria-label':'Choose tools for this message',
    'aria-haspopup':'dialog', 'aria-controls':id, 'aria-expanded':'false', title:'Choose tools for this message', disabled:!catalog}, '‹');
  const search = h('input', {type:'search', 'aria-label':'Search permitted tools', placeholder:'Search tools or groups…', autocomplete:'off'});
  const list = h('div', {class:'assistant-tool-options'});
  const message = h('div', {class:'tiny muted assistant-tool-feedback', role:'status'});
  const chips = h('div', {class:'assistant-tool-chips', 'aria-label':'Tools selected for the next message'});
  const explanation = h('p', {class:'tiny muted'});
  const explain = () => { explanation.textContent = catalog?.discovery_available === false
    ? 'Choices apply to one message and do not run anything. This gateway lists only tools reviewed in its setup; additional tools require a setup review.'
    : 'Choices add tools to the defaults for one message. They do not run anything. The assistant can discover more permitted tools.'; };
  explain();
  const panel = h('div', {id, class:'assistant-tool-panel', hidden:true, role:'dialog', 'aria-label':'Choose assistant tools'},
    h('div', {class:'assistant-tool-heading'}, h('strong', null, 'Tools for this message'),
      h('button', {type:'button', 'aria-label':'Close tool picker', onclick:()=>close(true)}, '×')),
    explanation,
    search, list);
  const root = h('div', {class:'assistant-tool-picker'}, toggle, panel);

  function fits(values) {
    const names = new Set(catalog.default_tools);
    if (!values.size && catalog.discovery_available === false) catalog.tools.forEach(t=>names.add(t.name));
    for (const value of values) {
      if (value.startsWith('group:')) catalog.groups.find(g=>`group:${g.id}`===value)?.tools.forEach(n=>names.add(n));
      else names.add(value.slice(5));
    }
    // Backend performs the authoritative serialized-schema budget check.
    const tokens = catalog.tools.filter(t=>names.has(t.name)).reduce((n,t)=>n+t.schema_tokens, 0) + (catalog.discovery_available === false ? 0 : 700);
    return names.size + (catalog.discovery_available === false ? 0 : 2) <= catalog.limits.max_advertised
      && (catalog.limits.schema_tokens == null || tokens <= catalog.limits.schema_tokens);
  }
  function renderChips() {
    chips.replaceChildren(...[...selected].map(value => h('button', {type:'button', class:'assistant-tool-chip',
      'aria-label':`Remove ${byId.get(value).label}`, disabled:locked, onclick:()=>select(value)}, byId.get(value).label, ' ×')));
    chips.hidden = !selected.size;
  }
  function select(value) {
    if (locked || destroyed) return;
    const next = new Set(selected);
    if (next.has(value)) next.delete(value); else next.add(value);
    if (next.size && !fits(next)) { message.textContent = 'Choose fewer groups or individual tools to stay within the tool budget.'; return; }
    selected.clear(); next.forEach(v=>selected.add(v)); revision++;
    message.textContent = selected.size ? 'Selected for the next message only. Nothing has run.' : '';
    renderChips(); renderOptions(value);
  }
  function renderOptions(focusId) {
    const hadFocus = panel.contains(document.activeElement);
    const q = search.value.replace(/^\//, '').trim().toLowerCase();
    const matching = choices.filter(c=>(c.label+' '+c.description).toLowerCase().includes(q));
    list.replaceChildren(...matching.map(c=>h('button', {type:'button', class:'assistant-tool-option',
      dataset:{selection:c.id}, 'aria-pressed':String(selected.has(c.id)), disabled:locked,
      onclick:()=>select(c.id)}, h('span', null, c.label), h('span', {class:'tiny muted'}, c.description))));
    if (!matching.length) list.append(h('p', {class:'tiny muted'}, 'No permitted tools match.'));
    if (focusId && hadFocus) {
      [...list.querySelectorAll('button')].find(b=>b.dataset.selection===focusId)?.focus();
    }
  }
  function position() {
    if (!opened) return;
    const rect = toggle.getBoundingClientRect(), width = Math.min(430, innerWidth-24);
    panel.style.width = `${width}px`;
    panel.style.left = `${Math.max(12, Math.min(rect.right-width, innerWidth-width-12))}px`;
    const above = rect.top > innerHeight-rect.bottom;
    panel.style.top = above ? 'auto' : `${rect.bottom+8}px`;
    panel.style.bottom = above ? `${innerHeight-rect.top+8}px` : 'auto';
    panel.style.maxHeight = `${Math.max(120, Math.min(440, (above ? rect.top : innerHeight-rect.bottom)-20))}px`;
  }
  function outside(e) { if (!root.contains(e.target) && !panel.contains(e.target)) close(false); }
  function escape(e) {
    if (opened && e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); }
  }
  function open(focus=true, query=null) {
    if (locked || !catalog || destroyed) return;
    if (query !== null) search.value = query;
    if (!opened) {
      opened = true; panel.hidden = false; document.body.append(panel);
      toggle.setAttribute('aria-expanded', 'true'); toggle.textContent = '›';
      document.addEventListener('pointerdown', outside);
      document.addEventListener('keydown', escape, true);
      window.addEventListener('resize', position);
      window.addEventListener('scroll', position, true);
    }
    renderOptions(); position(); if (focus) search.focus();
  }
  function close(focus=false) {
    if (!opened) return;
    opened = false; panel.hidden = true; root.append(panel);
    toggle.setAttribute('aria-expanded', 'false'); toggle.textContent = '‹';
    document.removeEventListener('pointerdown', outside);
    document.removeEventListener('keydown', escape, true);
    window.removeEventListener('resize', position);
    window.removeEventListener('scroll', position, true);
    if (focus) toggle.focus();
  }
  function prepare(text) {
    if (locked || destroyed) return null;
    if (!catalog) return {text, selection:[], revision};
    let rest = text.trim(), match;
    const next = new Set(selected);
    while ((match = rest.match(/^\/([a-z][a-z0-9_-]*)(?=\s|$)/))) {
      const choice = byCommand.get(match[1]);
      if (!choice) { message.textContent = `Unknown or unavailable command /${match[1]}. Choose a permitted tool from the menu.`; return null; }
      next.add(choice.id); rest = rest.slice(match[0].length).trimStart();
    }
    if (!fits(next)) { message.textContent = 'Choose fewer groups or individual tools to stay within the tool budget.'; return null; }
    if ([...next].join() !== [...selected].join()) {
      selected.clear(); next.forEach(v=>selected.add(v)); revision++; renderChips();
      message.textContent = 'Selected for the next message only. Nothing has run.';
    }
    close(false);
    return {text:rest, selection:[...selected], revision};
  }
  function typing() {
    if (locked || !catalog) return;
    const match = input.value.match(/^\/([a-z][a-z0-9_-]*)(\s)/);
    if (match && byCommand.has(match[1])) {
      const ready = prepare(input.value); if (ready) input.value = ready.text;
    } else if (/^\/[a-z0-9_-]*$/.test(input.value)) open(false, input.value.slice(1));
    else if (opened && document.activeElement === input) close(false);
  }
  function inputKeys(e) {
    if (e.key === 'ArrowDown' && (opened || input.value.startsWith('/'))) { e.preventDefault(); open(true); }
  }
  toggle.addEventListener('click', ()=>opened ? close(true) : open());
  search.addEventListener('input', ()=>renderOptions());
  panel.addEventListener('keydown', e=>{
    if (!['ArrowDown','ArrowUp','Home','End'].includes(e.key)) return;
    const buttons = [...list.querySelectorAll('button')]; if (!buttons.length) return;
    e.preventDefault();
    const index = buttons.indexOf(document.activeElement);
    const next = e.key==='Home' ? 0 : e.key==='End' ? buttons.length-1 :
      e.key==='ArrowDown' ? (index+1)%buttons.length : (index<=0 ? buttons.length-1 : index-1);
    buttons[next].focus();
  });
  input.addEventListener('input', typing); input.addEventListener('keydown', inputKeys);
  if (!catalog) message.textContent = 'Tool choices are temporarily unavailable. You can still send a message with the default tools.';
  renderChips();
  return {root, chips, message, prepare,
    updateCatalog(value) {
      close(); catalog=value; choices=catalogChoices(value);
      byCommand=new Map(choices.map(c=>[c.command,c])); byId=new Map(choices.map(c=>[c.id,c]));
      selected.clear(); revision++; renderChips(); explain(); toggle.disabled=locked||!catalog;
      message.textContent=catalog ? 'Tool choices reset for this setup; they apply to the next message only.' : 'Tool choices are temporarily unavailable.';
    },
    sent(snapshot) { if (revision===snapshot.revision) { selected.clear(); revision++; renderChips(); message.textContent=''; } },
    setBusy(value) { locked=value; toggle.disabled=locked||!catalog; if (locked) close(); renderChips(); },
    destroy() { destroyed=true; close(); panel.remove(); input.removeEventListener('input',typing); input.removeEventListener('keydown',inputKeys); }
  };
}
