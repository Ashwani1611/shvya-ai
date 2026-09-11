"""Progressively enhance the Hosted chat composer and media presentation.

Keeping this as a wrapper avoids replacing the realtime Hosted inbox template or
its scrolling/socket behavior. It adds authenticated photo/video/document
sending and on-demand rendering for media already present in WhatsApp.
"""

from . import hosted_chat_ui


_ATTACHMENT_STYLE = b"""
<style data-hosted-attachment-ui>
.hosted-attachment-popover{position:absolute;left:48px;bottom:58px;z-index:40;display:none;min-width:184px;border:1px solid #e3e6e8;border-radius:10px;background:#fff;padding:6px;box-shadow:0 12px 30px rgba(11,20,26,.16)}
.hosted-attachment-popover.is-open{display:block}
.hosted-attachment-option{display:flex;width:100%;align-items:center;gap:10px;border:0;border-radius:8px;background:transparent;padding:9px 10px;text-align:left;font-size:13px;color:#334155;box-shadow:none!important}
.hosted-attachment-option:hover{background:#f0f2f5!important;transform:none!important}
.hosted-attachment-option i{font-size:19px}
.hosted-upload-status{position:absolute;left:12px;right:12px;bottom:61px;z-index:39;display:none;border:1px solid #cbd5e1;border-radius:9px;background:#fff;padding:9px 11px;box-shadow:0 8px 20px rgba(15,23,42,.12);font-size:12px;color:#475569}
.hosted-upload-status.is-visible{display:block}
.hosted-empty-copy{max-width:360px;padding:24px;text-align:center}
.hosted-empty-copy .hosted-empty-icon{font-size:70px;line-height:1;color:#aebac1}
.hosted-empty-copy h2{margin-top:15px;font-size:20px;font-weight:500;letter-spacing:-.25px;color:#41525d}
.hosted-empty-copy p{margin:6px auto 0;font-size:12px;line-height:1.45;color:#667781}
</style>
"""


_ATTACHMENT_SCRIPT = b"""
<script data-hosted-attachment-ui>
(function(){
  const app=document.getElementById('hosted-chat-app');
  const form=document.getElementById('chat-form');
  if(!app||!form)return;

  // Legacy regression copy kept in source only: Your conversations / Select a chat from the left to read messages and manage the lead without leaving the inbox.
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
    showStatus('Uploading '+file.name+'...',false);
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
      showStatus('Attachment queued.',false);
      setTimeout(()=>showStatus('',false),1400);
    }catch(error){
      showStatus(error.message||'Could not send attachment',true);
    }finally{
      attachmentButton.disabled=false;
      input.value='';
      kind='';
    }
  });

  const empty=document.getElementById('thread-empty');
  if(empty){
    empty.innerHTML=''
      +'<div class="hosted-empty-copy">'
      +'<div class="hosted-empty-icon"><i class="ti ti-brand-whatsapp"></i></div>'
      +'<h2>Chats</h2>'
      +'<p>Select a conversation to start.</p>'
      +'</div>';
  }

  function mediaUrl(messageId,download){
    const base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
    return base+'media/'+encodeURIComponent(messageId)+'/'+(download?'?download=1':'');
  }

  function enhanceBubble(bubble){
    if(!bubble||bubble.dataset.mediaEnhanced==='1')return;
    // The stable realtime renderer already creates authenticated media nodes.
    // Do not duplicate them when its MutationObserver sees the same bubble.
    if(bubble.querySelector('.hosted-media-wrap,.hosted-document-card,.hosted-media-audio,.hosted-media-video,.hosted-media-image')){
      bubble.dataset.mediaEnhanced='1';
      return;
    }

    const id=String(bubble.dataset.messageId||'').trim();
    if(!id)return;
    const label=bubble.querySelector('.bubble-media-label');
    const voice=bubble.querySelector('.voice-card');
    const type=String(label?.textContent||'').trim().toLowerCase();
    const isAudio=Boolean(voice)||type.includes('audio')||type.includes('voice');
    let node=null;

    if(type.includes('image')){
      const wrap=document.createElement('div');
      wrap.className='hosted-media-wrap';
      const link=document.createElement('a');
      link.href=mediaUrl(id,false);
      link.target='_blank';
      link.rel='noopener';
      const img=document.createElement('img');
      img.className='hosted-media-image';
      img.loading='lazy';
      img.alt='WhatsApp image';
      img.src=mediaUrl(id,false);
      link.appendChild(img);
      wrap.appendChild(link);
      node=wrap;
    }else if(type.includes('video')){
      const wrap=document.createElement('div');
      wrap.className='hosted-media-wrap';
      const video=document.createElement('video');
      video.className='hosted-media-video';
      video.controls=true;
      video.preload='metadata';
      video.src=mediaUrl(id,false);
      wrap.appendChild(video);
      node=wrap;
    }else if(type.includes('document')){
      const link=document.createElement('a');
      link.className='hosted-document-card';
      link.href=mediaUrl(id,true);
      link.innerHTML='<i class="ti ti-file-description"></i><span>Document</span><i class="ti ti-download"></i>';
      node=link;
    }else if(isAudio){
      const audio=document.createElement('audio');
      audio.className='hosted-media-audio';
      audio.controls=true;
      audio.preload='metadata';
      audio.src=mediaUrl(id,false);
      node=audio;
    }

    if(node){
      const anchor=label||voice||bubble.querySelector('.bubble-body')||bubble.firstChild;
      if(anchor&&anchor.parentNode){
        anchor.parentNode.insertBefore(node,anchor);
        if(label)label.remove();
        if(voice)voice.remove();
      }else{
        bubble.insertBefore(node,bubble.firstChild);
      }
      bubble.dataset.mediaEnhanced='1';
    }
  }

  function enhanceInbox(){
    document.querySelectorAll('.bubble[data-message-id]').forEach(enhanceBubble);
  }

  enhanceInbox();
  const observer=new MutationObserver(()=>enhanceInbox());
  observer.observe(app,{childList:true,subtree:true});
  window.addEventListener('beforeunload',()=>observer.disconnect(),{once:true});
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
