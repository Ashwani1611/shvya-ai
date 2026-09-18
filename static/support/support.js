/* Progressive enhancement only; authorization and ticket rules stay in Django. */
(() => {
  "use strict";
  const app = document.querySelector(".sp-app");
  if (!app) return;
  const csrf = () => app.querySelector('[name="csrfmiddlewaretoken"]')?.value || "";
  let lastFocus = null;
  app.addEventListener("click", async (event) => {
    const open = event.target.closest("[data-open-dialog]");
    if (open) {
      const dialog = document.getElementById(open.dataset.openDialog);
      if (dialog?.showModal) { lastFocus = open; dialog.showModal(); }
    }
    const close = event.target.closest("[data-close-dialog]");
    if (close) {
      const dialog = close.closest("dialog");
      if (dialog) dialog.close();
      else { const back = app.querySelector(".sp-back"); if (back) window.location.assign(back.href); }
    }
    const copy = event.target.closest("[data-copy-target]");
    if (copy) {
      const field = document.getElementById(copy.dataset.copyTarget);
      if (!field) return;
      try { await navigator.clipboard.writeText(field.value); copy.textContent = "Link copied ✓"; }
      catch { field.focus(); field.select(); copy.textContent = "Select and copy the link above"; }
    }
  });
  app.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("close", () => lastFocus?.focus());
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
  });
  // Dependent issue choices never contain HTML. Server validates the relationship again.
  let issues = [];
  const issueScript = document.getElementById("sp-issues");
  try { issues = JSON.parse(issueScript?.textContent || "[]"); } catch { /* Server-rendered choices still work. */ }
  app.querySelectorAll("[data-issue-form]").forEach((form) => {
    const category = form.querySelector('[name="category"]');
    const issue = form.querySelector('[name="issue"]');
    if (!category || !issue || !issues.length) return;
    const rebuild = () => {
      const selected = issue.value;
      issue.replaceChildren(new Option("Choose an issue", ""));
      issues.filter((row) => String(row.category_id) === category.value).forEach((row) => issue.add(new Option(row.name, String(row.id))));
      if (Array.from(issue.options).some((option) => option.value === selected)) issue.value = selected;
      issue.disabled = !category.value;
    };
    category.addEventListener("change", rebuild); rebuild();
  });
  app.querySelectorAll("[data-attachments]").forEach((form) => {
    const input = form.querySelector('input[type="file"]');
    const feedback = form.querySelector(".sp-upload-feedback");
    if (!input) return;
    const verify = () => {
      const files = Array.from(input.files || []);
      const maxFiles = Number(form.dataset.maxFiles || 8), maxMB = Number(form.dataset.maxMb || 25);
      const error = files.length > maxFiles ? `Choose no more than ${maxFiles} files.` : files.some((file) => file.size > maxMB*1024*1024) ? `Each file must be smaller than ${maxMB} MB.` : "";
      input.setCustomValidity(error);
      if (feedback) feedback.textContent = error || (files.length ? `${files.length} file${files.length === 1 ? "" : "s"} ready to attach.` : "");
      return !error;
    };
    input.addEventListener("change", verify);
    form.addEventListener("submit", (event) => { if (!verify()) { event.preventDefault(); input.reportValidity(); } });
    // Microphone permission is requested only after an explicit click. Recording
    // stays local until the ticket form is submitted, and never survives closing it.
    if (window.MediaRecorder && navigator.mediaDevices?.getUserMedia && window.DataTransfer) {
      const tools = document.createElement("div"); tools.className = "sp-audio-tools";
      const record = document.createElement("button"); record.type = "button"; record.className = "sp-button sp-button--small"; record.dataset.recordAudio = ""; record.textContent = "Record voice note";
      const hint = document.createElement("small"); hint.textContent = "Up to 2 minutes · microphone access is optional";
      tools.append(record, hint); input.parentElement.append(tools);
      let recorder, stream, cap, requesting = false, generation = 0;
      const stop = () => { generation += 1; if (recorder?.state === "recording") recorder.stop(); stream?.getTracks().forEach((track) => track.stop()); clearTimeout(cap); };
      record.addEventListener("click", async () => {
        if (recorder?.state === "recording") { stop(); return; }
        if (requesting) return;
        requesting = true; record.disabled = true;
        const attempt = ++generation;
        try {
          const acquired = await navigator.mediaDevices.getUserMedia({audio:true});
          if (attempt !== generation || !form.isConnected || document.hidden) { acquired.getTracks().forEach((track) => track.stop()); return; }
          stream = acquired;
          const mime = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"].find((type) => MediaRecorder.isTypeSupported(type));
          recorder = new MediaRecorder(stream, mime ? {mimeType:mime} : {});
          const chunks = [];
          recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
          recorder.onstop = () => {
            stream?.getTracks().forEach((track) => track.stop()); clearTimeout(cap);
            const kind = recorder.mimeType || "audio/webm";
            const extension = kind.includes("ogg") ? "ogg" : kind.includes("mp4") ? "m4a" : "webm";
            const file = new File(chunks, `voice-note-${Date.now()}.${extension}`, {type:kind});
            const transfer = new DataTransfer(); Array.from(input.files || []).forEach((f) => transfer.items.add(f));
            if (file.size) transfer.items.add(file); input.files = transfer.files; verify();
            record.textContent = "Record another voice note"; record.classList.remove("is-recording"); hint.textContent = "Recording attached. It will be uploaded only when you submit.";
          };
          recorder.start(); record.textContent = "● Stop recording"; record.classList.add("is-recording"); hint.textContent = "Recording… click Stop when you’re done.";
          cap = setTimeout(stop, 120000);
        } catch { stream?.getTracks().forEach((track) => track.stop()); hint.textContent = "Microphone unavailable. You can upload an audio file instead."; }
        finally { requesting = false; record.disabled = false; }
      });
      form.addEventListener("submit", (event) => {
        if (requesting || recorder?.state === "recording") { event.preventDefault(); stop(); hint.textContent = "Review your attachments, then submit again."; }
      });
      form.closest("dialog")?.addEventListener("close", stop);
      window.addEventListener("pagehide", stop);
      document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
    }
  });
  const replyForm = app.querySelector("[data-reply-form]");
  if (replyForm) {
    const body = replyForm.querySelector('[name="body"]');
    const note = replyForm.querySelector('[name="internal"]');
    const warning = replyForm.querySelector("[data-internal-warning]");
    const send = replyForm.querySelector("[data-send-label]");
    const status = replyForm.querySelector('[name="status"]');
    const saved = replyForm.querySelector("[data-saved-reply]");
    const setNote = () => {
      if (warning) warning.hidden = !note?.checked;
      if (send) send.textContent = note?.checked ? "Add internal note" : "Send reply ↗";
      if (status) status.disabled = !!note?.checked;
    };
    note?.addEventListener("change", setNote); setNote();
    saved?.addEventListener("change", () => {
      const option = saved.selectedOptions[0]; if (!option?.value || !body) return;
      const text = [option.dataset.body, option.dataset.link].filter(Boolean).join("\n\n");
      const start = body.selectionStart ?? body.value.length, end = body.selectionEnd ?? start;
      body.setRangeText(text, start, end, "end"); body.focus(); saved.value = "";
    });
    let lastTyping = 0;
    body?.addEventListener("input", () => {
      if (!app.dataset.typingUrl || Date.now() - lastTyping < 6000) return;
      lastTyping = Date.now();
      fetch(app.dataset.typingUrl, {method:"POST", credentials:"same-origin", headers:{"X-CSRFToken":csrf()}}).catch(() => {});
    });
  }
  const bulk = app.querySelector("[data-bulk-form]");
  if (bulk) {
    const all = bulk.querySelector("[data-select-all]"); const items = Array.from(bulk.querySelectorAll('[name="tickets"]'));
    const update = () => { const count=items.filter((i)=>i.checked).length; bulk.querySelector("[data-selection-count]").textContent=`${count} selected`; if(all) { all.checked=items.length>0&&count===items.length; all.indeterminate=count>0&&count<items.length; } };
    all?.addEventListener("change", () => { items.forEach((i)=>{ i.checked=all.checked; }); update(); }); items.forEach((i)=>i.addEventListener("change",update));
    bulk.addEventListener("submit",(event)=>{if(!items.some((i)=>i.checked)){event.preventDefault();bulk.querySelector("[data-selection-count]").textContent="Select at least one ticket.";}});
    const action=bulk.querySelector('[name="action"]'); const setAction=()=>{["status","priority"].forEach((name)=>{const input=bulk.querySelector(`[name="${name}"]`); input.hidden=action.value!==name; input.disabled=action.value!==name;});}; action.addEventListener("change",setAction);setAction();
  }
  // Small state checks: never reload a draft, replace the thread, or move the scroll position.
  if (app.dataset.ticketState) {
    let timer, delay=5000, stopped=false;
    const schedule=()=>{clearTimeout(timer);if(!stopped&&!document.hidden)timer=setTimeout(poll,delay);};
    const poll=async()=>{
      if(document.hidden||stopped)return;
      try{
        const response=await fetch(app.dataset.ticketState,{credentials:"same-origin",cache:"no-store",headers:{"Accept":"application/json"}});
        if(response.status===401||response.status===403||response.status===404){stopped=true;return;}
        if(!response.ok||!response.headers.get("content-type")?.includes("application/json"))throw new Error("StateUnavailable");
        const state=await response.json();delay=5000;
        if(state.public_messages!==Number(app.dataset.publicCount)||state.status!==app.dataset.status||(state.version!==undefined&&state.version!==Number(app.dataset.ticketVersion))){const banner=app.querySelector("[data-new-activity]");if(banner)banner.hidden=false;}
        const typing=app.querySelector("[data-typing-indicator]");if(typing)typing.textContent=(state.typing||[]).length?`${state.typing.slice(0,3).join(", ")} typing…`:"";
      }catch{delay=Math.min(delay*2,60000);}finally{schedule();}
    };
    document.addEventListener("visibilitychange",()=>{if(document.hidden)clearTimeout(timer);else schedule();});window.addEventListener("pagehide",()=>{stopped=true;clearTimeout(timer);});schedule();
  }
})();
