(() => {
  const modal = document.getElementById('account-settings-modal');
  const form = document.getElementById('account-settings-form');
  if (!modal || !form) return;
  const error = document.getElementById('account-settings-error');
  const save = form.querySelector('[type="submit"]');
  let url = '', opener;
  const bools = ['ai_auto_reply', 'auto_lead_creation', 'bump_up_messages', 'auto_follow_up'];
  const values = ['bump_up_count', 'business_hours_start', 'business_hours_end', 'active_conversation_delay_value', 'active_conversation_delay_unit'];
  [...form.querySelectorAll('input:not([type=hidden]), select')].forEach(el => el.setAttribute('aria-label', el.name.replaceAll('_', ' ')));
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.setAttribute('aria-label', 'Instagram Automation Settings');
  function close() { modal.classList.remove('is-open'); modal.setAttribute('aria-hidden', 'true'); opener?.focus(); }
  function showError(message) { error.textContent = message; error.classList.remove('hidden'); }
  document.querySelectorAll('[data-instagram-settings-url]').forEach(button => button.addEventListener('click', async () => {
    opener = button; url = button.dataset.instagramSettingsUrl;
    modal.classList.add('is-open'); modal.setAttribute('aria-hidden', 'false');
    error.classList.add('hidden'); save.disabled = true;
    [...form.elements].forEach(el => { if (el.name && el.type !== 'hidden') el.disabled = true; });
    document.getElementById('account-settings-label').textContent = 'Instagram · Applies to this account only';
    form.querySelector('[data-close-settings]').focus();
    try {
      const response = await fetch(url, {headers: {'Accept': 'application/json'}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not load settings.');
      bools.forEach(key => { form.elements[key].checked = !!data.settings[key]; });
      values.forEach(key => { form.elements[key].value = data.settings[key]; });
      [...form.elements].forEach(el => { el.disabled = false; });
    } catch (e) { showError(e.message); }
  }));
  modal.querySelectorAll('[data-close-settings]').forEach(button => button.addEventListener('click', close));
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  modal.addEventListener('keydown', e => {
    if (e.key === 'Escape') close();
    if (e.key !== 'Tab') return;
    const nodes = [...form.querySelectorAll('button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled)')];
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  form.addEventListener('submit', async e => {
    e.preventDefault(); if (save.disabled) return;
    save.disabled = true; error.classList.add('hidden');
    const payload = {};
    bools.forEach(key => { payload[key] = form.elements[key].checked; });
    values.forEach(key => { payload[key] = form.elements[key].value; });
    try {
      const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': form.elements.csrfmiddlewaretoken.value}, body: JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not save settings.');
      close(); window.shvyaToast?.('Instagram automation settings saved.', 'success');
    } catch (e) { showError(e.message); }
    finally { save.disabled = false; }
  });
})();
