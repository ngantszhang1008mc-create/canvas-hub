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
  async function show(main, source) {
    const aidPath = A(`/external-sources/${source.id}`); // Capture account before asynchronous work.
    const route = location.hash;
    const target = panelShell(main);
    let disclosure;
    let generation = 0;
    const version = {}; versions.set(target,version);
    const alive = () => target.isConnected && !target.hidden && versions.get(target)===version && location.hash===route && A(`/external-sources/${source.id}`)===aidPath;
    const close = h('button',{class:'workspace-icon-button',title:'Close source Agent','aria-label':'Close source Agent',onclick:()=>{
      generation++; versions.delete(target); target.hidden=true; main.querySelector('.workspace-context')?.classList.remove('agent-open','external-open');
    }},'×');
    const content = h('div',{class:'external-agent-content'});
    mount(target,h('div',{class:'workspace-agent-heading'},h('h2',null,'Source Agent'),close),content);
    async function action(path, body={}) {
      const buttons = [...content.querySelectorAll('button')]; buttons.forEach(b=>b.disabled=true);
      try { source=await api(aidPath+path,{method:'POST',body}); if(!alive())return; disclosure=null; await draw(); window.dispatchEvent(new Event('external-sources-updated')); }
      catch(e) { if(!alive())return; content.append(h('p',{role:'alert'},e.message)); buttons.forEach(b=>b.disabled=false); }
    }
    async function draw() {
      if (!alive()) return;
      generation++;
      const c=source.classification, req=source.request;
      const proposal=source.adapters.find(a=>a.revision===source.proposed_revision);
      const status=h('p',{role:'status'},source.status.replaceAll('_',' '));
      mount(content,h('h3',null,source.title),h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.url),status,
        note('Local adapters read public pages only. Login and JavaScript-only sites may need a connected Agent. Adapter approval stays separate.'),
        c ? h('div',null,note(`${c.roles.join(' · ')} · ${Math.round(c.confidence*100)}% confidence`),h('p',null,c.summary||''),note(`Read ${c.read_at} · ${c.provided_by}`)) : note('No successful classification saved.'),
        source.error ? h('p',{role:'alert'},source.error):null);
      if (req && ['open','claimed'].includes(req.status)) {
        content.append(note(req.claimed_by==='Canvas Hub built-in Assistant' ? `Request #${req.id} · Assistant is reading and analysing. Cancel discards the result; a model request already sent may still use API quota.` : `Request #${req.id} · ${req.status}. Connect an MCP Agent with --allow-fulfil and ask it to claim this request and submit_source_analysis.`),
          h('a',{href:'#/requests'},'Connected Agent requests'),h('button',{onclick:async()=>{
            await api(aidPath.replace(/external-sources\/\d+$/,`requests/${req.id}/cancel`),{method:'POST'}); source=await api(aidPath); disclosure=null; await draw(); if(alive())window.dispatchEvent(new Event('external-sources-updated'));
          }},'Cancel analysis'));
        const token=++generation;
        setTimeout(async()=>{ if(token!==generation||!alive())return; try{source=await api(aidPath);await draw();if(source.request?.status==='done')window.dispatchEvent(new Event('external-sources-updated'));}catch(e){status.textContent=e.message;} },1200);
      } else {
        if(req?.result_note) content.append(note(req.result_note));
        if (!disclosure) disclosure=await api(aidPath+'/disclosure');
        if(!alive())return;
        const repeat=!!req||!!c||source.decision!=='undecided';
        if(disclosure.available) {
          content.append(note(`Read only this public URL: ${disclosure.urls.join(', ')}. Its page text will be sent to ${disclosure.setup || disclosure.provider} (${disclosure.endpoint}), model ${disclosure.model}. This uses your API quota. No Canvas cookies or login are used.`),
            h('button',{class:'primary',onclick:()=>action('/analyze',{token:disclosure.token,consent:true,reanalyze:repeat})},repeat?'Approve and reanalyse':'Approve and analyse'));
        } else content.append(note(disclosure.message),h('a',{href:'#/assistant/settings'},'Open Assistant settings'));
        content.append(h('button',{onclick:()=>action('/analyze',{executor:'mcp',reanalyze:repeat})},'Use connected Agent instead'));
      }
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
    // Opening the panel only discloses. The approval button starts one paid attempt.
    await draw();

    if(matchMedia('(max-width:860px)').matches)target.scrollIntoView({block:'start'});
  }
  const dismissedMemory=new Set();
  function dismissed(key) {
    try { return new Set(JSON.parse(localStorage.getItem(key)||'[]')); }
    catch { return new Set(dismissedMemory); }
  }
  async function attach(main, scope) {
    const route=location.hash, accountPath=A('/external-sources'), batchPath=A('/external-analysis');
    const previous=bindings.get(main);
    if(previous)window.removeEventListener('external-sources-updated',previous);
    const refresh=()=>{if(main.querySelector('.external-source-list')?.isConnected)attach(main,scope).catch(()=>{});};
    bindings.set(main,refresh); window.addEventListener('external-sources-updated',refresh);
    const query=new URLSearchParams(scope).toString();
    const valid=()=>main.isConnected&&bindings.get(main)===refresh&&location.hash===route&&A('/external-sources')===accountPath;
    const result=await api(accountPath+'?'+query);
    if(!valid()||!result.sources.length)return;
    const seenURLs=new Set();
    const eligible=result.sources.filter(s=>{
      if(s.analysis_request_id||s.classification||s.decision!=='undecided'||s.access_needed||s.status!=='unclassified'||seenURLs.has(s.url))return false;
      seenURLs.add(s.url);return true;
    });
    const scopeLabel=scope.item_id ? 'this item' : 'this course';
    const list=h('details',{class:'workspace-evidence external-source-list',open:true});
    const batchArea=h('section',{class:'external-batch','aria-label':'Batch source analysis'});
    const key='canvas-source-dismissed:'+accountPath;
    let toast, polling=0;
    function dismiss() {
      const ids=dismissed(key); eligible.forEach(s=>{ids.add(s.id);dismissedMemory.add(s.id);});
      try { localStorage.setItem(key,JSON.stringify([...ids])); } catch {}
      toast?.remove();
    }
    async function progress(batch) {
      if(!valid())return;
      const run=++polling;
      mount(batchArea,h('h3',null,`Analysis · ${scopeLabel}`),
        h('p',{role:'status','aria-live':'polite'},`${batch.completed} / ${batch.total} completed · ${batch.failed} failed · ${batch.state}`),
        note('Only the approved links run, one at a time. Cancel stops queued links and discards in-flight results; API usage already incurred cannot be undone.'));
      all.disabled=batch.state==='running'||!eligible.length;
      if(batch.state==='running') {
        batchArea.append(h('button',{onclick:async(e)=>{
          e.currentTarget.disabled=true;
          try { const stopped=await api(batchPath+'/'+batch.token+'/cancel',{method:'POST'});await progress(stopped); if(valid())window.dispatchEvent(new Event('external-sources-updated')); }
          catch(error){if(valid())batchArea.append(h('p',{role:'alert'},error.message));}
        }},'Cancel batch'));
        setTimeout(async()=>{
          if(!valid()||run!==polling)return;
          try { const next=await api(batchPath+'/'+batch.token);await progress(next);
            if(next.state!=='running')window.dispatchEvent(new Event('external-sources-updated'));
          } catch(error) {if(valid())batchArea.append(h('p',{role:'alert'},error.message));}
        },1000);
      }
      batchArea.append(h('details',null,h('summary',null,'Link results'),batch.entries.map(e=>h('p',null,`${e.status} · ${e.url}`))));
    }
    async function review() {
      dismiss(); const reviewVersion=++polling;
      mount(batchArea,note('Preparing source disclosure…'));
      try {
        const d=await api(batchPath+'/disclosure?'+query);
        if(!valid()||reviewVersion!==polling)return;
        if(!d.available) {mount(batchArea,note(d.message),h('a',{href:'#/assistant/settings'},'Open Assistant settings'));return;}
        mount(batchArea,h('h3',{tabindex:'-1'},`Analyse ${d.count} links · ${scopeLabel}`),
          note(`These public pages will be read and their text sent to ${d.setup||d.provider} (${d.endpoint}), model ${d.model}. This uses your API quota. No Canvas cookies or login are used. Existing decisions and previous attempts are excluded.`),
          h('ul',null,d.urls.map(url=>h('li',null,url))),
          h('button',{class:'primary',onclick:async(e)=>{
            e.currentTarget.disabled=true;
            try {const batch=await api(batchPath+'/'+d.token+'/start',{method:'POST',body:{consent:true}});await progress(batch);}
            catch(error){if(valid())batchArea.append(h('p',{role:'alert'},error.message));}
          }},`Approve and analyse ${d.count} links`));
        batchArea.querySelector('h3').focus();
      } catch(error){if(valid())mount(batchArea,h('p',{role:'alert'},error.message));}
    }
    const all=h('button',{class:'primary external-analyse-all',disabled:!eligible.length,onclick:(e)=>{e.preventDefault();e.stopPropagation();list.open=true;review();}},`Analyse all (${eligible.length})`);
    list.append(h('summary',{class:'external-source-heading'},h('span',null,'External sources'),all),batchArea);
    for(const source of result.sources) {
      list.append(h('div',{class:'external-source-row'},h('div',null,h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.title),
        note(`${source.status.replaceAll('_',' ')}${source.classification?' · '+source.classification.roles.join(', '):''}${source.last_success_at?' · read '+source.last_success_at:''}`)),
        launcher([{id:'analyze',icon:'message',label:source.classification||source.decision!=='undecided'?'Review source':'Analyse link',
          description:'Review the public URL and Assistant API usage before analysis; cached decisions are reused.',run:()=>{dismiss();return show(main,source);} }])));
    }
    const host=main.querySelector('.workspace-content')||main;
    host.querySelector('.external-source-list')?.remove(); main.querySelector('.external-source-nudge')?.remove(); host.append(list);
    if(eligible.some(s=>!dismissed(key).has(s.id))) {
      toast=h('aside',{class:'external-source-nudge','aria-label':'Uninspected external links'},
        h('button',{class:'workspace-icon-button',title:'Dismiss link reminder','aria-label':'Dismiss link reminder',onclick:dismiss},'×'),
        h('p',{role:'status'},`${eligible.length} external links in ${scopeLabel} have not been inspected.`),
        h('div',{class:'row'},h('button',{class:'primary',onclick:review},`Analyse all (${eligible.length})`),
          h('button',{onclick:()=>{dismiss();list.open=true;all.focus();list.scrollIntoView({block:'center'});}},'View links')));
      main.append(toast);
    }
    try { const current=await api(batchPath+'/current?'+query);if(current&&valid()&&polling===0)await progress(current); }
    catch(error){if(valid())batchArea.append(note(error.message));}
  }
  return {attach,show};
}
