/* One controller for independently loaded CRM panels across inboxes. */
(() => {
  'use strict';
  let collapsed = false;
  try { collapsed = sessionStorage.getItem('shvya-contact-collapsed') === 'true'; } catch (_) {}
  function panelChrome(host) {
    host.classList.toggle('contact-collapsed', collapsed);
    let button = host.querySelector('[data-contact-collapse]');
    if (!button) { button = document.createElement('button'); button.type='button'; button.dataset.contactCollapse=''; button.className='contact-collapse'; host.prepend(button); }
    button.setAttribute('aria-expanded', String(!collapsed));
    button.setAttribute('aria-label', collapsed ? 'Expand contact details' : 'Collapse contact details');
    button.title=button.getAttribute('aria-label');
    button.textContent=collapsed ? '‹' : '›';
  }
  const states = new Map();
  const pending = new Set();
  const csrf = () => document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  async function post(url, data) {
    const response = await fetch(url, {method:'POST', credentials:'same-origin', headers:{'X-CSRFToken':csrf(), 'X-Requested-With':'XMLHttpRequest', 'Accept':'application/json'}, body:data, keepalive:true});
    let result = {}; try { result = await response.json(); } catch (_) {}
    if (!response.ok || response.redirected) throw new Error(result.error || 'Could not save. Please retry.');
    return result;
  }
  function feedback(host, message, error=false) {
    const status=host.querySelector('[data-contact-feedback]');
    if(status){status.textContent=message;status.hidden=false;status.style.color=error?'#b42318':'#245480';}
  }
  function saveState(form) {
    let state=states.get(form);
    if(!state){state={dirty:new Map(),latest:new Map(),timer:null,chain:Promise.resolve(),failed:false};states.set(form,state);}
    return state;
  }
  function enqueue(form) {
    const state=saveState(form); clearTimeout(state.timer);
    if(!state.dirty.size) return state.chain;
    const changes=new Map(state.dirty); state.dirty.clear();
    const data=new FormData(); changes.forEach((v,k)=>data.set(k,v));
    const host=form.closest('[data-contact-host]');
    const status=host?.querySelector('[data-save-status]');
    if(status)status.textContent='Saving…';
    const job=state.chain.then(async()=>{
      try {
        await post(form.action,data);state.failed=false;
        if(status)status.textContent=state.dirty.size?'Unsaved changes':'Saved';
      } catch(error) {
        changes.forEach((v,k)=>{if(state.latest.get(k)===v&&!state.dirty.has(k))state.dirty.set(k,v);});
        state.failed=state.dirty.size>0;
        if(status)status.textContent='Not saved';
        feedback(host,error.message+' Edit the field to retry.',true);
      }
    });
    state.chain=job; pending.add(job);job.finally(()=>pending.delete(job));return job;
  }
  async function flush() {
    for(const [form,state] of states){if(state.dirty.size)enqueue(form);}
    await Promise.all([...pending]);
    for (const [form,state] of states) { if (!form.isConnected && !state.failed && !state.dirty.size) states.delete(form); }
    return ![...states.values()].some(s=>s.failed||s.dirty.size);
  }
  async function load(host, force=false) {
    if(!host || !host.dataset.sidebarUrl)return false;
    const url=host.dataset.sidebarUrl;
    if(host.dataset.loadedUrl===url&&!force){
      if(host._panelLoadPromise)return host._panelLoadPromise;
      return true;
    }
    host._panelRequest?.abort();
    const request=new AbortController();
    host._panelRequest=request;
    host.dataset.loadedUrl=url;
    if(!force)host.innerHTML='<p class="contact-empty" role="status">Loading contact…</p>';
    panelChrome(host);
    const job=(async()=>{
      try{
        const response=await fetch(url,{credentials:'same-origin',signal:request.signal});
        if(!response.ok||!response.headers.get('content-type')?.includes('text/html')||(response.redirected&&!new URL(response.url).pathname.endsWith('/contact-panel/')))throw new Error('Could not load contact details.');
        const html=await response.text();
        if(!host.isConnected||host.dataset.sidebarUrl!==url)return false;
        const selected=host.querySelector('[data-contact-panel]:not([hidden])')?.dataset.contactPanel;
        host.innerHTML=html;
        host.querySelector('[data-create-chat-lead]')?.setAttribute('action',url);
        panelChrome(host);
        host.querySelector('[data-note-editor]')?.removeAttribute('onsubmit');
        if(window.htmx)window.htmx.process(host);
        if(selected)select(host,selected);
        return true;
      }catch(error){
        if(error.name!=='AbortError'&&host.dataset.sidebarUrl===url){
          delete host.dataset.loadedUrl;
          host.innerHTML='<p class="contact-empty" role="alert">Could not load contact details.</p><button type="button" class="contact-primary" data-panel-retry>Retry</button>';
          panelChrome(host);
        }
        return false;
      }
    })();
    host._panelLoadPromise=job;
    try{return await job;}
    finally{if(host._panelLoadPromise===job)host._panelLoadPromise=null;}
  }
  function select(host,name){
    host.querySelectorAll('[data-contact-panel]').forEach(p=>p.hidden=p.dataset.contactPanel!==name);
    host.querySelectorAll('[data-contact-tab]').forEach(t=>t.setAttribute('aria-selected',String(t.dataset.contactTab===name)));
  }
  function init(root=document){root.querySelectorAll('[data-contact-host]').forEach(h=>{panelChrome(h);load(h);});}
  document.addEventListener('input',e=>{
    const form=e.target.closest('form[data-autosave]');
    if(form&&e.target.name){const state=saveState(form);state.dirty.set(e.target.name,e.target.value);state.latest.set(e.target.name,e.target.value);clearTimeout(state.timer);state.timer=setTimeout(()=>enqueue(form),650);const s=form.closest('[data-contact-host]').querySelector('[data-save-status]');if(s)s.textContent='Unsaved changes';}
    const host=e.target.closest('[data-contact-host]');if(!host)return;
    if(e.target.matches('[data-reply-search]'))filterReplies(host);
    if(e.target.matches('[data-template-search]'))host.querySelectorAll('[data-template-name]').forEach(n=>n.hidden=!n.dataset.templateName.includes(e.target.value.toLowerCase()));
  });
  document.addEventListener('change',async e=>{
    const host=e.target.closest('[data-contact-host]');if(!host)return;
    const autosave=e.target.closest('form[data-autosave]');
    if(autosave&&e.target.name){saveState(autosave).dirty.set(e.target.name,e.target.value);saveState(autosave).latest.set(e.target.name,e.target.value);enqueue(autosave);}
    if(e.target.matches('[data-reply-category]'))filterReplies(host);
    const routing=e.target.closest('[data-routing]');
    if(routing){e.target.disabled=true;try{if(!await flush())throw new Error('Save your pending changes before changing pipeline or stage.');const data=new FormData();data.set(e.target.name,e.target.value);await post(routing.action,data);await load(host,true);}catch(error){e.target.value=[...e.target.options].find(o=>o.defaultSelected)?.value||'';feedback(host,error.message,true);}finally{e.target.disabled=false;}}
  });
  function filterReplies(host){const q=host.querySelector('[data-reply-search]').value.toLowerCase();const c=host.querySelector('[data-reply-category]').value;host.querySelectorAll('[data-reply]').forEach(r=>r.hidden=(c&&r.dataset.category!==c)||!r.textContent.toLowerCase().includes(q));}
  document.addEventListener('click',async e=>{
    const opener=e.target.closest('[data-open-contact-tab]');
    if(opener){
      const host=document.querySelector('[data-contact-host]');
      if(!host)return;
      const target=opener.dataset.openContactTab||'personal';
      collapsed=false;
      try{sessionStorage.setItem('shvya-contact-collapsed','false');}catch(_){}
      panelChrome(host);
      await load(host);
      if(!host.querySelector('[data-contact-panel="'+target+'"]')){
        await load(host,true);
      }
      if(!host.querySelector('[data-contact-panel="'+target+'"]'))return;
      select(host,target);
      host.querySelector('.contact-scroll')?.scrollTo({top:0,behavior:'smooth'});
      if(target==='templates')host.querySelector('[data-template-search]')?.focus({preventScroll:true});
      return;
    }
    const host=e.target.closest('[data-contact-host]');if(!host)return;
    if(e.target.closest('[data-contact-collapse]')){collapsed=!collapsed;try{sessionStorage.setItem('shvya-contact-collapsed',String(collapsed));}catch(_){}panelChrome(host);return;}
    const followup=e.target.closest('[data-followup-url]');
    if(followup&&!followup.disabled){followup.disabled=true;const data=new FormData();data.set('enabled',followup.getAttribute('aria-checked')==='true'?'false':'true');try{if(!await flush())throw new Error('Save pending changes before changing follow-ups.');await post(followup.dataset.followupUrl,data);await load(host,true);}catch(error){feedback(host,error.message,true);}finally{followup.disabled=false;}}
    const tab=e.target.closest('[data-contact-tab]');if(tab)select(host,tab.dataset.contactTab);
    const copy=e.target.closest('[data-copy-phone]');if(copy){try{await navigator.clipboard.writeText(copy.dataset.copyPhone);feedback(host,'Phone copied.');}catch(_){feedback(host,'Unable to copy phone.',true);}}
    if(e.target.closest('[data-panel-retry]'))load(host,true);
    const ai=e.target.closest('[data-ai-url]');if(ai&&!ai.disabled){ai.disabled=true;const data=new FormData();data.set('enabled',ai.getAttribute('aria-checked')==='true'?'0':'1');try{const r=await post(ai.dataset.aiUrl,data);ai.setAttribute('aria-checked',String(r.enabled));}catch(error){feedback(host,error.message,true);}finally{ai.disabled=false;}}
    const reply=e.target.closest('[data-insert-reply]');if(reply){const input=document.getElementById('message-body');if(!input||input.disabled){feedback(host,'This conversation is not currently available for replies.',true);return;}const body=reply.closest('[data-reply]').querySelector('[data-reply-body]').textContent;input.value=input.value?input.value+(input.tagName==='TEXTAREA'?'\n':' ')+body:body;input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();feedback(host,'Reply added. Review it in the message box and send.');}
  });
  document.addEventListener('submit',async e=>{
    const form=e.target,host=form.closest('[data-contact-host]');if(!host)return;
    if(form.matches('[data-autosave],[data-routing]')){e.preventDefault();enqueue(form);return;}
    if(!form.matches('[data-send-template],[data-start-sequence],[data-note-editor],[data-create-chat-lead]'))return;
    e.preventDefault();e.stopImmediatePropagation();const button=form.querySelector('[type=submit]');if(button.disabled)return;button.disabled=true;
    try{const result=await post(form.action,new FormData(form));if(form.matches('[data-create-chat-lead]')){if(result.redirect_url){location.assign(result.redirect_url);return;}host.dataset.sidebarUrl=result.sidebar_url;await load(host,true);document.dispatchEvent(new Event('shvya:lead-created'));}else if(form.matches('[data-send-template]')){feedback(host,'Template queued.');if(window.shvyaWhatsAppNavigate)await window.shvyaWhatsAppNavigate(window.location.href,false,false);}else{await load(host,true);feedback(host,result.message||'Note saved.');}}catch(error){feedback(host,error.message,true);}finally{button.disabled=false;}
  },true);
  document.addEventListener('leadCardUpdated',async e=>{const host=document.querySelector('[data-contact-host]');if(host&&(!e.detail?.lead_id||host.querySelector('[data-lead-id]')?.dataset.leadId===e.detail.lead_id)){document.getElementById('modal-root')?.replaceChildren();await load(host,true);}});
  document.addEventListener('shvya:contact-refresh',()=>init());
  window.addEventListener('beforeunload',e=>{if([...states.values()].some(s=>s.dirty.size||s.failed)||pending.size){e.preventDefault();e.returnValue='';}});
  window.ShvyaContact={init,load,flush};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>init());else init();
})();
