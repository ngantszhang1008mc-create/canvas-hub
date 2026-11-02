// Context is captured once per pane/run. Navigation cannot retarget a request.
export function makeContextAgent(env) {
  const {h, mount, api, accountId, renderMessages, guard, toast} = env;
  const running = new WeakSet(), versions = new WeakMap();
  async function pane(scope, target, initial = null) {
    const token = {}; versions.set(target,token);
    const alive = () => target.isConnected && versions.get(target) === token;
    const aid = accountId();
    const base = `/api/accounts/${aid}`;
    const cfg = await api('/api/assistant/config');
    if (!alive()) return;
    if (!cfg.configured) {
      mount(target, h('p', null, 'Set up an Agent provider before using this action.'),
        h('a', {href:'#/assistant/settings', class:'btn'}, 'Provider settings'));
      return;
    }
    const profiles = cfg.profiles.filter(p => p.configured);
    const setup = h('select', {'aria-label':'Agent setup'}, profiles.map(p => h('option', {value:p.id,selected:p.id===cfg.active_profile}, p.name)));
    const providerLabel = h('p', {class:'tiny muted'});
    const updateLabel = () => {const p = profiles.find(p => p.id === setup.value); providerLabel.textContent = `${p?.preset || 'Provider'} · ${p?.model || 'Codex configured default'} · ${scope.item_id ? 'This assignment' : 'This course'}`;};
    setup.addEventListener('change',updateLabel); updateLabel();
    const log = h('div', {class:'context-agent-log', 'aria-live':'polite'});
    const status = h('div', {class:'small', role:'status'});
    const review = h('div');
    const input = h('textarea', {rows:3,'aria-label':'Ask contextual Agent',placeholder:'Ask about this work…'});
    const send = h('button',{class:'primary',onclick:()=>dispatch('ask',input.value)},'Send');
    const stop = h('button',{class:'danger',hidden:true,onclick:async()=>{if(conv) await guard(()=>api(`${base}/assistant/conversations/${conv}/stop`,{method:'POST'}));}},'Stop');
    let conv = null, polling = false, ended = false;
    mount(target,h('div',{class:'context-agent-controls'},setup,h('a',{href:'#/assistant/settings',class:'tiny'},'Settings')),providerLabel,
      h('p',{class:'tiny muted'},'Messages and retrieved sources go to this setup. Provider charges may apply.'),log,status,review,input,h('div',{class:'row'},send,stop));
    const current = await api(`${base}/workspace/conversation`,{method:'POST',body:scope});
    conv = current.id;
    async function refresh() {
      if (!alive() || ended) return;
      const result = await api(`${base}/assistant/conversations/${conv}`);
      if (!alive()) return;
      renderMessages(log,result.messages,{send:t=>dispatch('ask',t)});
      if(result.draft?.text) log.append(h('div',{class:'msg assistant live'}, result.draft.text));
      const busy = ['running','cancelling'].includes(result.conversation.run_state);
      send.disabled = busy; setup.disabled = busy; stop.hidden = !busy;
      status.textContent = busy ? (result.conversation.run_state==='cancelling' ? 'Stopping…' : 'Working…') : result.conversation.run_error || '';
      if (!busy && polling) { polling=false; env.onUpdated?.(); }
      if (busy) {polling=true; setTimeout(()=>refresh().catch(e=>{status.textContent=e.message;}),400);}
    }
    async function dispatch(action, text='') {
      if (running.has(target) || (action === 'ask' && !text.trim())) return;
      running.add(target); send.disabled=true;
      const request = {...scope,action,text,profile_id:setup.value, request_id:globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`};
      try {
        const prepared = await api(`${base}/workspace/action/prepare`,{method:'POST',body:request});
        if (!alive()) return;
        async function start(acknowledge=false) {
          if(!alive())return;
          mount(review);
          const response = await api(`${base}/workspace/action/start`,{method:'POST',body:{...request,disclosure_fingerprint:prepared.disclosure.fingerprint,acknowledge}});
          conv=response.conversation_id; input.value=''; polling=true; await refresh();
        }
        if (prepared.disclosure.acknowledged) await start();
        else mount(review,h('div',{class:'card agent-disclosure'},h('strong',null,'Use this Agent setup?'),
          h('p',{class:'small'},prepared.disclosure.message || 'Review the setup before starting.'),
          h('dl',{class:'small'},h('dt',null,'Setup'),h('dd',null,prepared.disclosure.profile_name),
            h('dt',null,'Model'),h('dd',null,prepared.disclosure.model || 'Codex configured default'),
            h('dt',null,'Endpoint'),h('dd',null,prepared.disclosure.endpoint || prepared.disclosure.provider),
            h('dt',null,'Sources'),h('dd',null,prepared.context?.sources?.find(s=>s.title)?.title || (scope.item_id ? 'This assignment and its stored attachments' : 'This course and its stored syllabus'))),
          h('p',{class:'tiny muted'},'Stored sources may be incomplete. This action sends your request and relevant course data to the selected provider.'),
          h('div',{class:'row'},h('button',{class:'primary',onclick:()=>guard(async()=>{send.disabled=true;await start(true);})},'Allow and start'),
            h('button',{onclick:()=>{mount(review);send.disabled=false;}},'Cancel'))));
      } catch(e) {toast(e.message,true);status.textContent=e.message;}
      finally {running.delete(target); if(!polling)send.disabled=false;}
    }
    await refresh();
    if (initial) await dispatch(initial);
    input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();dispatch('ask',input.value);}});
    return {dispatch,close:()=>{ended=true;}};
  }
  return {open:(scope,target)=>guard(()=>pane(scope,target)),start:(scope,action,target)=>guard(()=>pane(scope,target,action))};
}
