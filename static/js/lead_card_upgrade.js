(() => {
  'use strict';
  if (window.shvyaLeadPickers) return;
  window.shvyaLeadPickers = true;
  const selector = '.lead-card select[name="stage"], #pipeline-select, #modal-root select[name="pipeline"], #modal-root select[name="stage"]';
  function enhance(root = document) {
    root.querySelectorAll(selector).forEach(select => {
      if (select.dataset.applePicker) return;
      select.dataset.applePicker = '1';
      const button = document.createElement('button');
      button.type = 'button'; button.className = select.className + ' lead-picker-trigger';
      button.setAttribute('aria-haspopup', 'dialog');
      const title = select.name === 'stage' ? 'Choose a stage' : 'Choose a pipeline';
      button.setAttribute('aria-label', title);
      const sync = () => { button.textContent = select.selectedOptions[0]?.textContent || title; button.disabled = select.disabled; };
      sync(); select.hidden = true; select.after(button);
      select.addEventListener('change', sync);
      new MutationObserver(sync).observe(select, {childList:true, subtree:true, attributes:true});
      button.addEventListener('click', () => open(select, button, title, sync));
    });
  }
  function open(select, trigger, title, sync) {
    const original = select.value;
    let chosen = original, pending = false, submitted = false;
    const dialog = document.createElement('dialog');
    dialog.className = 'lead-picker-dialog';
    dialog.innerHTML = '<header><span class="lead-picker-eyebrow">WORKSPACE</span><h2></h2><p>Find the right place for your next step.</p></header><input type="search" placeholder="Search…" aria-label="Search choices"><div class="lead-picker-options" role="radiogroup"></div><p class="lead-picker-empty" hidden>No matching options.</p><p class="lead-picker-error" role="alert"></p><footer><button type="button" data-cancel>Cancel</button><button type="button" data-apply>Apply</button></footer>';
    dialog.querySelector('h2').textContent = title;
    const headingId = 'lead-picker-heading'; dialog.querySelector('h2').id = headingId;
    dialog.setAttribute('aria-labelledby', headingId);
    const search = dialog.querySelector('input'), list = dialog.querySelector('[role="radiogroup"]');
    list.setAttribute('aria-label', title);
    const apply = dialog.querySelector('[data-apply]'), cancel = dialog.querySelector('[data-cancel]');
    const error = dialog.querySelector('[role="alert"]');
    const options = Array.from(select.options).filter(o => !o.disabled && !(o.parentElement.tagName === 'OPTGROUP' && o.parentElement.disabled));
    function draw() {
      list.replaceChildren();
      const visible = options.filter(o => o.textContent.toLocaleLowerCase().includes(search.value.toLocaleLowerCase()));
      visible.forEach(o => {
        const row = document.createElement('button'); row.type = 'button'; row.setAttribute('role', 'radio');
        row.setAttribute('aria-checked', String(o.value === chosen)); row.dataset.value = o.value;
        const label = document.createElement('span'); label.textContent = o.textContent;
        const check = document.createElement('span'); check.textContent = o.value === chosen ? '✓' : ''; check.setAttribute('aria-hidden', 'true');
        row.append(label, check); row.disabled = pending;
        row.addEventListener('click', () => { chosen = o.value; draw(); Array.from(list.children).find(n => n.dataset.value === chosen)?.focus(); });
        list.append(row);
      });
      dialog.querySelector('.lead-picker-empty').hidden = visible.length > 0;
      apply.disabled = pending || chosen === original || !visible.some(o => o.value === chosen);
    }
    function cleanup() {
      document.removeEventListener('htmx:beforeRequest', before);
      document.removeEventListener('htmx:afterRequest', after);
      dialog.remove(); if (trigger.isConnected) trigger.focus();
    }
    function relevant(event) { return event.detail?.elt === select || event.detail?.elt === select.form; }
    function before(event) { if (submitted && relevant(event)) { pending = true; apply.textContent = 'Saving…'; cancel.disabled = true; search.disabled = true; draw(); } }
    function after(event) {
      if (!submitted || !relevant(event)) return;
      pending = false;
      if (event.detail.successful) { dialog.close(); }
      else { select.value = original; sync(); submitted = false; apply.textContent = 'Apply'; cancel.disabled = false; search.disabled = false; error.textContent = 'Could not save. Your previous selection is unchanged. Please retry.'; draw(); }
    }
    document.addEventListener('htmx:beforeRequest', before);
    document.addEventListener('htmx:afterRequest', after);
    dialog.addEventListener('close', cleanup, {once:true});
    dialog.addEventListener('cancel', e => { if (pending) e.preventDefault(); });
    cancel.addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', e => { if (e.target === dialog && !pending) { const r=dialog.getBoundingClientRect(); if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom) dialog.close(); } });
    search.addEventListener('input', draw);
    dialog.addEventListener('keydown', e => {
      if (pending || !['ArrowDown','ArrowUp','Home','End'].includes(e.key)) return;
      const rows = Array.from(list.children); if (!rows.length) return;
      if ((e.key === 'Home' || e.key === 'End') && e.target === search) return;
      e.preventDefault(); const i = rows.indexOf(document.activeElement);
      const next = e.key === 'Home' ? 0 : e.key === 'End' ? rows.length-1 : (i + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
      rows[next].focus();
    });
    apply.addEventListener('click', () => {
      if (pending || apply.disabled || !select.isConnected) return;
      error.textContent = ''; submitted = true; select.value = chosen; sync();
      select.dispatchEvent(new Event('change', {bubbles:true}));
      if (!pending) dialog.close();
    });
    document.body.append(dialog); draw(); dialog.showModal(); search.focus();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => enhance()); else enhance();
  document.addEventListener('htmx:afterSwap', () => enhance());
})();
