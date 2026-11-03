// One contextual sign-in step at a time. Opening this UI never signs in or calls a model.
export async function showSiteLogin(env) {
  const {h, mount, api, root, path, source, current, analyse, inspected, reopen} = env;
  const cp=path+'/site-connection', site=new URL(source.url).origin;
  const note=text=>h('p',{class:'small muted'},text);
  let epoch=0, busy=false, approvedPost=false;
  const changed=()=>window.dispatchEvent(new Event('external-sources-updated'));
  const clearInputs=()=>root.querySelectorAll('input[type="password"],input[name="site-username"]').forEach(el=>{el.value='';});
  const title=text=>h('h3',{tabindex:'-1'},text);
  function focusStep() {
    if(window.innerWidth<900)root.scrollIntoView({block:'start'});
    (root.querySelector('input[name="site-username"]')||root.querySelector('h3'))?.focus({preventScroll:true});
  }
  async function run(fn) {
    if(busy||!current())return;
    busy=true; epoch++; // Stop status polling while a user action or review is in progress.
    const controls=[...root.querySelectorAll('button,input')];controls.forEach(el=>el.disabled=true);
    try {await fn();}
    catch(e) {if(current())root.append(h('p',{role:'alert'},e.message));}
    finally {busy=false;controls.filter(el=>el.isConnected).forEach(el=>el.disabled=false);}
  }
  const button=(label,fn,primary=false)=>h('button',{type:'button',class:primary?'primary':'',onclick:()=>run(fn)},label);
  function details(state) {
    const box=h('details',{class:'site-login-details'},h('summary',null,'Connection details'),
      note(`Connection: ${state.state.replaceAll('_',' ')}`),note('Only approved HTTPS origins can receive requests. Saved source data stays unchanged if sign-in fails.'));
    const reason={unapproved_origin:'A sign-in destination needs your approval.',unsafe_target:'The destination could not be used safely.',
      post_replay:'The sign-in flow would repeat a submission. It cannot be continued automatically.',verification:'The requested page redirected instead of showing course content.',
      loop:'The sign-in page redirected in a loop.',limit:'The sign-in page redirected too many times.',timeout:'The sign-in redirects took too long.',
      navigation_changed:'The sign-in destination changed unexpectedly.',subresource:'A page resource tried to redirect; this flow may not be supported.',
      request_failed:'The next sign-in page could not be reached safely.'}[state.redirect_reason];
    if(reason)box.append(note(reason));
    if(state.blocked_origins?.length)box.append(note(`Blocked origins: ${state.blocked_origins.join(', ')}`));
    if(state.blocked_submission_origin)box.append(note(`A submission to ${state.blocked_submission_origin} could not be verified and was blocked.`));
    if(state.http_status)box.append(note(`HTTP status: ${state.http_status}`));
    box.append(button('Refresh connection status',async()=>render(await api(cp))));
    if(!['not_connected','closed','error','not_supported'].includes(state.state)) {
      box.append(button('Reopen sign-in page',async()=>{await api(cp+'/retry_login',{method:'POST',body:{}});if(current())await render(await api(cp));}),
        button('Close connection',async()=>{await api(cp+'/cancel',{method:'POST',body:{}});if(current()){await render(await api(cp));changed();}}));
    }
    if(state.method==='basic'||state.http_auth?.scheme==='basic')box.append(button('Use browser sign-in instead',()=>browserOffer([],true)));
    return box;
  }
  async function browserOffer(origins=[],restart=false) {
    const offer=await api(cp+'/offer',{method:'POST',body:{identity_origins:origins}});
    if(!current())return;
    epoch++;
    const ids=h('input',{type:'text',value:origins.join(', '),placeholder:'https://identity.example.edu','aria-label':'Additional identity-provider origins'});
    mount(root,title('Open the site’s sign-in window'),
      note('Sign in with your site account in a separate window. Canvas Hub creates an isolated local profile for this account and site.'),
      note(`Allow requests to: ${offer.origins.join(', ')}. Sign-in submissions require a separate one-time approval. No page text is sent to the Assistant at this step.`),
      button(restart?'Agree and reopen sign-in window':'Agree and open sign-in window',async()=>{
        if(restart)await api(cp+'/cancel',{method:'POST',body:{}});
        if(!current())return;
        await api(cp+'/start',{method:'POST',body:{token:offer.token,consent:true}});
        if(current()){await render(await api(cp));focusStep();}
      },true),
      h('details',{class:'site-login-details'},h('summary',null,'Additional sign-in domains'),
        note('Only add an identity-provider HTTPS origin you recognize. Unknown domains remain blocked.'),ids,
        button('Review sign-in domains',()=>browserOffer(ids.value.split(',').map(s=>s.trim()).filter(Boolean),restart))));
  }
  async function reviewAnalysis() {
    const offer=await api(path+'/connected-disclosure',{method:'POST',body:{}});
    if(!current())return;
    epoch++;
    if(!offer.available){mount(root,title('Analysis is not ready'),note(offer.message),h('a',{href:'#/assistant/settings'},'Open Assistant settings'));return;}
    mount(root,title('Review the text before analysis'),
      note(`This exact text and site origin will be sent to ${offer.setup||offer.provider} (${offer.endpoint}), model ${offer.model}. ${offer.usage_note||'This may use your quota.'} The text and classification will be saved locally, including in backups.`),
      note('Review all the text. Automatic filtering cannot recognize every secret; do not approve text containing credentials or information you do not want sent.'),
      h('details',{open:true},h('summary',null,'Exact text to send and save'),h('pre',{class:'text'},offer.content)),
      button('Approve, analyse and save',()=>analyse(offer.token),true));
    focusStep();
  }
  async function verify() {
    const result=await api(cp+'/verify',{method:'POST',body:{}});
    if(!current())return;
    epoch++;
    if(!result.review_token) {
      const message=result.state==='reconnect_needed'?'Sign-in was not accepted or has expired. Check your site credentials and try again.':
        result.state==='access_denied'?'The site denied access. Check your course permissions; this does not prove login is required.':
        'Could not confirm access to the requested course page. Complete sign-in in the site window, then try again.';
      await render(await api(cp),message);return;
    }
    mount(root,title('Confirm the course page'),note('The requested page is readable. Check that the text belongs to your course before continuing.'),
      h('pre',{class:'text site-login-preview'},result.content),
      button('Confirm this is the requested course page',async()=>{
        await api(cp+'/confirm_content',{method:'POST',body:{request:result.review_token}});
        if(current()){await render(await api(cp));changed();focusStep();}
      },true),h('details',{class:'site-login-details'},h('summary',null,'Not the right page?'),
        note('Saved course data has not changed.'),button('Return to sign-in',async()=>render(await api(cp)))));
    focusStep();
  }
  async function basicForm(state,notice) {
    const offer=await api(path+'/login-inspection');
    if(!current())return;
    if(!offer.available) {
      mount(root,title('Set up the Assistant before inspecting'),note(offer.message),
        h('a',{href:'#/assistant/settings'},'Open Assistant settings'),details(state));return;
    }
    const username=h('input',{type:'text',name:'site-username',autocomplete:'off',autocapitalize:'none',spellcheck:'false',required:true,maxlength:1024,'aria-label':'Site username'});
    const password=h('input',{type:'password',name:'site-password',autocomplete:'off',required:true,maxlength:4096,'aria-label':'Site password'});
    const form=h('form',{class:'site-login-form',autocomplete:'off',onsubmit:e=>{e.preventDefault();run(async()=>{
      // Credentials exist only in these local variables until the guarded POST.
      const request={username:username.value,password:password.value};clearInputs();
      let cancelled=false, finished=false;
      const active=()=>current()&&!cancelled;
      const progress=h('p',{role:'status','aria-live':'polite'},'Signing in…');
      const cancel=h('button',{type:'button',onclick:()=>stop().catch(e=>{if(current())progress.textContent=e.message;})},'Cancel sign-in and inspection');
      root.append(progress,cancel);
      async function stop() {
        if(cancelled||finished)return;
        cancelled=true;request.username='';request.password='';clearInputs();cancel.disabled=true;
        progress.textContent='Cancelling…';
        // Cancel the durable attempt before closing its connection. Work already
        // dispatched may have used quota; cancellation must not replay it.
        try {await api(path+'/login-inspection/cancel',{method:'POST',body:{token:offer.token}});}
        finally {
          try {await api(cp+'/cancel',{method:'POST',body:{}});} catch {}
          if(current()){await render(await api(cp),'Cancelled. Any inspection already sent may have used quota.');changed();}
        }
      }
      // Leaving this account/course/panel cancels a pending continuation. Do not
      // depend on the credential request finishing before noticing navigation.
      const watch=setInterval(()=>{if(!current())stop().catch(()=>{});},200);
      try {
        let next=await api(cp);
        if(!active())return;
        if(['not_connected','closed','error'].includes(next.state)) {
          const offer=await api(cp+'/offer',{method:'POST',body:{method:'basic',identity_origins:[]}});
          if(!active())return;
          next=await api(cp+'/start',{method:'POST',body:{token:offer.token,consent:true}});
        }
        for(let i=0;next.state==='opening'&&i<30&&active();i++) {
          await new Promise(resolve=>setTimeout(resolve,500));
          if(active())next=await api(cp);
        }
        if(!active())return;
        if(next.http_auth?.scheme!=='basic'||next.http_auth.origin!==site) {
          await render(next,'The requested page did not provide a Basic sign-in challenge. No credentials were sent. Review connection details before trying browser sign-in.');return;
        }
        request.challenge=next.http_auth;
        try {next=await api(cp+'/http_auth',{method:'POST',body:{request}});}
        finally {request.username='';request.password='';}
        if(!active())return;
        if(next.http_auth||next.failure_reason)await render(await api(cp),'Sign-in was not accepted. Check your site username, password and permissions, then try again.');
        else {
          progress.textContent='Checking access to the requested page…';
          const result=await api(path+'/login-inspection',{method:'POST',body:{token:offer.token,consent:true}});
          if(!active())return;
          finished=true;
          if(result.started)await inspected(result.source,'Sign-in succeeded. Inspection has started.');
          else await render(await api(cp),result.state==='cancelled'?'Inspection was cancelled.':'Inspection did not start; another request may already be active. Return to source review to check it.');
        }
      } catch(e) {
        if(active())await render(await api(cp),'Sign-in or inspection could not finish. '+e.message);
      } finally {clearInterval(watch);request.username='';request.password='';clearInputs();cancel.remove();progress.remove();}
    });}},
      h('label',null,'Username',username),h('label',null,'Password',password),
      h('button',{type:'submit',class:'primary'},'Sign in and inspect'));
    mount(root,title('Sign in to '+new URL(source.url).hostname),
      note('Enter your site account. Successful sign-in starts one inspection automatically.'),
      notice?h('p',{role:'alert'},notice):null,
      note(`Send this page’s text to ${offer.setup||offer.provider} (${offer.endpoint}), model ${offer.model}. ${offer.usage_note||'This uses your API or Codex quota.'}`),
      note('Text may include private course information. Sign in and inspect approves sending and saving it without a second review.'),form,
      note('Credentials stay in memory for this exact HTTPS page until you close the connection; they are not saved or sent to the Assistant.'),
      h('details',{class:'site-login-details'},h('summary',null,'Inspection scope and storage'),
        note(`Only ${offer.urls.join(', ')} is inspected. Linked pages are not opened. Filtered text and classification are saved locally and included in backups. Filtering cannot identify every secret.`)),details(state));
  }
  async function render(state,notice=null) {
    if(!current())return;
    const version=++epoch;
    clearInputs();
    if(state.state==='not_supported') {
      const secure=new URL(source.url);secure.protocol='https:';
      mount(root,title('Use a secure page to sign in'),note('Sign-in requires HTTPS. Review this separate address before continuing; its availability has not been verified.'),
        h('a',{href:secure.href,target:'_blank',rel:'noopener noreferrer'},secure.href),
        button('Use HTTPS version',async()=>{
          const next=await api(path+'/https-source',{method:'POST',body:{consent:true}});
          if(!current())return;
          const nextPath=path.replace(/external-sources\/\d+$/,`external-sources/${next.id}`);
          mount(root,title('Checking the HTTPS page'),h('p',{role:'status'},'Checking whether this separate address requires sign-in…'));
          let check=await api(nextPath+'/login-check',{method:'POST',body:{}});
          for(let i=0;check.state==='running'&&i<45&&current();i++) {
            await new Promise(resolve=>setTimeout(resolve,500));
            if(current())check=await api(nextPath+'/login-check');
          }
          if(current()){changed();await reopen(await api(nextPath));}
        },true));return;
    }
    const inactive=['not_connected','closed','error'].includes(state.state);
    const basic=state.http_auth?.scheme==='basic'||(inactive&&(state.method==='basic'||source.login_check?.auth_scheme==='basic'));
    if(basic)await basicForm(state,notice);
    else if(inactive) {
      await browserOffer();
      if(source.login_check?.outcome!=='login_required') {
        if(!current())return;
        root.querySelector('h3').textContent='Sign-in requirement is unknown';
        root.querySelector('h3').after(note('Could not confirm that login is required. Only try this manual sign-in window if you know the site needs it.'));
      }
      if(current()&&(notice||state.failure_reason))root.prepend(h('p',{role:'alert'},notice||'The sign-in window could not open or has closed. Check your browser installation and try again.'));
      return;
    } else {
      let heading='Sign in in the site window', instruction=approvedPost?'Submit the sign-in form again in the site window. Then check the course page.':'Complete sign-in and any MFA in the separate site window. Then check the course page.';
      let label='I’ve signed in — check page', next=verify;
      if(state.state==='opening'){heading='Opening sign-in';instruction='Wait for the isolated site connection to open.';label=null;}
      else if(state.state==='connected'){heading='Course page confirmed';instruction='You can now review the page text and Assistant usage before analysis.';label='Review connected source analysis';next=reviewAnalysis;}
      else if(state.http_auth){heading='Sign-in method is unsupported';instruction='This site requested an unsupported HTTP authentication method. No credentials were sent.';label=null;}
      else if(state.method==='basic'){heading='Check the requested course page';instruction='The local connection is open. Check whether this exact page is readable before continuing.';label='Check requested page';}
      else if(state.pending_post){heading='Allow the sign-in submission';instruction=`The sign-in form is waiting to submit to ${state.pending_post.origin}. Approve once, then submit it again in the site window.`;label='Approve one sign-in submission';next=async()=>{await api(cp+'/approve_post',{method:'POST',body:{request:state.pending_post}});approvedPost=true;if(current())await render(await api(cp));};}
      else if(state.blocked_origins?.length){heading='Review the sign-in destination';instruction='The site needs another domain. Only approve identity-provider domains you recognize; the window will reopen.';label='Review sign-in domains';next=()=>browserOffer([...new Set([...(state.origins||[]),...state.blocked_origins])].filter(x=>x!==site),true);}
      else if(state.popup_blocked){heading='This sign-in popup is unsupported';instruction='Try a sign-in option in the same site window, then check the page. Popup-only sign-in cannot be completed here.';}
      else if(state.redirect_blocked){heading='Sign-in could not continue safely';instruction='This redirect is unsupported. Check connection details; saved source data is unchanged.';label='Retry sign-in';next=async()=>{await api(cp+'/retry_login',{method:'POST',body:{}});if(current())await render(await api(cp));};}
      mount(root,title(heading),note(instruction),notice?h('p',{role:'alert'},notice):null,
        label?button(label,next,true):state.state==='opening'?h('p',{role:'status'},'Waiting for connection status…'):null,details(state));
    }
    const seen=JSON.stringify(state);
    // Never remount unchanged input fields, content confirmation or provider review.
    async function poll() {
      if(!current()||version!==epoch||busy)return;
      try {
        const next=await api(cp);
        if(!current()||version!==epoch||busy)return;
        if(JSON.stringify(next)!==seen){await render(next);return;}
        setTimeout(poll,1000);
      } catch(e) {if(current()&&version===epoch)root.append(h('p',{role:'alert'},e.message));}
    }
    if(!inactive)setTimeout(poll,1000);
  }
  await render(await api(cp));
  if(current())focusStep();
}
