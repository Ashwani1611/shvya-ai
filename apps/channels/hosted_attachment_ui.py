"""Progressively enhance the existing Hosted chat composer with file sending.

Keeping this as a wrapper avoids replacing the realtime Hosted inbox template or
its scrolling/socket behavior. The paperclip button gains an authenticated
photo/video/document picker backed by the Hosted-only media send endpoint.
"""

from . import hosted_chat_ui


_ATTACHMENT_STYLE = b"""
<style data-hosted-attachment-ui>
.chat-compose{position:relative}
.hosted-attachment-popover{position:absolute;left:50px;bottom:62px;z-index:40;display:none;min-width:190px;border:1px solid #e5e7eb;border-radius:12px;background:#fff;padding:7px;box-shadow:0 12px 30px rgba(15,23,42,.18)}
.hosted-attachment-popover.is-open{display:block}
.hosted-attachment-option{display:flex;width:100%;align-items:center;gap:10px;border:0;border-radius:8px;background:transparent;padding:10px 11px;text-align:left;font-size:13px;color:#334155}
.hosted-attachment-option:hover{background:#f8fafc}
.hosted-attachment-option i{font-size:20px}
.hosted-upload-status{position:absolute;left:15px;right:15px;bottom:66px;z-index:39;display:none;border:1px solid #cbd5e1;border-radius:10px;background:#fff;padding:10px 12px;box-shadow:0 8px 20px rgba(15,23,42,.12);font-size:12px;color:#475569}
.hosted-upload-status.is-visible{display:block}
</style>
"""


_ATTACHMENT_SCRIPT = b"""
<script data-hosted-attachment-ui>
(function(){
  const app=document.getElementById('hosted-chat-app');
  const form=document.getElementById('chat-form');
  if(!app||!form)return;

  const buttons=Array.from(form.querySelectorAll('.chat-icon-button'));
  const attachmentButton=buttons.find(button=>button.querySelector('.ti-paperclip'));
  const chatInput=document.getElementById('chat-key-input');
  const csrf=document.querySelector('input[name=csrfmiddlewaretoken]')?.value||'';
  if(!attachmentButton||!chatInput)return;

  attachmentButton.title='Attach photo, video or document';
  attachmentButton.setAttribute('aria-label','Attach photo, video or document');

  const popover=document.createElement('div');
  popover.className='hosted-attachment-popover';
  popover.innerHTML=''
    +'<button type="button" class="hosted-attachment-option" data-kind="image"><i class="ti ti-photo"></i><span>Photo</span></button>'
    +'<button type="button" class="hosted-attachment-option" data-kind="video"><i class="ti ti-video"></i><span>Video</span></button>'
    +'<button type="button" class="hosted-attachment-option" data-kind="document"><i class="ti ti-file-text"></i><span>Document</span></button>';

  const input=document.createElement('input');
  input.type='file';
  input.hidden=true;
  input.id='hosted-attachment-input';

  const status=document.createElement('div');
  status.className='hosted-upload-status';

  form.appendChild(popover);
  form.appendChild(input);
  form.appendChild(status);

  let kind='';
  const accepts={
    image:'image/*',
    video:'video/*',
    document:'.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,application/pdf,text/plain,text/csv'
  };

  function showStatus(message,isError){
    status.textContent=message||'';
    status.classList.toggle('is-visible',Boolean(message));
    status.style.color=isError?'#b91c1c':'#475569';
    status.style.borderColor=isError?'#fecaca':'#cbd5e1';
  }

  attachmentButton.addEventListener('click',event=>{
    event.preventDefault();
    event.stopPropagation();
    popover.classList.toggle('is-open');
  });

  popover.addEventListener('click',event=>{
    const option=event.target.closest('[data-kind]');
    if(!option)return;
    kind=option.dataset.kind||'';
    input.accept=accepts[kind]||'';
    input.value='';
    popover.classList.remove('is-open');
    input.click();
  });

  document.addEventListener('click',event=>{
    if(!popover.contains(event.target)&&event.target!==attachmentButton&&!attachmentButton.contains(event.target)){
      popover.classList.remove('is-open');
    }
  });

  input.addEventListener('change',async()=>{
    const file=input.files&&input.files[0];
    const chat=String(chatInput.value||'').trim();
    if(!file||!kind||!chat)return;
    if(file.size>25*1024*1024){
      showStatus('Attachments must be 25 MB or smaller.',true);
      input.value='';
      return;
    }

    const caption=String(form.elements.body?.value||'').trim();
    const data=new FormData();
    data.append('chat',chat);
    data.append('message_type',kind);
    data.append('attachment',file,file.name);
    data.append('caption',caption);

    attachmentButton.disabled=true;
    showStatus('Uploading '+file.name+'…',false);
    try{
      const base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
      const response=await fetch(base+'send-media/',{
        method:'POST',
        headers:{'X-CSRFToken':csrf},
        body:data
      });
      const result=await response.json();
      if(!response.ok)throw new Error(result.error||'Could not send attachment');
      if(form.elements.body)form.elements.body.value='';
      showStatus('Attachment queued for WhatsApp.',false);
      setTimeout(()=>showStatus('',false),1800);
    }catch(error){
      showStatus(error.message||'Could not send attachment',true);
    }finally{
      attachmentButton.disabled=false;
      input.value='';
      kind='';
    }
  });
})();
</script>
"""


def hosted_session_chats_view(request, account_id):
    response = hosted_chat_ui.hosted_session_chats_view(request, account_id)
    content_type = response.get("Content-Type", "")
    if response.status_code != 200 or "text/html" not in content_type:
        return response

    content = response.content
    if b"data-hosted-attachment-ui" not in content:
        if b"</head>" in content:
            content = content.replace(b"</head>", _ATTACHMENT_STYLE + b"</head>", 1)
        if b"</body>" in content:
            content = content.replace(b"</body>", _ATTACHMENT_SCRIPT + b"</body>", 1)

    response.content = content
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(content))
    return response
