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
      target.querySelectorAll('input[type="password"],input[aria-label="Site username"]').forEach(input=>{input.value='';});
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
      const drawGeneration=generation;
      const current=()=>alive()&&generation===drawGeneration;
      const c=source.classification, req=source.request;
      const proposal=source.adapters.find(a=>a.revision===source.proposed_revision);
      const status=h('p',{role:'status'},source.status.replaceAll('_',' '));
      mount(content,h('h3',null,source.title),h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.url),status,
        note('Local adapters read public pages only. Connect this site for sign-in; JavaScript-only pages may need a connected Agent. Adapter approval stays separate.'),
        c ? h('div',null,note(`${c.roles.join(' · ')} · ${Math.round(c.confidence*100)}% confidence`),h('p',null,c.summary||''),note(`Read ${c.read_at} · ${c.provided_by}`)) : note('No successful classification saved.'),
        source.error ? h('p',{role:'alert'},source.error):null);
      if (source.analysis_running || (req && ['open','claimed'].includes(req.status))) {
        content.append(note(!req ? 'Finishing the previous attempt.' : req.claimed_by==='Canvas Hub built-in Assistant' ? `Request #${req.id} · Assistant is reading and analysing. Cancel discards the result; a model request already sent may still use API quota.` : `Request #${req.id} · ${req.status}. Connect an MCP Agent with --allow-fulfil and ask it to claim this request and submit_source_analysis.`),
          h('a',{href:'#/requests'},'Connected Agent requests'),['open','claimed'].includes(req?.status)?h('button',{onclick:async()=>{
            await api(aidPath.replace(/external-sources\/\d+$/,`requests/${req.id}/cancel`),{method:'POST'}); source=await api(aidPath); disclosure=null; await draw(); if(alive())window.dispatchEvent(new Event('external-sources-updated'));
          }},'Cancel analysis'):note('Finishing the previous attempt; please wait before re-inspecting.'));
        const token=++generation;
        setTimeout(async()=>{ if(token!==generation||!alive())return; try{source=await api(aidPath);await draw();if(!source.analysis_running&&!['open','claimed'].includes(source.request?.status))window.dispatchEvent(new Event('external-sources-updated'));}catch(e){status.textContent=e.message;} },1200);
      } else {
        const checkResult=h('div',{role:'status'});
        let probePoll=0;
        function showProbe(result) {
          if(!current())return;
          if(result.failure_id&&!result.manual&&(req?.status!=='declined'||req?.id!==result.failure_id)) {mount(checkResult);return;}
          if(result.state==='not_checked')return;
          if(result.state==='running') {mount(checkResult,note('Checking whether sign-in is needed. This anonymous check uses no saved session or model call.'));return;}
          const message={
            login_required:result.auth_scheme==='basic'?'This site requires HTTP Basic authentication. Use the local connection form to enter your credentials.':'This page shows an authentication challenge. Sign in, then check the requested course page.',
            access_denied:'The site denied access. This does not establish that login is required; check your permissions or connect manually.',
            readable:'The anonymous page is readable. The failed inspection may have another cause; course access is still unverified.',
            unknown:'Login requirement is unknown. A chooser, security challenge or incomplete page is not proof of a login requirement.',
            unsupported_unavailable:result.evidence?.includes('timeout')?'The anonymous check timed out. Login requirement is unknown.':result.evidence?.includes('probe_busy')?'Login checks are busy. Retry the check or connect manually.':'The anonymous check could not read this site. Login requirement is unknown.'
          }[result.outcome]||'Login requirement is unknown.';
          mount(checkResult,note(message),result.final_host?note(`Checked host: ${result.final_host}. Course access after login is not verified.`):null,
            h('button',{class:result.outcome==='login_required'?'primary':'',onclick:()=>{
              connectBox.scrollIntoView({block:'center'});connectBox.querySelector('button')?.focus();
            }},result.outcome==='login_required'?'Sign in to this site':'Connect manually'));
        }
        async function followProbe() {
          const poll=++probePoll;
          for(let i=0;i<30&&current()&&poll===probePoll;i++) {
            const result=await api(aidPath+'/login-check');
            if(!current()||poll!==probePoll)return;
            showProbe(result);
            if(result.state!=='running')return;
            await new Promise(resolve=>setTimeout(resolve,1000));
          }
        }
        content.append(note('Failed inspections automatically check for login anonymously. Saved sessions and model calls are never used for this check.'),
          h('button',{onclick:async e=>{
            const button=e.currentTarget; button.disabled=true;
            try {
              await api(aidPath+'/login-check',{method:'POST',body:{}});
              await followProbe();
            } catch(err) { if(current())checkResult.textContent=err.message; }
            finally { if(current())button.disabled=false; }
          }},'Check login requirement'),checkResult);
        const connectBox=h('section',{'aria-label':'Site connection'});
        content.append(connectBox);
        let connectionGeneration=0;
        async function connectionPanel() {
          const connectionVersion=++connectionGeneration;
          const cp=aidPath+'/site-connection';
          let state=await api(cp); if(!current()||connectionVersion!==connectionGeneration)return;
          const output=h('div',{'aria-live':'polite'});
          const button=(label,fn)=>h('button',{onclick:async e=>{
            const b=e.currentTarget; b.disabled=true;
            // A review opened by this action must stay visible until the user
            // acts again. In-flight status polls may not remount it away.
            connectionGeneration++;
            try { await fn(); } catch(err) { if(alive())output.append(h('p',{role:'alert'},err.message)); }
            finally { if(alive())b.disabled=false; }
          }},label);
          if(state.state==='not_supported') {
            const secure=new URL(source.url);secure.protocol='https:';
            mount(connectBox,note(state.message),note('To connect, first review the separate HTTPS version. Its availability has not been verified; credentials are never sent over HTTP.'),
              h('a',{href:secure.href,target:'_blank',rel:'noopener noreferrer'},secure.href),
              button('Use HTTPS version',async()=>{
                const upgraded=await api(aidPath+'/https-source',{method:'POST',body:{consent:true}});
                if(!current())return;
                window.dispatchEvent(new Event('external-sources-updated'));
                await show(main,upgraded);
              }),output);return;
          }
          mount(connectBox,h('h3',null,'Connect this site'),
            note('Use a separate local browser profile for this account and site. Enter credentials and MFA in its window; HTTP Basic authentication uses a local form below. No page text or session is sent to the Assistant.'),
            note('Only the site and identity-provider origins you approve may receive requests. Login form submissions require a separate one-time approval.'),
            note(`Connection: ${state.state.replaceAll('_',' ')}`),output);
          const failure={
            browser_unavailable:'The sign-in browser could not open. Check that Chromium or your configured Chrome installation is available and that a desktop window can open.',
            browser_closed:'The sign-in browser closed unexpectedly. Reopen the connection to retry.',
            request_failed:'The requested page could not be reached safely. Check the address and connection; this does not establish that login is required.',
            access_denied:'The site denied access. Login or course permission has not been verified.',
            page_unavailable:'The site returned an unsuccessful response. Retry later; login requirement is unknown.'
          }[state.failure_reason];
          if(failure)connectBox.append(note(failure));
          if(['not_connected','closed','error'].includes(state.state)) {
            const ids=h('input',{type:'text',placeholder:'https://identity.example.edu', 'aria-label':'Additional identity-provider origins'});
            connectBox.append(note('Optional: enter identity-provider HTTPS origins separated by commas. Unknown origins are blocked.'),ids,
              button('Review connection',async()=>{
                const origins=ids.value.split(',').map(s=>s.trim()).filter(Boolean);
                const offer=await api(cp+'/offer',{method:'POST',body:{identity_origins:origins}});
                if(!alive())return;
                mount(output,note(`Approve local sign-in for ${offer.site}. Network access will be limited to: ${offer.origins.join(', ')}. A persistent local browser profile will be created.`),
                  button('Agree and open sign-in window',async()=>{
                    await api(cp+'/start',{method:'POST',body:{token:offer.token,consent:true}}); if(alive())await connectionPanel();
                  }));
              }));
          } else {
            if(state.popup_blocked)connectBox.append(note('This site tried to open another sign-in popup. This connection supports sign-in in its main window only. Try a same-window sign-in option; popup-only SSO is not supported.'));
            if(state.http_auth?.scheme==='basic') {
              const username=h('input',{type:'text',autocomplete:'off','aria-label':'Site username'});
              const password=h('input',{type:'password',autocomplete:'off','aria-label':'Site password'});
              connectBox.append(note(`HTTP Basic sign-in for ${state.http_auth.origin}. Credentials are used only for this exact page over HTTPS, kept in memory until the connection closes, and excluded from Assistant requests and exports.`),
                username,password,button('Sign in to requested page',async()=>{
                  const request={challenge:state.http_auth,username:username.value,password:password.value};
                  username.value='';password.value='';
                  try {await api(cp+'/http_auth',{method:'POST',body:{request}});}
                  finally {request.username='';request.password='';}
                  if(current())await connectionPanel();
                }));
            } else if(state.http_auth)connectBox.append(note('This HTTP authentication scheme is not supported. Use an authorized browser or connected Agent; do not paste credentials into chat.'));
            if(state.redirect_blocked) {
              const reason={
                unapproved_origin:'The destination has not been approved. Review the blocked origin before reconnecting.',
                unsafe_target:'The destination could not be used safely.',
                post_replay:'The sign-in flow would repeat a submission. It cannot be continued automatically.',
                verification:'The requested page redirected instead of showing course content. Reopen sign-in, then check the requested page again.',
                loop:'The sign-in page redirected in a loop.',
                limit:'The sign-in page redirected too many times.',
                timeout:'The sign-in redirects took too long.',
                navigation_changed:'The sign-in destination changed unexpectedly. Reopen the sign-in page to try again.',
                subresource:'A page resource tried to redirect. This sign-in flow may not be supported.',
                request_failed:'The next sign-in page could not be reached safely.'
              }[state.redirect_reason]||'This redirect is not supported.';
              connectBox.append(note(`A sign-in redirect was stopped. ${reason} Saved source data is unchanged.`));
            }
            if(state.blocked_origins?.length)connectBox.append(note(`Blocked origins: ${state.blocked_origins.join(', ')}. Close and reopen after reviewing any identity-provider origin you need.`));
            if(state.blocked_submission_origin)connectBox.append(note(`A submission to ${state.blocked_submission_origin} was blocked because its purpose could not be verified. This sign-in flow may not be supported.`));
            if(state.pending_post)connectBox.append(note(`A sign-in submission to ${state.pending_post.origin} is waiting for approval. Approve once, then submit again in the site window.`),
              button('Approve one sign-in submission',async()=>{await api(cp+'/approve_post',{method:'POST',body:{request:state.pending_post}});if(alive())await connectionPanel();}));
            if(state.state==='connected')connectBox.append(button('Review connected source analysis',async()=>{
              const offer=await api(aidPath+'/connected-disclosure',{method:'POST',body:{}}); if(!alive())return;
              if(!offer.available){mount(output,note(offer.message));return;}
              mount(output,note(`The exact text below and the website origin will be sent to ${offer.setup||offer.provider} (${offer.endpoint}), model ${offer.model}; ${offer.usage_note||'this may use your quota.'} The reviewed text and classification will be saved locally and included in data backups.`),
                note('Forms, common credential patterns, links and email addresses are removed automatically, but no filter can recognize every secret. Review the entire text and do not approve if it contains credentials, tokens, or information you do not want sent.'),
                h('details',{open:true},h('summary',null,'Exact text to send and save'),h('pre',{class:'text'},offer.content)),
                button('Approve, analyse and save',()=>action('/analyze-connected',{token:offer.token,consent:true})));
            }));
            connectBox.append(button('Refresh connection status',connectionPanel),
              button('Reopen sign-in page',async()=>{await api(cp+'/retry_login',{method:'POST',body:{}});if(alive())await connectionPanel();}),
              button('Check requested page',async()=>{
                const result=await api(cp+'/verify',{method:'POST',body:{}}); if(!alive())return;
                mount(output,note(result.state==='content_visible'?'Content appeared at the exact requested address. Confirm it is your course page; this does not prove enrollment or send text to an Assistant.':`Could not verify the requested page: ${result.state.replaceAll('_',' ')}. Existing saved data is unchanged.`),
                  result.content?h('details',null,h('summary',null,'View retrieved page text'),h('pre',{class:'text'},result.content)):null,
                  result.review_token?button('Confirm this is the requested course page',async()=>{await api(cp+'/confirm_content',{method:'POST',body:{request:result.review_token}});if(alive())await connectionPanel();}):null);
              }),button('Close connection',async()=>{await api(cp+'/cancel',{method:'POST',body:{}});if(alive())await connectionPanel();}));
            // Read status without remounting unchanged credential fields or reviews.
            const seen=JSON.stringify(state);
            const poll=async()=>{
              if(!current()||connectionVersion!==connectionGeneration)return;
              try {
                const next=await api(cp);
                if(!current()||connectionVersion!==connectionGeneration)return;
                if(JSON.stringify(next)!==seen) {await connectionPanel();return;}
                setTimeout(poll,1200);
              } catch(err) {if(current())output.append(note(err.message));}
            };
            setTimeout(poll,1200);
          }
        }
        await connectionPanel(); if(!alive())return;
        followProbe().catch(err=>{if(current())checkResult.textContent=err.message;});
        if(req?.status==='declined') content.append(note('Latest inspection failed. Previous successful results are retained; you can re-inspect after reviewing the disclosure.'));
        if(req?.result_note) content.append(note(req.result_note));
        if (!disclosure) disclosure=await api(aidPath+'/disclosure');
        if(!alive())return;
        const repeat=!!req||!!c||source.decision!=='undecided';
        if(disclosure.available) {
          content.append(note(`Read only this public URL: ${disclosure.urls.join(', ')}. Its page text will be sent to ${disclosure.setup || disclosure.provider} (${disclosure.endpoint}), model ${disclosure.model}. ${disclosure.usage_note||(disclosure.provider === 'codex' ? 'This may use your Codex quota.' : 'This uses your API quota.')} No Canvas cookies or login are used.`),
            h('button',{class:'primary',onclick:()=>action('/analyze',{token:disclosure.token,consent:true,reanalyze:repeat})},repeat?'Approve and re-inspect':'Approve and analyse'));
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
      const failed=s.request?.status==='declined';
      const fresh=!s.analysis_request_id&&!s.classification&&s.decision==='undecided'&&s.status==='unclassified';
      if((!fresh&&!failed)||s.analysis_running||s.access_needed||seenURLs.has(s.url))return false;
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
      list.append(h('div',{class:'external-source-row'},h('div',{class:'external-source-info'},h('a',{href:source.url,target:'_blank',rel:'noopener noreferrer'},source.title),
        note(`${source.status.replaceAll('_',' ')}${source.request?.status==='declined'?' · latest inspection failed':''}${source.classification?' · '+source.classification.roles.join(', '):''}${source.last_success_at?' · read '+source.last_success_at:''}`)),
        h('div',{class:'external-source-actions'},
          source.analysis_request_id && !source.analysis_running && !['open','claimed'].includes(source.request?.status) ? h('button',{onclick:()=>show(main,source)},'Re-inspect') : null,
          launcher([{id:'analyze',icon:'message',label:source.classification||source.decision!=='undecided'?'Review source':'Analyse link',
            description:'Review the public URL and Assistant API usage before analysis; cached decisions are reused.',run:()=>{dismiss();return show(main,source);} }]))));
    }
    const host=main.querySelector('.workspace-content')||main;
    host.querySelector('.external-source-list')?.remove(); main.querySelector('.external-source-nudge')?.remove(); host.append(list);
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
