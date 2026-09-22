/* Persisted delegation works after the inbox shell changes. */
(() => {
  const drafts=new Map();
  document.addEventListener('keydown',event=>{
    if(event.target.matches('#wa-web-shell #message-body') && event.key==='Enter' && !event.shiftKey && !event.isComposing){event.preventDefault();event.target.form.requestSubmit();}
  });
  document.addEventListener('input',event=>{const form=event.target.closest('#wa-web-shell #composer-form');if(form)drafts.set(form.dataset.leadId,event.target.value);});
  document.addEventListener('shvya:contact-refresh',()=>{const form=document.querySelector('#wa-web-shell #composer-form');if(form)form.querySelector('#message-body').value=drafts.get(form.dataset.leadId)||'';});
  document.addEventListener('submit',async event=>{
    const form=event.target.closest('#wa-web-shell #composer-form');if(!form)return;
    event.preventDefault();const input=form.querySelector('#message-body'),button=form.querySelector('[type=submit]');
    if(button.disabled||!input.value.trim())return;
    const body=input.value.trim(),data=new FormData(form);data.set('body',body);button.disabled=true;
    const error=form.parentElement.querySelector('#send-error');error.classList.add('hidden');
    try{const response=await fetch(form.action,{method:'POST',headers:{'X-CSRFToken':form.querySelector('[name=csrfmiddlewaretoken]').value},body:data});const result=await response.json();if(!response.ok)throw new Error(result.error||'Message could not be sent.');if(input.value.trim()===body){input.value='';drafts.delete(form.dataset.leadId);}if(form.isConnected)window.shvyaWhatsAppNavigate?.(location.href,false,false);}
    catch(err){error.textContent=err.message;error.classList.remove('hidden');}finally{button.disabled=false;}
  });
})();
