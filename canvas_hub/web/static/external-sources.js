import {showSiteLogin} from './site-login.js';

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
  async function show(main, source, mode='review') {
    const aidPath = A(`/external-sources/${source.id}`); // Capture account before asynchronous work.
    const route = location.hash;
    const target = panelShell(main);
    let disclosure;
    let inspectionNotice;
    let generation = 0;
    const version = {}; versions.set(target,version);
    const alive = () => target.isConnected && !target.hidden && versions.get(target)===version && location.hash===route && A(`/external-sources/${source.id}`)===aidPath;
    const close = h('button',{class:'workspace-icon-button',title:'Close source Agent','aria-label':'Close source Agent',onclick:()=>{
      target.querySelectorAll('input[type="password"],input[aria-label="Site username"]').forEach(input=>{input.value='';});
      generation++; versions.delete(target); target.hidden=true; main.querySelector('.workspace-context')?.classList.remove('agent-open','external-open');
    }},'×');
    const content = h('div',{class:'external-agent-content'});
    mount(target,h('div',{class:'workspace-agent-heading'},h('h2',null,mode==='login'?'Site sign-in':'Source Agent'),close),content);
    async function action(path, body={}) {
      const buttons = [...content.querySelectorAll('button')]; buttons.forEach(b=>b.disabled=true);
      try { source=await api(aidPath+path,{method:'POST',body}); if(!alive())return; disclosure=null; mode='review'; await draw(); window.dispatchEvent(new Event('external-sources-updated')); }
      catch(e) { if(!alive())return; content.append(h('p',{role:'alert'},e.message)); buttons.forEach(b=>b.disabled=false); }
    }
    async function draw() {
      if (!alive()) return;
      generation++;
      const drawGeneration=generation;
      const current=()=>alive()&&generation===drawGeneration;
      target.querySelector('.workspace-agent-heading h2').textContent=mode==='login'?'Site sign-in':'Source Agent';
      const c=source.classification, req=source.request;
      const signedInSource=['connected_local_reviewed','connected_local_authorized'].includes(c?.access_mode);
      const proposal=source.adapters.find(a=>a.revision===source.proposed_revision);
      const status=h('p',{role:'status'},signedInSource&&source.status==='enabled'?'Inspection complete':source.status.replaceAll('_',' '));
      mount(content,h('h3',null,source.title),h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.url));
      if(source.inspection?.enabled===false) {
        target.querySelector('.workspace-agent-heading h2').textContent='Saved source';
        content.append(...[h('p',{role:'status'},source.inspection.message),
          c?h('section',null,h('h3',null,'Saved classification'),note(c.roles.join(' · ')),h('p',null,c.summary||'')):null,
          source.last_success_at?note(`Last saved read: ${source.last_success_at}`):null,
          source.records.length?h('section',null,h('h3',null,'Saved observations'),h('ul',null,source.records.map(r=>h('li',null,h('a',{href:`#/item/${r.id}`},r.title))))):null,
          source.reads.length?h('details',null,h('summary',null,'Saved source text'),source.reads.map(r=>h('div',null,note(r.read_at),r.evidence.map(p=>h('pre',{class:'text'},p.content||''))))):null].filter(Boolean));
        return;
      }
      if(mode==='login'&&!source.analysis_running&&!['open','claimed'].includes(req?.status)) {
        const box=h('section',{class:'site-login','aria-label':'Site connection'});
        content.append(box,h('button',{class:'site-login-back',onclick:()=>show(main,source)},'Back to source review'));
        await showSiteLogin({h,mount,api,root:box,path:aidPath,source,current,
          analyse:token=>action('/analyze-connected',{token,consent:true}),reopen:next=>show(main,next,'login'),
          inspected:async(next,notice)=>{if(!current())return;source=next;inspectionNotice=notice;mode='review';disclosure=null;await draw();window.dispatchEvent(new Event('external-sources-updated'));}});
        return;
      }
      content.append(inspectionNotice?h('p',{role:'status'},inspectionNotice):null,status,
        c ? h('div',null,note(`${c.roles.join(' · ')} · ${Math.round(c.confidence*100)}% confidence`),h('p',null,c.summary||'')) : null);
      if (source.analysis_running || (req && ['open','claimed'].includes(req.status))) {
        content.append(note(!req ? 'Finishing the previous attempt.' : req.claimed_by==='Canvas Hub built-in Assistant' ? `Request #${req.id} · Assistant is reading and analysing. Cancel discards the result; a model request already sent may still use API quota.` : `Request #${req.id} · ${req.status}. Connect an MCP Agent with --allow-fulfil and ask it to claim this request and submit_source_analysis.`),
          h('a',{href:'#/requests'},'Connected Agent requests'),['open','claimed'].includes(req?.status)?h('button',{onclick:async()=>{
            await api(aidPath.replace(/external-sources\/\d+$/,`requests/${req.id}/cancel`),{method:'POST'}); source=await api(aidPath); disclosure=null; await draw(); if(alive())window.dispatchEvent(new Event('external-sources-updated'));
          }},'Cancel analysis'):note('Finishing the previous attempt; please wait before re-inspecting.'));
        const token=++generation;
        setTimeout(async()=>{ if(token!==generation||!alive())return; try{source=await api(aidPath);await draw();if(!source.analysis_running&&!['open','claimed'].includes(source.request?.status))window.dispatchEvent(new Event('external-sources-updated'));}catch(e){status.textContent=e.message;} },1200);
      } else {
        const probe=source.login_check||{state:'not_checked'};
        const needsLogin=probe.outcome==='login_required';
        const advanced=h('details',{class:'external-source-diagnostics'},h('summary',null,'More options and diagnostics'));
        if(req?.status==='declined'||probe.state!=='not_checked') {
          const message=probe.state==='running'?'Checking whether sign-in is needed…':needsLogin?
            'This page requires sign-in. Continue below to connect this site.':probe.outcome==='access_denied'?
            'The site denied access. Could not determine whether sign-in would help.':probe.outcome==='readable'?
            (req?.status==='declined'?'The public page is readable. This analysis failed for another reason.':'The public page is readable without sign-in.'):
            'Could not determine whether this page needs sign-in. You can retry analysis or connect manually.';
          content.append(h('section',{'aria-label':'Inspection recovery'},h('h3',null,req?.status==='declined'?'Latest inspection failed':'Sign-in check'),note(message),
            needsLogin?h('button',{class:'primary',onclick:()=>show(main,source,'login')},'Needs login'):null));
          if(probe.state==='running')setTimeout(async()=>{
            if(!current())return;
            try {source=await api(aidPath);if(current()){await draw();window.dispatchEvent(new Event('external-sources-updated'));}}
            catch(e){if(current())status.textContent=e.message;}
          },1000);
        }
        if(source.error)advanced.append(note(source.error));
        if(req?.result_note)advanced.append(note(req.result_note));
        if(probe.final_host)advanced.append(note(`Checked host: ${probe.final_host}. Course access is not verified.`));
        advanced.append(note('Failed inspections check for sign-in anonymously, without saved sessions or a model call.'),
          h('button',{onclick:async e=>{
            e.currentTarget.disabled=true;
            try {await api(aidPath+'/login-check',{method:'POST',body:{}});source=await api(aidPath);if(current())await draw();}
            catch(err){if(current()){advanced.append(h('p',{role:'alert'},err.message));e.currentTarget.disabled=false;}}
          }},'Check login requirement'),
          h('button',{onclick:()=>show(main,source,'login')},needsLogin?'Open sign-in':'Connect manually'));
        if(signedInSource)content.append(note('Saved signed-in page. A new inspection needs a current connection and fresh approval.'),
          h('button',{onclick:()=>show(main,source,'login')},'Review signed-in source'));
        const publicReview=needsLogin||signedInSource?advanced:content;
        if (!disclosure) disclosure=await api(aidPath+'/disclosure');
        if(!alive())return;
        const repeat=!!req||!!c||source.decision!=='undecided';
        if(disclosure.available) {
          publicReview.append(note(`Read only this public URL: ${disclosure.urls.join(', ')}. Its page text will be sent to ${disclosure.setup || disclosure.provider} (${disclosure.endpoint}), model ${disclosure.model}. ${disclosure.usage_note||(disclosure.provider === 'codex' ? 'This may use your Codex quota.' : 'This uses your API quota.')} No Canvas cookies or login are used.`),
            h('button',{class:'primary',onclick:()=>action('/analyze',{token:disclosure.token,consent:true,reanalyze:repeat})},repeat?'Approve and re-inspect':'Approve and analyse'));
        } else publicReview.append(note(disclosure.message),h('a',{href:'#/assistant/settings'},'Open Assistant settings'));
        advanced.append(h('button',{onclick:()=>action('/analyze',{executor:'mcp',reanalyze:repeat})},'Use connected Agent instead'));
        content.append(advanced);
      }
      if(proposal && ['proposed','validated'].includes(proposal.status) && source.decision!=='declined') {
        content.append(h('section',{class:'external-proposal'},h('h3',null,'Approve this adapter?'),
          note('Local adapters read public pages only. Site sign-in and adapter approval stay separate.'),
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
      if(source.last_success_at) content.append(note(`Last successful ${signedInSource?'inspection':'adapter read'} ${source.last_success_at}`));
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
    const refresh=()=>{if(valid()&&main.querySelector('.external-source-list')?.isConnected)attach(main,scope).catch(()=>{});};
    bindings.set(main,refresh); window.addEventListener('external-sources-updated',refresh);
    const query=new URLSearchParams(scope).toString();
    const valid=()=>main.isConnected&&bindings.get(main)===refresh&&location.hash===route&&A('/external-sources')===accountPath;
    const result=await api(accountPath+'?'+query);
    if(!valid()||!result.sources.length)return;
    if(result.inspection?.enabled===false) {
      const list=h('details',{class:'workspace-evidence external-source-list',open:true},
        h('summary',{class:'external-source-heading'},'External sources'),
        h('p',{class:'external-inspection-paused',role:'status'},result.inspection.message));
      for(const source of result.sources)list.append(h('div',{class:'external-source-row'},
        h('div',{class:'external-source-info'},h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.title),
          note(source.classification?'Saved · '+source.classification.roles.join(', '):'Saved link')),
        h('button',{onclick:()=>show(main,source)},'View saved source')));
      const host=main.querySelector('.workspace-content')||main;
      host.querySelector('.external-source-list')?.remove();main.querySelector('.external-source-nudge')?.remove();host.append(list);
      return;
    }
    const seenURLs=new Set();
    const eligible=result.sources.filter(s=>{
      const failed=s.request?.status==='declined';
      const fresh=!s.analysis_request_id&&!s.classification&&s.decision==='undecided'&&s.status==='unclassified';
      if((!fresh&&!failed)||s.analysis_running||s.access_needed||s.login_check?.outcome==='login_required'||seenURLs.has(s.url))return false;
      seenURLs.add(s.url);return true;
    });
    const fresh=eligible.filter(s=>!s.analysis_request_id);
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
        batch.state==='running'?note('Only approved links run, one at a time. Cancel stops queued links; API usage already incurred cannot be undone.'):
          batch.failed?note('Failed links are checked for sign-in automatically. Use Needs login beside the matching link when a login requirement is confirmed.'):null);
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
          note(`These public pages will be read and their text sent to ${d.setup||d.provider} (${d.endpoint}), model ${d.model}. ${d.usage_note||(d.provider === 'codex' ? 'This may use your Codex quota.' : 'This uses your API quota.')} No Canvas cookies or login are used. New: ${d.new_count}; previously failed and to be retried: ${d.retry_count}. Successful inspections are excluded.`),
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
      const failed=source.request?.status==='declined';
      const needsLogin=source.login_check?.outcome==='login_required'&&!source.analysis_running;
      const connected=source.connection_state==='connected';
      const recovery=needsLogin?(connected?'Course page readable — ready for analysis':'Needs login'):failed?(source.login_check?.state==='running'?'Checking sign-in requirement…':source.login_check?.outcome==='readable'?
        'Analysis failed · public page is readable':'Analysis failed · could not determine login requirement'):null;
      list.append(h('div',{class:'external-source-row'},h('div',{class:'external-source-info'},h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.title),
        h('p',{class:'small muted',role:failed?'status':null},recovery||`${source.status.replaceAll('_',' ')}${source.classification?' · '+source.classification.roles.join(', '):''}${source.last_success_at?' · read '+source.last_success_at:''}`)),
        h('div',{class:'external-source-actions'},
          needsLogin?h('button',{class:'primary',onclick:()=>show(main,source,'login')},connected?'Continue analysis':'Needs login'):
            source.analysis_request_id && !source.analysis_running && !['open','claimed'].includes(source.request?.status) ? h('button',{onclick:()=>show(main,source)},failed?'Review failure':'Re-inspect') : null,
          launcher([{id:'analyze',icon:'message',label:source.classification||source.decision!=='undecided'?'Review source':'Analyse link',
            description:'Review the public URL and Assistant API usage before analysis; cached decisions are reused.',run:()=>{dismiss();return show(main,source);} }]))));
    }
    const host=main.querySelector('.workspace-content')||main;
    host.querySelector('.external-source-list')?.remove(); main.querySelector('.external-source-nudge')?.remove(); host.append(list);
    if(result.sources.some(s=>s.login_check?.state==='running'))setTimeout(()=>{if(valid())refresh();},1200);
    if(fresh.some(s=>!dismissed(key).has(s.id))) {
      toast=h('aside',{class:'external-source-nudge','aria-label':'Uninspected external links'},
        h('button',{class:'workspace-icon-button',title:'Dismiss link reminder','aria-label':'Dismiss link reminder',onclick:dismiss},'×'),
        h('p',{role:'status'},`${fresh.length} external links in ${scopeLabel} have not been inspected.`),
        h('div',{class:'row'},h('button',{class:'primary',onclick:review},`Analyse all (${eligible.length})`),
          h('button',{onclick:()=>{dismiss();list.open=true;all.focus();list.scrollIntoView({block:'center'});}},'View links')));
      main.append(toast);
    }
    try { const current=await api(batchPath+'/current?'+query);if(current&&valid()&&polling===0)await progress(current); }
    catch(error){if(valid())batchArea.append(note(error.message));}
  }
  return {attach,show};
}
