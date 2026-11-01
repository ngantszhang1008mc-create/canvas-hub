// Placement UI only. Live component rendering and persistence stay in components.js.
export function makeWidgets(env) {
  const {h, mount, api, A, componentView, launcher, sourceStatus, ago} = env;
  const kindNames = {checklist:'Checklist', score_model:'Score calculator', grade_projector:'Grade projector', hours_tracker:'Hours tracker'};
  const configOf = w => ({mode:w.mode, rule:w.rule, expanded:w.expanded});
  const error = e => h('p', {class:'workspace-notice', role:'alert'}, e.message || String(e));
  const ruleText = r => !r ? 'No condition' : r.kind === 'date_window' ? `${r.reason} · ${r.start} – ${r.end} · ${r.time_zone}` :
    r.kind === 'event_today' ? `Event #${r.item_id} is today · ${r.time_zone} · source within ${r.max_age_days || 7} days` :
      `Active work matches ${r.item_id ? 'item #' + r.item_id : 'course ' + r.course_id}`;
  let openDialog = null, activeBoard = null;

  async function manager({componentId=null, board=null, onChange=null, base=A('/widgets')}={}) {
    if(!onChange && activeBoard?.root.isConnected && activeBoard.base===base)onChange=activeBoard.draw;
    if (openDialog) { openDialog.close(); openDialog.remove(); }
    board = board || await api(base+(componentId?`?component_id=${componentId}`:""));
    const returnFocus = document.activeElement?.closest('.workspace-launcher')?.querySelector('.workspace-launcher-toggle') || document.activeElement;
    const list = h('div', {class:'widget-library'});
    const status = h('div', {role:'status', 'aria-live':'polite'});
    const close = h('button', {class:'sm', 'aria-label':'Close widget library'}, 'Done');
    const zone = h('input', {value:board.time_zone || '', placeholder:'Asia/Hong_Kong', 'aria-label':'Default widget time zone'});
    const dialog = h('dialog', {class:'widget-dialog', 'aria-label':'Manage widgets'},
      h('div', {class:'spread'}, h('h2', null, 'Your widgets'), close),
      h('p', {class:'small muted'}, 'Pin useful tools, show them in context, or keep them here for later. Changes use the same saved tool everywhere.'),
      h('details', {class:'widget-zone'}, h('summary', null, 'Time zone'),
        h('p', {class:'tiny muted'}, 'Default for new rules. Existing rules keep their recorded time zone.'),
        h('div', {class:'row'}, zone, h('button', {class:'sm', onclick:()=>run(()=>api(base+'/time-zone',{method:'PUT',body:{time_zone:zone.value}}))}, 'Save time zone'))),
      status, list);
    const finish = () => { dialog.close(); dialog.remove(); if (openDialog === dialog) openDialog = null; if(returnFocus?.isConnected)returnFocus.focus(); else activeBoard?.root.parentElement?.querySelector('.overall-widget-toolbar .workspace-launcher-toggle')?.focus(); };
    close.addEventListener('click', finish);
    dialog.addEventListener('cancel', e=>{e.preventDefault();finish();});
    document.body.append(dialog); openDialog=dialog;
    let busy=false;
    async function run(fn, focusKey) {
      if(busy)return;
      busy=true; status.replaceChildren();
      const locks=[...dialog.querySelectorAll('button:not([aria-label="Close widget library"])')].map(b=>[b,b.disabled]);
      locks.forEach(([b])=>b.disabled=true);
      try { board=await fn(); onChange?.(board); draw(); status.textContent='Widget settings saved';
        if(focusKey) [...list.querySelectorAll('[data-focus]')].find(e=>e.dataset.focus===focusKey)?.focus();
      } catch(e) { status.replaceChildren(error(e),h('button',{class:'sm',onclick:()=>run(()=>api(base))},'Reload widget library')); }
      finally { locks.forEach(([b,disabled])=>{if(b.isConnected)b.disabled=disabled;}); busy=false; }
    }
    const save = (w, config, focusKey) => run(()=>api(base+'/'+encodeURIComponent(w.key),{method:'PUT',body:{config,expected_version:w.version}}),focusKey);
    function move(w, direction) {
      const keys=board.widgets.filter(x=>x.version).map(x=>x.key), index=keys.indexOf(w.key), target=index+direction;
      if(target<0||target>=keys.length)return;
      [keys[index],keys[target]]=[keys[target],keys[index]];
      run(()=>api(base+'/order',{method:'PUT',body:{keys,revision:board.revision}}),`${w.key}:${direction<0?'up':'down'}`);
    }
    function editor(w, row) {
      row.querySelector('.widget-rule-editor')?.remove();
      const r=w.rule || {}, label=(name,node)=>h('label', {class:'widget-field'},h('span',null,name),node);
      const type=h('select', {'aria-label':`Condition for ${w.title}`},
        h('option',{value:'date_window'},'Date window'),h('option',{value:'event_today'},'Stored event is today'),h('option',{value:'active_work'},'Active work'));
      type.value=r.kind || 'date_window';
      const fields=h('div',{class:'widget-rule-fields'}), msg=h('div',{class:'tiny muted'});
      let read;
      function show() {
        const z=h('input',{value:r.time_zone || board.time_zone || '',placeholder:'Asia/Hong_Kong','aria-label':'Rule time zone'});
        const iid=h('input',{type:'number',min:1,step:1,value:r.item_id || '',placeholder:'Stored item ID','aria-label':'Rule source item ID'});
        const age=h('input',{type:'number',min:1,max:90,value:r.max_age_days || 7,'aria-label':'Source freshness in days'});
        if(type.value==='date_window') {
          const start=h('input',{type:'date',value:r.start || ''}),end=h('input',{type:'date',value:r.end || ''}),reason=h('input',{value:r.reason || '',maxLength:300});
          mount(fields,label('From (inclusive)',start),label('Through (inclusive)',end),label('Why show this now?',reason),label('Time zone',z),label('Source item ID (optional)',iid),label('Source must be read within (days)',age));
          read=()=>({kind:'date_window',start:start.value,end:end.value,time_zone:z.value,reason:reason.value,...(iid.value?{item_id:Number(iid.value),max_age_days:Number(age.value)}:{})});
          msg.textContent='Use dates you chose or have source evidence for. A date window does not infer grade urgency.';
        } else if(type.value==='event_today') {
          const events=(board.event_sources || []).filter(e=>!w.component?.course_id || e.course_canvas_id===w.component.course_id);
          const event=h('select',{'aria-label':'Stored calendar event'},h('option',{value:''},'Choose a stored event'),events.map(e=>h('option',{value:e.id},`${e.title} · #${e.id}${e.presence!=='observed'?' · unavailable':''}`)));
          if(r.item_id&&!events.some(e=>e.id===r.item_id))event.append(h('option',{value:r.item_id},`Event #${r.item_id} · source unavailable`));
          event.value=r.item_id || '';
          mount(fields,label('Calendar event',event),label('Time zone',z),label('Source must be read within (days)',age));
          read=()=>({kind:'event_today',item_id:Number(event.value),time_zone:z.value,max_age_days:Number(age.value)});
          msg.textContent='Choose an event already in your course data. Its confirmed start and source freshness determine visibility. Your prepared checklist stays directly editable.';
        } else {
          const scope=h('select',{'aria-label':'Active work scope'},h('option',{value:'course'},'Course'),h('option',{value:'item'},'Assignment / event'));
          scope.value=r.item_id?'item':'course';
          const id=h('input',{value:r.course_id || r.item_id || w.component?.course_id || '', 'aria-label':'Active work source ID'});
          mount(fields,label('Match saved active work',scope),label('Course or item ID',id));
          read=()=>({kind:'active_work',...(scope.value==='item'?{item_id:Number(id.value)}:{course_id:id.value})});
          msg.textContent='Appears when you select matching work with “Keep working here”.';
        }
      }
      type.addEventListener('change',show); show();
      const form=h('form',{class:'widget-rule-editor',onsubmit:e=>{e.preventDefault();save(w,{...configOf(w),mode:'conditional',rule:read()},`${w.key}:configure`);}},
        label('Show when',type),msg,fields,h('div',{class:'row'},h('button',{type:'submit',class:'primary sm'},'Save condition'),h('button',{type:'button',class:'sm',onclick:()=>form.remove()},'Cancel')));
      row.append(form);type.focus();
    }
    function draw() {
      mount(list, board.widgets.map((w,index)=>{
        const focusKey=`${w.key}:mode`;
        const mode=h('select',{'aria-label':`Placement for ${w.title}`,dataset:{focus:focusKey}},
          h('option',{value:'pinned'},'Pinned'),...(w.component?[h('option',{value:'conditional'},'When relevant')]:[]),h('option',{value:'library'},'Library / hidden'));
        mode.value=w.mode;
        const row=h('section',{class:'widget-library-row',dataset:{widgetKey:w.key}},
          h('div',{class:'spread'},h('div',null,h('h3',null,w.title),h('p',{class:'tiny muted'},w.component?kindNames[w.component.kind]:'Built-in widget')),mode));
        mode.addEventListener('change',()=>{
          if(mode.value==='conditional'&&!w.rule){editor(w,row);mode.value=w.mode;}
          else save(w,{...configOf(w),mode:mode.value},focusKey);
        });
        const state=w.mode==='library'?'Hidden until you choose to show it':w.mode==='pinned'?'Always on Overall':`${w.condition.state==='unknown'?'Needs review':w.visible?'Showing now':'Waiting'} · ${w.condition.reason}`;
        row.append(h('p',{class:`widget-library-reason ${w.condition.state==='unknown'?'needs-attention':''}`},state));
        const actions=h('div',{class:'row'});
        if(w.component) actions.append(h('button',{class:'sm',dataset:{focus:`${w.key}:configure`},onclick:()=>editor(w,row)},'Set condition'),
          h('a',{class:'small',href:w.component.item_id?`#/item/${w.component.item_id}`:w.component.course_id?`#/course/${w.component.course_id}`:'#/trackers',onclick:finish},'Open saved tool'));
        if(w.version)actions.append(h('button',{class:'sm',disabled:index===0||null,'aria-label':`Move ${w.title} earlier`,dataset:{focus:`${w.key}:up`},onclick:()=>move(w,-1)},'↑'),
          h('button',{class:'sm',disabled:!board.widgets.slice(index+1).some(x=>x.version)||null,'aria-label':`Move ${w.title} later`,dataset:{focus:`${w.key}:down`},onclick:()=>move(w,1)},'↓'));
        row.append(actions);
        if(w.pending) {
          const p=w.pending;
          row.append(h('div',{class:'widget-proposal'},h('strong',null,'Agent suggestion · review before applying'),
            h('p',null,`${w.mode} → ${p.config.mode}${p.config.expanded?' · wide card':''}`),h('p',null,ruleText(p.config.rule)),
            h('div',{class:'row'},...[true,false].map(approve=>h('button',{class:'sm',onclick:()=>run(()=>api(base+'/'+encodeURIComponent(w.key)+'/review',{method:'POST',body:{proposal_id:p.id,approve}}))},approve?'Approve placement':'Dismiss suggestion')))));
        }
        return row;
      }));
    }
    draw();dialog.showModal();
    if(componentId) [...list.children].find(e=>e.dataset.widgetKey===`component:${componentId}`)?.scrollIntoView({block:'nearest'});
  }

  function boardView(initial, builtins) {
    let board=initial;
    const base=A('/widgets'), cards=new Map();
    const grid=h('div',{class:'overall-widget-grid'}),status=h('div',{role:'status'});
    const invitation=h('section',{class:'widget-invitation'},h('h2',null,'Make room for your tools'),
      h('p',null,'Bring a saved checklist or course calculator here. Tick, adjust and keep working, right on Overall.'),
      h('button',{class:'sm',onclick:()=>manage()},'Add a widget'));
    const contextStatus=h('span',{class:'small muted'});
    const root=h('div',{class:'overall-widgets'},status,grid);
    // Small grid tracks let short cards use the space beside a taller calculator.
    // DOM/tab order remains the saved order, with no dragging over form controls.
    const resize=new ResizeObserver(entries=>{
      if(!root.isConnected){resize.disconnect();return;}
      for(const entry of entries)entry.target.style.gridRowEnd=`span ${Math.ceil((entry.target.getBoundingClientRect().height+16)/24)}`;
    });
    const manage=()=>manager({board,onChange:draw,base});
    async function change(w,config) {
      status.replaceChildren();
      try {draw(await api(base+'/'+encodeURIComponent(w.key),{method:'PUT',body:{config,expected_version:w.version}}));}
      catch(e){status.replaceChildren(error(e));}
    }
    function create(w) {
      const inner=w.component?componentView({component:w.component.kind,view:w.component}):builtins[w.key];
      const label=h('span',{class:'widget-type'}),why=h('div',{class:'widget-why'}),actions=h('div');
      const card=h('section',{class:'overall-widget',dataset:{widgetKey:w.key},'aria-label':w.title},h('header',{class:'widget-heading'},label,actions),why,inner);
      if(w.component) {
        const source=sourceStatus(w.component);if(source)card.append(source);
        card.append(h('a',{class:'widget-origin',href:w.component.item_id?`#/item/${w.component.item_id}`:w.component.course_id?`#/course/${w.component.course_id}`:'#/trackers'},'Open saved tool →'));
      }
      return {card,inner,label,why,actions};
    }
    function draw(next=board) {
      board=next;
      const pending=board.widgets.filter(w=>w.pending).length, uncertain=board.widgets.filter(w=>w.mode==='conditional'&&w.condition.state==='unknown').length;
      contextStatus.textContent=[pending?`${pending} widget suggestion${pending===1?'':'s'} to review`:'',uncertain?`${uncertain} condition${uncertain===1?' needs':'s need'} review`:''].filter(Boolean).join(' · ') || 'Your tools, ready to use';
      const focused=document.activeElement;
      for(const w of board.widgets) {
        let cached=cards.get(w.key);
        if(!cached && w.component && !w.visible)continue;
        if(!cached){cached=create(w);cards.set(w.key,cached);}
        const {card,inner,label,why,actions}=cached;
        card.hidden=!w.visible || !w.component && inner?.hidden;
        card.classList.toggle('widget-wide',w.expanded);
        label.textContent=w.component?kindNames[w.component.kind]:'Workspace';
        mount(why,w.mode==='conditional'?h('span',null,w.condition.reason):h('span',null,'Pinned'),
          w.mode==='conditional'&&w.condition.source?h('a',{href:`#/item/${w.condition.source.item_id}`},`Source · read ${ago(w.condition.source.observed_at)}`):null);
        mount(actions,launcher([
          {label:`Manage ${w.title}`,icon:'sliders',description:'Pin, set a condition or move this widget in your library.',run:()=>manager({board,componentId:w.component?.id,onChange:draw,base})},
          ...(w.mode==='conditional'?[{label:`Pin ${w.title}`,icon:'home',description:'Keep this widget on Overall.',run:()=>change(w,{...configOf(w),mode:'pinned'})}]:[]),
          {label:`${w.expanded?'Narrow':'Widen'} ${w.title}`,icon:'maximize',description:'Change this card’s width. All controls remain available.',run:()=>change(w,{...configOf(w),expanded:!w.expanded})},
          {label:`Hide ${w.title}`,icon:'folder',description:'Keep in the library until you choose to show it again.',run:()=>change(w,{...configOf(w),mode:'library'})}
        ]));
        grid.append(card); // Reuse live controls, including any unsaved edits.
      }
      for(const [key,cached] of cards)if(!board.widgets.some(w=>w.key===key)){cached.card.remove();cards.delete(key);}
      const showInvite=!board.widgets.some(w=>w.component&&w.visible);
      invitation.hidden=!showInvite;grid.append(invitation);
      requestAnimationFrame(()=>{
        if(root.isConnected){for(const el of grid.children)resize.observe(el);}
      });
      if(focused?.isConnected)focused.focus({preventScroll:true});
    }
    activeBoard={base,root,draw};
    draw();
    const toolbar=h('div',{class:'overall-widget-toolbar'},contextStatus,
      launcher([{label:'Add widget',icon:'plus',description:'Bring an existing checklist, calculator or tracker onto Overall.',run:manage},
        {label:'Manage widgets',icon:'sliders',description:'Set placement, conditions, order and time zone. Review Agent suggestions.',run:manage}]));
    return {root,toolbar};
  }
  return {boardView,manager};
}
