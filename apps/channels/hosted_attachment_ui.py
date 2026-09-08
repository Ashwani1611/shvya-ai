"""Progressively enhance the Hosted chat composer and media presentation.

Keeping this as a wrapper avoids replacing the realtime Hosted inbox template or
its scrolling/socket behavior. It adds authenticated photo/video/document
sending and on-demand rendering for media already present in WhatsApp.
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
.hosted-thread-empty{background:#f0f2f5!important;border-bottom:6px solid #25d366;color:#54656f!important}
.hosted-empty-copy{max-width:520px;padding:32px;text-align:center}
.hosted-empty-copy .hosted-empty-icon{font-size:82px;line-height:1;color:#c8d0d5}
.hosted-empty-copy h2{margin-top:22px;font-size:30px;font-weight:300;letter-spacing:-.5px;color:#41525d}
.hosted-empty-copy p{margin:12px auto 0;max-width:470px;font-size:14px;line-height:1.55;color:#667781}
.hosted-conversation-avatar{width:49px;height:49px;flex:0 0 49px;display:flex;align-items:center;justify-content:center;border-radius:999px;background:#dfe5e7;color:#60727d;font-size:18px;font-weight:600;text-transform:uppercase;overflow:hidden}
.hosted-media-image{display:block;width:auto;max-width:min(360px,100%);max-height:420px;border-radius:7px;object-fit:cover;background:#d9dde1}
.hosted-media-video{display:block;width:min(420px,100%);max-height:430px;border-radius:7px;background:#111}
.hosted-media-audio{display:block;width:min(330px,100%);min-width:250px}
.hosted-document-card{display:flex;min-width:250px;max-width:360px;align-items:center;gap:11px;border-radius:7px;background:rgba(0,0,0,.06);padding:11px 12px;color:#334155;text-decoration:none}
.hosted-document-card:hover{background:rgba(0,0,0,.09)}
.hosted-document-card i{font-size:27px;flex:0 0 auto}
.hosted-document-card span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600}
.hosted-media-wrap{margin-bottom:5px}
.hosted-media-download{display:inline-flex;align-items:center;gap:5px;margin-top:5px;font-size:11px;color:#54656f;text-decoration:none}
.hosted-media-download:hover{text-decoration:underline}
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

  const empty=document.getElementById('thread-empty');
  if(empty){
    empty.innerHTML=''
      +'<div class="hosted-empty-copy">'
      +'<div class="hosted-empty-icon"><i class="ti ti-brand-whatsapp"></i></div>'
      +'<h2>Your conversations</h2>'
      +'<p>Select a chat from the left to read messages and manage the lead without leaving the inbox.</p>'
      +'</div>';
  }

  function mediaUrl(messageId,download){
    const base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
    return base+'media/'+encodeURIComponent(messageId)+'/'+(download?'?download=1':'');
  }

  function enhanceBubble(bubble){
    if(!bubble||bubble.dataset.mediaEnhanced==='1')return;
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
      video.preload='none';
      video.src=mediaUrl(id,false);
      wrap.appendChild(video);
      node=wrap;
    }else if(type.includes('document')){
      const link=document.createElement('a');
      link.className='hosted-document-card';
      link.href=mediaUrl(id,true);
      link.innerHTML='<i class="ti ti-file-description"></i><span>Download document</span><i class="ti ti-download"></i>';
      node=link;
    }else if(isAudio){
      const audio=document.createElement('audio');
      audio.className='hosted-media-audio';
      audio.controls=true;
      audio.preload='none';
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
      if(!type.includes('document')){
        const download=document.createElement('a');
        download.className='hosted-media-download';
        download.href=mediaUrl(id,true);
        download.innerHTML='<i class="ti ti-download"></i><span>Download</span>';
        node.insertAdjacentElement('afterend',download);
      }
      bubble.dataset.mediaEnhanced='1';
    }
  }

  function enhanceConversations(){
    document.querySelectorAll('.hosted-conversation').forEach(row=>{
      if(row.dataset.avatarEnhanced==='1')return;
      const flex=row.querySelector('.flex');
      const name=String(row.querySelector('.hosted-conversation-name')?.textContent||'').trim();
      if(!flex)return;
      const avatar=document.createElement('div');
      avatar.className='hosted-conversation-avatar';
      avatar.textContent=(name&&name[0])?name[0]:'?';
      flex.insertBefore(avatar,flex.firstChild);
      row.dataset.avatarEnhanced='1';
    });
  }

  function enhanceInbox(){
    document.querySelectorAll('.bubble[data-message-id]').forEach(enhanceBubble);
    enhanceConversations();
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
