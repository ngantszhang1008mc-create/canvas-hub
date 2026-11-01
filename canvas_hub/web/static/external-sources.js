// A contextual source list and side Agent panel. No analysis happens while rendering.
export function makeExternalSources(env) {
  const { h, mount, api, A, launcher } = env;
  const versions = new WeakMap(), bindings = new WeakMap();
  const note = (s) => h('p', {class:'small muted'}, s);
  function panelShell(main) {
    const shell = main.querySelector('.workspace-context');
    const host = shell || main;
    let panel = host.querySelector('.external-agent');
    if (!panel) {
      panel = h('aside', {class:'external-agent workspace-agent', 'aria-label':'Source Agent'});
      host.append(panel);
    }
    if (shell) { shell.classList.add('agent-open','external-open'); shell.querySelector('.workspace-agent:not(.external-agent)')?.setAttribute('hidden',''); }
    panel.hidden = false;
    return panel;
  }
  async function show(main, source, start=false) {
    const aidPath = A(`/external-sources/${source.id}`); // Capture account before asynchronous work.
    const target = panelShell(main);
    let generation = 0;
    const version = {}; versions.set(target,version);
    const alive = () => target.isConnected && !target.hidden && versions.get(target)===version;
    const close = h('button',{class:'workspace-icon-button',title:'Close source Agent','aria-label':'Close source Agent',onclick:()=>{
      generation++; versions.delete(target); target.hidden=true; main.querySelector('.workspace-context')?.classList.remove('agent-open','external-open');
    }},'×');
    const content = h('div',{class:'external-agent-content'});
    mount(target,h('div',{class:'workspace-agent-heading'},h('h2',null,'Source Agent'),close),content);
    async function action(path, body={}) {
      const buttons = [...content.querySelectorAll('button')]; buttons.forEach(b=>b.disabled=true);
      try { source=await api(aidPath+path,{method:'POST',body}); if(!alive())return; await draw(); window.dispatchEvent(new Event('external-sources-updated')); }
      catch(e) { if(!alive())return; content.append(h('p',{role:'alert'},e.message)); buttons.forEach(b=>b.disabled=false); }
    }
    async function draw() {
      if (!alive()) return;
      generation++;
      const c=source.classification, req=source.request;
      const proposal=source.adapters.find(a=>a.revision===source.proposed_revision);
      const status=h('p',{role:'status'},source.status.replaceAll('_',' '));
      mount(content,h('h3',null,source.title),h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.url),status,
        note('Local adapters read public pages only. Site login uses a connected Agent with its own authorized access. Custom code requires manual review.'),
        c ? h('div',null,note(`${c.roles.join(' · ')} · ${Math.round(c.confidence*100)}% confidence`),h('p',null,c.summary||''),note(`Read ${c.read_at} · ${c.provided_by}`)) : note('No successful classification saved.'),
        source.error ? h('p',{role:'alert'},source.error):null);
      if (req && ['open','claimed'].includes(req.status)) {
        content.append(note(`Request #${req.id} · ${req.status}. Connect an MCP Agent with --allow-fulfil. Ask it to list_view_requests, claim this request, read the page and submit_source_analysis.`),
          h('a',{href:'#/requests'},'Connected Agent requests'),h('button',{onclick:async()=>{
            await api(aidPath.replace(/external-sources\/\d+$/,`requests/${req.id}/cancel`),{method:'POST'}); source=await api(aidPath); await draw();
          }},'Cancel analysis'));
        const token=++generation;
        setTimeout(async()=>{ if(token!==generation||!alive())return; try{source=await api(aidPath);await draw();if(source.request?.status==='done')window.dispatchEvent(new Event('external-sources-updated'));}catch(e){status.textContent=e.message;} },1200);
      } else content.append(h('button',{onclick:()=>action('/analyze',{reanalyze:!!c||source.decision!=='undecided'})},c?'Reanalyse explicitly':'Analyse with connected Agent'));
      if(proposal && ['proposed','validated'].includes(proposal.status) && source.decision!=='declined') {
        content.append(h('section',{class:'external-proposal'},h('h3',null,'Approve this adapter?'),
          note(`Adapter ${source.adapter_id} · revision ${proposal.revision}`),
          h('p',null,'Approve the exact pages and fields below. Approval runs a public smoke read; it enables this revision only if every declared field passes.'),
          h('ul',null,proposal.spec.pages.map(url=>h('li',null,url))),
          note(`Read ${proposal.spec.format.toUpperCase()} · ${proposal.spec.records ? proposal.spec.records.kind+' fields: '+Object.keys(proposal.spec.records.fields).filter(k=>k!=='id').join(', ') : 'Page text only'}`),
          note(proposal.spec.limitations||'No additional site limitations recorded.'),
          h('details',null,h('summary',null,'Exact extraction settings'),h('pre',{class:'text'},JSON.stringify(proposal.spec,null,2))),note(`SHA-256 ${proposal.spec_hash}`),
          h('div',{class:'row'},h('button',{class:'primary',onclick:()=>action('/decision',{approve:true,revision:proposal.revision,spec_hash:proposal.spec_hash})},'Approve, test and enable'),
            h('button',{onclick:()=>action('/decision',{approve:false,revision:proposal.revision,spec_hash:proposal.spec_hash})},'Decline adapter'))));
      } else if(source.decision!=='declined') content.append(h('button',{onclick:()=>action('/decision',{approve:false})},source.enabled_revision?'Disable adapter':'Keep classification only'));
      if(source.enabled_revision && ['enabled','stale'].includes(source.status)) content.append(h('button',{onclick:()=>action('/refresh')},'Refresh without Agent'));
      if(source.last_success_at) content.append(note(`Last successful adapter read ${source.last_success_at}`));
      if(source.records.length) content.append(h('h3',null,'Stored observations'),h('ul',null,source.records.map(r=>h('li',null,h('a',{href:`#/item/${r.id}`},r.title)))));
      if(source.conflicts?.length) content.append(h('div',{role:'status'},h('h3',null,'Deadline conflicts'),source.conflicts.map(c=>h('p',null,`${c.title}: ${c.external_due} / ${c.other_due}. Both sources are retained; check the original evidence.`))));
      if(source.origins.length) content.append(h('details',null,h('summary',null,'Source context'),source.origins.map(o=>h('p',null,h('a',{href:`#/item/${o.id}`},o.title)))));
      if(source.reads.length) content.append(h('details',null,h('summary',null,'Read history'),source.reads.map(r=>h('div',null,note(`${r.read_at} · ${r.status} · revision ${r.revision||'Agent'}`),h('pre',{class:'text'},JSON.stringify(r.evidence.map(p=>({url:p.url,content:p.content?.slice(0,3000)})),null,2))))));
    }
    source=await api(aidPath);
    if(!alive())return;
    if(start) source=await api(aidPath+'/analyze',{method:'POST',body:{}});
    await draw();
    if(start)window.dispatchEvent(new Event('external-sources-updated'));
    if(matchMedia('(max-width:860px)').matches)target.scrollIntoView({block:'start'});
  }
  async function attach(main, scope) {
    const route=location.hash, accountPath=A('/external-sources');
    const previous=bindings.get(main);
    if(previous)window.removeEventListener('external-sources-updated',previous);
    const refresh=()=>{if(main.querySelector('.external-source-list')?.isConnected)attach(main,scope).catch(()=>{});};
    bindings.set(main,refresh); window.addEventListener('external-sources-updated',refresh);
    const path=accountPath+'?'+new URLSearchParams(scope);
    const result=await api(path);
    if(!main.isConnected||bindings.get(main)!==refresh||location.hash!==route||A('/external-sources')!==accountPath||!result.sources.length)return;
    const list=h('details',{class:'workspace-evidence external-source-list',open:true},h('summary',null,'External sources'));
    for(const source of result.sources) {
      list.append(h('div',{class:'external-source-row'},h('div',null,h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.title),
        note(`${source.status.replaceAll('_',' ')}${source.classification?' · '+source.classification.roles.join(', '):''}${source.last_success_at?' · read '+source.last_success_at:''}`)),
        launcher([{id:'analyze',icon:'message',label:source.classification||source.decision!=='undecided'?'Review source':'Analyse link',
          description:'Open the source Agent panel. A new unclassified link leaves one request for a connected Agent; cached decisions are reused.',run:()=>show(main,source,!source.classification&&source.decision==='undecided') }])));
    }
    const host=main.querySelector('.workspace-content')||main;
    host.querySelector('.external-source-list')?.remove(); host.append(list);
  }
  return {attach,show};
}
