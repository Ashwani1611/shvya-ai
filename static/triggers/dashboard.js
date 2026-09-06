(() => {
  'use strict';
  const root = document.querySelector('#smart-triggers');
  if (!root) return;
  const $ = id => document.getElementById(id);
  document.body.classList.add('st-page');
  function mobileNav(open) {
    document.body.classList.toggle('st-nav-open', open);
    $('st-nav-backdrop').hidden = !open;
    $('st-mobile-nav').setAttribute('aria-expanded', String(open));
    if (open) document.querySelector('#app-sidebar a')?.focus();
  }
  $('st-mobile-nav').onclick = () => mobileNav(!document.body.classList.contains('st-nav-open'));
  $('st-nav-backdrop').onclick = () => { mobileNav(false); $('st-mobile-nav').focus(); };
  window.addEventListener('keydown', e => { if (e.key === 'Escape') mobileNav(false); });
  window.matchMedia('(max-width: 767px)').addEventListener('change', () => mobileNav(false));
  const cat = JSON.parse($('trigger-catalog').textContent);
  const admin = root.dataset.admin === 'true';
  let rules = [], editing = null, draft = null, dirty = false, deleting = null;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const option = (value, text, selected) => `<option value="${esc(value)}" ${String(value) === String(selected) ? 'selected' : ''}>${esc(text)}</option>`;
  const options = (items, value, label='name', key='id') => option('', 'Select…', value) + items.map(x => option(x[key], x[label], value)).join('');
  let labelNumber = 0;
  const label = (text, html) => {
    let id = html.match(/\bid="([^"]+)"/)?.[1];
    if (!id) { id = 'st-field-' + (++labelNumber); html = html.replace(/^<(input|select|textarea)/, `<$1 id="${id}"`); }
    return `<label for="${id}">${text}</label>${html}`;
  };
  const select = (id, items, value) => `<select id="${id}" required>${options(items, value)}</select>`;
  const input = (id, value='', type='text', extra='') => `<input id="${id}" type="${type}" value="${esc(value)}" ${extra}>`;
  const help = {
    stage_moved:'Runs when a lead enters a selected stage.', sequence_ended:'Runs when a selected Auto Follow-up sequence finishes. Pipeline filters are optional.',
    lead_created:'Runs when a new lead is added, including imports.', no_response:'Runs after the latest sent WhatsApp message goes unanswered for this long.',
    keyword:'Matches incoming WhatsApp messages. Use * to match any message.', stage_idle:'Runs once per stage entry, after the selected duration.', call_logged:'Uses the original CRM call statuses. Only calls logged by a team member are included.'
  };
  async function api(path='', method='GET', body) {
    const response = await fetch(root.dataset.api + path, {method, credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRFToken':root.querySelector('[name=csrfmiddlewaretoken]').value}, body: body === undefined ? undefined : JSON.stringify(body)});
    if (response.redirected) throw new Error('Your session expired. Refresh and sign in again.');
    const text = await response.text();
    let data;
    try { data = JSON.parse(text); } catch { throw new Error(response.ok ? 'Unexpected response. Please refresh.' : 'Request failed. Check your connection and permissions.'); }
    if (!response.ok) throw new Error(data.error || 'Unable to save this change.');
    return data;
  }
  function notice(message) { $('st-notice').textContent = message; $('st-notice').hidden = false; }
  const nameOf = (collection, id) => collection.find(x => String(x.id) === String(id))?.name || 'Unavailable selection';
  function scopeSummary(c) { return c.scopes.map(s => `${nameOf(cat.pipelines,s.pipeline)}: ${s.stages.map(id => nameOf(cat.stages,id)).join(', ')}`).join(' or '); }
  function actionSummary(r) {
    const a = r.action;
    if (r.action_type === 'start_sequence') return nameOf(cat.sequences,a.sequence) + (a.replace ? ' · replaces assigned sequence' : '');
    if (r.action_type === 'move_stage') return `${nameOf(cat.pipelines,a.pipeline)} → ${nameOf(cat.stages,a.stage)}`;
    if (['ai','followup'].includes(r.action_type)) return a.enabled ? 'Turn on' : 'Turn off';
    if (r.action_type === 'attribute') return `${cat.attributes.find(x=>x.key===a.key)?.name || a.key}: ${a.value}`;
    if (r.action_type === 'email') return a.subject || '';
    if (r.action_type === 'reminder') return `After ${a.duration} ${a.unit}`;
    if (r.action_type === 'message') return a.schedule === 'fixed' ? `Next ${a.time} · ${cat.timezone}` : a.schedule === 'relative' ? `After ${a.duration} ${a.unit}` : `From ${a.date_attribute}`;
    return 'Remove the currently assigned sequence';
  }
  async function load() {
    try { rules = (await api('rules/')).rules; renderList(); } catch(e) { notice(e.message); $('st-rule-list').textContent='Rules could not be loaded. Refresh to try again.'; }
  }
  function renderList() {
    $('st-total').textContent = rules.length; $('st-active').textContent = rules.filter(r=>r.enabled).length;
    const q = $('st-search').value.toLowerCase(), filter = $('st-filter').value;
    const shown = rules.filter(r => r.name.toLowerCase().includes(q) && (filter === 'all' || r.enabled === (filter === 'enabled')));
    if (!shown.length) {
      $('st-rule-list').innerHTML = `<div class="st-empty"><span class="st-empty-icon">ϟ</span><h2>${rules.length ? 'No matching rules' : 'Your first automation starts here'}</h2><p>${rules.length ? 'Try another name or change the status filter.' : 'Start a follow-up, move a lead, or set a reminder when the moment is right.'}</p>${admin && !rules.length ? '<button class="st-primary" data-new>＋ Create your first rule</button>' : ''}</div>`;
      $('st-rule-list').querySelector('[data-new]')?.addEventListener('click',()=>openEditor()); return;
    }
    const canOrder = admin && !q && filter === 'all';
    $('st-rule-list').innerHTML = `<div class="st-table-wrap"><table><thead><tr><th>Order</th><th>Rule name</th><th>When</th><th>Then</th><th>Enabled</th><th>Actions</th></tr></thead><tbody>${shown.map(r=>`<tr data-id="${r.id}" draggable="${canOrder}"><td><div class="st-order"><span title="Drag to reorder">⠿</span>${canOrder ? `<button class="st-icon-button" data-move="-1" aria-label="Move ${esc(r.name)} up" ${rules.indexOf(r)===0?'disabled':''}>↑</button><button class="st-icon-button" data-move="1" aria-label="Move ${esc(r.name)} down" ${rules.indexOf(r)===rules.length-1?'disabled':''}>↓</button>` : rules.indexOf(r)+1}</div></td><td><strong>${esc(r.name)}</strong><p>${r.conditions.attributes.length ? `${r.conditions.attributes.length} attribute condition(s)` : 'No attribute conditions'}</p></td><td>${esc(cat.triggers[r.trigger_type])}<p>${esc(scopeSummary(r.conditions))}</p></td><td>${esc(cat.actions[r.action_type])}<p>${esc(actionSummary(r))}</p></td><td><button class="st-switch" role="switch" aria-label="Enable ${esc(r.name)}" aria-checked="${r.enabled}" data-toggle ${admin?'':'disabled'}><span></span></button></td><td><div class="st-row-actions"><button class="st-icon-button" data-edit aria-label="${admin?'Edit':'View'} ${esc(r.name)}">${admin?'✎':'↗'}</button>${admin?`<button class="st-icon-button" data-delete aria-label="Delete ${esc(r.name)}">×</button>`:''}</div></td></tr>`).join('')}</tbody></table></div>`;
    let dragId;
    $('st-rule-list').querySelectorAll('tr[data-id]').forEach(row=>{
      const rule=rules.find(r=>r.id===row.dataset.id);
      row.querySelector('[data-edit]').onclick=()=>openEditor(rule);
      row.querySelector('[data-delete]')?.addEventListener('click',()=>{deleting=rule; $('st-confirm-text').textContent=rule.name; $('st-confirm').showModal();});
      row.querySelector('[data-toggle]').onclick=async e=>{e.currentTarget.disabled=true; try{await api(`rules/${rule.id}/`,'PUT',{...rule,enabled:!rule.enabled}); await load();}catch(err){notice(err.message);renderList();}};
      row.querySelectorAll('[data-move]').forEach(b=>b.onclick=()=>{const ids=rules.map(r=>r.id),i=ids.indexOf(rule.id),j=i+Number(b.dataset.move); [ids[i],ids[j]]=[ids[j],ids[i]];saveOrder(ids);});
      row.ondragstart=e=>{if(!canOrder){e.preventDefault();return;}dragId=rule.id;row.classList.add('st-dragging');e.dataTransfer.setData('text/plain',rule.id);};
      row.ondragend=()=>row.classList.remove('st-dragging'); row.ondragover=e=>{if(canOrder)e.preventDefault();};
      row.ondrop=e=>{e.preventDefault();if(!canOrder||!dragId||dragId===rule.id)return;const ids=rules.map(r=>r.id).filter(id=>id!==dragId);ids.splice(ids.indexOf(rule.id),0,dragId);saveOrder(ids);};
    });
  }
  async function saveOrder(ids){try{await api('reorder/','POST',{ids});await load();}catch(e){notice(e.message);}}
  function durationFields(c, prefix='a', minimum=0){return `<div class="st-columns">${label('Duration',input(prefix+'-duration',c.duration??1,'number',`min="${minimum}" max="525600" step="1" required`))}${label('Unit',`<select id="${prefix}-unit">${['minutes','hours','days'].map(x=>option(x,x[0].toUpperCase()+x.slice(1),c.unit||'hours')).join('')}</select>`)}</div>`;}
  function openEditor(rule) {
    editing=rule?.id || null; draft=rule?structuredClone(rule):{name:'',enabled:false,trigger_type:'',action_type:'',conditions:{scopes:[{pipeline:'',stages:[]}],attributes:[]},action:{}};
    dirty=false; $('st-home').hidden=true;$('st-editor').hidden=false;$('st-notice').hidden=true;$('st-form-error').hidden=true;
    $('st-editor-title').textContent=rule?(admin?'Edit rule':'View rule'):'Create a rule'; $('st-fields').disabled=!admin;
    $('st-name').value=draft.name;$('st-enabled').checked=draft.enabled;
    $('st-trigger').innerHTML=option('','Choose a trigger',draft.trigger_type)+Object.entries(cat.triggers).map(([k,v])=>option(k,v,draft.trigger_type)).join('');
    $('st-action').innerHTML=option('','Choose an action',draft.action_type)+Object.entries(cat.actions).map(([k,v])=>option(k,v,draft.action_type)).join('');
    renderTrigger();renderScopes();renderConditions();renderAction();summary();$('st-name').focus();
  }
  function renderTrigger(){
    const c=draft.conditions,kind=draft.trigger_type; $('st-trigger-help').textContent=help[kind]||'Select the event your rule should listen for.';
    let html='';
    if(['no_response','stage_idle'].includes(kind))html=durationFields(c,'c',1);
    if(kind==='keyword')html=label('Keywords',input('c-keywords',(c.keywords||[]).join(', '),'text','maxlength="500" required placeholder="interested, quote, price"'))+'<p class="st-hint">Up to 50 keywords separated by commas. Matches are case-insensitive.</p>';
    if(kind==='sequence_ended')html=label('Sequences',`<select id="c-sequences" multiple required size="4">${cat.sequences.map(s=>`<option value="${s.id}" ${(c.sequences||[]).includes(s.id)?'selected':''}>${esc(s.name)}</option>`).join('')}</select>`)+`<p class="st-hint">${cat.sequences.length?'Choose one or more. Ctrl / ⌘ selects multiple.':'Create a sequence in Auto Follow-ups first.'}</p>`;
    if(kind==='call_logged')html=label('Call status',`<select id="c-call-status" required>${option('','Select status',c.call_status)}${Object.entries(cat.call_statuses).map(([k,v])=>option(k,v,c.call_status)).join('')}</select>`);
    $('st-trigger-extra').innerHTML=html;
  }
  function renderScopes(){
    $('st-scopes').innerHTML=draft.conditions.scopes.map((s,i)=>`<div class="st-scope" data-scope="${i}"><div class="st-scope-head"><select aria-label="Pipeline ${i+1}" data-pipeline required>${options(cat.pipelines,s.pipeline)}</select><button type="button" class="st-icon-button" data-remove-scope aria-label="Remove pipeline group">×</button></div><div class="st-stage-options">${s.pipeline?cat.stages.filter(x=>x.pipeline_id===s.pipeline).map(stage=>`<label class="st-check"><input type="checkbox" value="${stage.id}" ${s.stages.includes(stage.id)?'checked':''}>${esc(stage.name)}</label>`).join(''):'<p class="st-hint">Choose a pipeline to see its stages.</p>'}</div></div>`).join('');
    $('st-scopes').querySelectorAll('[data-scope]').forEach(el=>{const i=Number(el.dataset.scope);el.querySelector('[data-pipeline]').onchange=e=>{draft.conditions.scopes[i]={pipeline:e.target.value,stages:[]};dirty=true;renderScopes();summary();};el.querySelector('[data-remove-scope]').onclick=()=>{draft.conditions.scopes.splice(i,1);dirty=true;renderScopes();summary();};el.querySelectorAll('input').forEach(x=>x.onchange=()=>{draft.conditions.scopes[i].stages=[...el.querySelectorAll('input:checked')].map(y=>y.value);dirty=true;summary();});});
  }
  function renderConditions(){
    $('st-conditions').innerHTML=draft.conditions.attributes.map((c,i)=>`<div class="st-condition" data-condition="${i}"><div class="st-columns">${label('Attribute',`<select data-key required>${options(cat.attributes,c.key,'name','key')}</select>`)}${label('Match',`<select data-match>${option('equals','Equals',c.match)}${option('contains','Contains',c.match)}</select>`)}<button type="button" class="st-icon-button" data-remove-condition aria-label="Remove condition">×</button></div>${label('Values',`<input data-values value="${esc(c.values.join(', '))}" required placeholder="Separate alternatives with commas">`)}</div>`).join('');
    $('st-conditions').querySelectorAll('[data-condition]').forEach(el=>{const i=Number(el.dataset.condition);el.querySelector('[data-remove-condition]').onclick=()=>{readConditions();draft.conditions.attributes.splice(i,1);dirty=true;renderConditions();summary();};});
  }
  function readConditions(){ $('st-conditions').querySelectorAll('[data-condition]').forEach(el=>{draft.conditions.attributes[Number(el.dataset.condition)]={key:el.querySelector('[data-key]').value,match:el.querySelector('[data-match]').value,values:el.querySelector('[data-values]').value.split(',').map(v=>v.trim()).filter(Boolean)};}); }
  function chips(){return `<div class="st-chips">${['lead_name','lead_first_name','phone','email','org_name','user_name','pipeline_name','stage_name','lead_source',...cat.attributes.map(a=>a.key)].map(k=>`<button class="st-chip" type="button" data-variable="${esc(k)}">＋ ${esc(k)}</button>`).join('')}</div>`;}
  function renderAction(){
    const a=draft.action,kind=draft.action_type;let html='';
    if(kind==='start_sequence')html=label('Auto Follow-up sequence',select('a-sequence',cat.sequences,a.sequence))+`<label class="st-check"><input type="checkbox" id="a-replace" ${a.replace?'checked':''}>Replace any assigned sequence</label><p class="st-info">${cat.sequences.length?'Uses your existing sequence steps, sender, and scheduling settings.':'Create a sequence in Auto Follow-ups before choosing this action.'}</p>`;
    if(kind==='move_stage')html=label('Destination pipeline',select('a-pipeline',cat.pipelines,a.pipeline))+label('Destination stage',select('a-stage',cat.stages.filter(s=>s.pipeline_id===a.pipeline),a.stage));
    if(['ai','followup'].includes(kind))html=label('Status',`<select id="a-enabled" required>${option('','Choose status',a.enabled)}${option('true','On',a.enabled)}${option('false','Off',a.enabled)}</select>`)+`<p class="st-info">${kind==='ai'?'Controls the lead’s existing AI setting. Organization and stage AI settings still apply.':'Controls the assigned Auto Follow-up sequence. If no sequence is assigned, the run is recorded as failed.'}</p>`;
    if(kind==='stop_sequence')html='<p class="st-warn">When this rule runs, the lead’s assigned sequence is removed. Previous follow-up history is retained.</p>';
    if(kind==='attribute'){
      const d=cat.attributes.find(x=>x.key===a.key); html=label('CRM attribute',`<select id="a-key" required>${options(cat.attributes,a.key,'name','key')}</select>`);
      html+=label('Value',d?.field_type==='option'?`<select id="a-value" required>${option('','Select value',a.value)}${d.options.map(x=>option(x,x,a.value)).join('')}</select>`:input('a-value',a.value,d?.field_type==='datetime'?'datetime-local':d?.field_type==='numeric'?'number':d?.field_type==='date'?'date':'text',`${d?'':'disabled'} ${d?.field_type==='numeric'?'step="any"':''}`));
    }
    if(kind==='reminder')html=durationFields(a)+label('Note (optional)',`<textarea id="a-note" maxlength="2000" placeholder="e.g. Follow up on the pricing discussion">${esc(a.note)}</textarea>`)+`<label class="st-check"><input id="a-overwrite" type="checkbox" ${a.overwrite?'checked':''}>Overwrite an existing pending reminder</label>`;
    if(['message','email'].includes(kind)){
      if(kind==='email')html='<p class="st-info">Send to: lead’s CRM email address. Uses the same sender-readiness setting as Auto Follow-ups.</p>'+label('Subject',input('a-subject',a.subject,'text','maxlength="255" required'));
      else html=label('WhatsApp API number',`<select id="a-account" required>${option('','Select connected number',a.account)}${cat.accounts.map(x=>option(x.id,`${x.business_name} ${x.display_phone_number}`,a.account)).join('')}</select>`);
      html+=label(kind==='email'?'Email body':'Message',`<textarea id="a-body" maxlength="20000" required placeholder="Hi {{lead_first_name}}, …">${esc(a.body)}</textarea>`)+chips();
      if(kind==='message'){
        html+='<p class="st-info">Free-text messages use Shvya’s WhatsApp API and require a reply from the lead within the last 24 hours. For approved templates, use a follow-up sequence.</p>';
        html+=label('Send at',`<select id="a-schedule">${option('relative','Relative to when the trigger fires',a.schedule||'relative')}${option('fixed','Fixed time · next occurrence',a.schedule)}${option('attribute','From a lead’s date-time attribute',a.schedule)}</select>`);
        if(a.schedule==='fixed')html+=label(`Time of day (${esc(cat.timezone)})`,input('a-time',a.time,'time','required'));
        else if(a.schedule==='attribute')html+=label('Date-time attribute',`<select id="a-date-attribute" required>${options(cat.attributes.filter(x=>x.field_type==='datetime'),a.date_attribute,'name','key')}</select>`);
        else html+=durationFields(a);
      }
    }
    $('st-action-fields').innerHTML=html;
    $('a-pipeline')?.addEventListener('change',e=>{draft.action.pipeline=e.target.value;draft.action.stage='';dirty=true;renderAction();summary();});
    $('a-key')?.addEventListener('change',e=>{draft.action.key=e.target.value;draft.action.value='';dirty=true;renderAction();summary();});
    $('a-schedule')?.addEventListener('change',e=>{readAction();draft.action.schedule=e.target.value;dirty=true;renderAction();summary();});
    $('st-action-fields').querySelectorAll('[data-variable]').forEach(b=>b.onclick=()=>{const t=$('a-body');t.setRangeText('{{'+b.dataset.variable+'}}',t.selectionStart,t.selectionEnd,'end');t.focus();dirty=true;});
  }
  function readAction(){
    const a=draft.action;
    ['sequence','pipeline','stage','key','value','subject','body','note','account','schedule','time','date-attribute','unit'].forEach(k=>{const el=$('a-'+k);if(el)a[k.replaceAll('-','_')]=el.value;});
    ['replace','overwrite'].forEach(k=>{if($('a-'+k))a[k]=$('a-'+k).checked;});
    if($('a-duration'))a.duration=Number($('a-duration').value);
    if($('a-enabled'))a.enabled=$('a-enabled').value===''?null:$('a-enabled').value==='true';
  }
  function readDraft(){
    draft.name=$('st-name').value;draft.enabled=$('st-enabled').checked;readConditions();readAction();
    const c=draft.conditions;
    if($('c-duration')){c.duration=Number($('c-duration').value);c.unit=$('c-unit').value;}
    if($('c-keywords'))c.keywords=$('c-keywords').value.split(',').map(x=>x.trim()).filter(Boolean);
    if($('c-sequences'))c.sequences=[...$('c-sequences').selectedOptions].map(o=>o.value);
    if($('c-call-status'))c.call_status=$('c-call-status').value;
  }
  function summary(){
    $('st-name-count').textContent=`${$('st-name').value.length} / 255`;
    $('st-summary-text').textContent=draft.trigger_type&&draft.action_type?`When ${cat.triggers[draft.trigger_type].toLowerCase()}${scopeSummary(draft.conditions)?' in '+scopeSummary(draft.conditions):''}, ${cat.actions[draft.action_type].toLowerCase()}.`:'Choose a trigger and an action to build your rule.';
  }
  function closeEditor(){if(dirty&&!window.confirm('Discard your unsaved changes?'))return;dirty=false;$('st-editor').hidden=true;$('st-home').hidden=false;}
  $('st-new')?.addEventListener('click',()=>openEditor());$('st-back').onclick=closeEditor;$('st-cancel').onclick=closeEditor;
  $('st-trigger').onchange=()=>{readDraft();draft.trigger_type=$('st-trigger').value;draft.conditions={scopes:draft.conditions.scopes,attributes:draft.conditions.attributes};dirty=true;renderTrigger();summary();};
  $('st-action').onchange=()=>{draft.action_type=$('st-action').value;draft.action={};dirty=true;renderAction();summary();};
  $('st-add-scope').onclick=()=>{draft.conditions.scopes.push({pipeline:'',stages:[]});dirty=true;renderScopes();};
  $('st-add-condition').onclick=()=>{readConditions();draft.conditions.attributes.push({key:'',match:'equals',values:[]});dirty=true;renderConditions();};
  $('st-form').addEventListener('input',()=>{dirty=true;summary();});
  $('st-form').onsubmit=async e=>{e.preventDefault();if(!admin)return;readDraft();$('st-form-error').hidden=true;$('st-save').disabled=true;try{await api(editing?`rules/${editing}/`:'rules/',editing?'PUT':'POST',draft);dirty=false;closeEditor();await load();}catch(err){$('st-form-error').textContent=err.message;$('st-form-error').hidden=false;$('st-form-error').scrollIntoView({block:'center',behavior:'smooth'});}finally{$('st-save').disabled=false;}};
  $('st-search').oninput=renderList;$('st-filter').onchange=renderList;
  $('st-delete-cancel').onclick=()=>$('st-confirm').close();
  $('st-delete-confirm').onclick=async()=>{if(!deleting)return;$('st-delete-confirm').disabled=true;try{await api(`rules/${deleting.id}/`,'DELETE');$('st-confirm').close();await load();}catch(e){$('st-confirm').close();notice(e.message);}finally{$('st-delete-confirm').disabled=false;}};
  async function history(){try{const runs=(await api('history/')).runs;$('st-history-list').innerHTML=runs.length?`<div class="st-table-wrap"><table><thead><tr><th>Rule / lead</th><th>Status</th><th>Details</th><th>Created</th></tr></thead><tbody>${runs.map(r=>`<tr><td><strong>${esc(r.rule)}</strong><p>${esc(r.lead)}</p></td><td><span class="st-badge ${esc(r.status)}">${esc(r.status.replaceAll('_',' '))}</span></td><td><p>${esc(r.detail || (r.status==='scheduled'?'Scheduled for '+new Date(r.due_at).toLocaleString():r.status==='sending'?'Delivery started. If this remains unchanged, check provider logs.':'—'))}</p></td><td>${esc(new Date(r.created_at).toLocaleString())}</td></tr>`).join('')}</tbody></table></div>`:'<div class="st-empty"><h2>No runs yet</h2><p>Enable a rule and try it with a sample lead. Its activity will appear here.</p></div>';}catch(e){notice(e.message);}}
  function tab(which){const isRules=which==='rules';$('st-rules-panel').hidden=!isRules;$('st-history-panel').hidden=isRules;$('st-rules-tab').setAttribute('aria-selected',isRules);$('st-history-tab').setAttribute('aria-selected',!isRules);if(!isRules)history();}
  $('st-rules-tab').onclick=()=>tab('rules');$('st-history-tab').onclick=()=>tab('history');$('st-refresh-history').onclick=history;
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  load();
})();
