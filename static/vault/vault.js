/* Progressive enhancement: every Vault section and form works without JavaScript.
 * Private input remains in this document only; never persist it in browser storage.
 */
(() => {
  'use strict';
  const root = document.querySelector('[data-vault-workspace]');
  const panels = root ? Array.from(root.querySelectorAll('[data-vault-panel]')) : [];
  const links = root ? Array.from(root.querySelectorAll('[data-section-link]')) : [];
  const select = root?.querySelector('[data-section-select]');
  const steps = root?.querySelector('[data-step-navigation]');
  let active = '';
  let toastTimeout;
  const toast = (message) => {
    const target = document.getElementById('vault-toast');
    if (!target) return;
    clearTimeout(toastTimeout);
    target.textContent = message;
    target.hidden = false;
    toastTimeout = setTimeout(() => { target.hidden = true; }, 4000);
  };
  const hashKey = () => {
    try { return decodeURIComponent(window.location.hash.slice(1)); }
    catch (_) { return ''; }
  };
  const hasPanel = (key) => panels.some(panel => panel.dataset.vaultPanel === key);
  const showPanel = (key, { focus = false, updateHash = false } = {}) => {
    if (!hasPanel(key)) key = panels[0]?.dataset.vaultPanel;
    if (!key) return;
    active = key;
    for (const panel of panels) panel.hidden = panel.dataset.vaultPanel !== key;
    for (const link of links) {
      const selected = link.dataset.sectionLink === key;
      link.classList.toggle('is-active', selected);
      if (selected) link.setAttribute('aria-current', 'step');
      else link.removeAttribute('aria-current');
    }
    if (select) select.value = key;
    if (steps) {
      steps.hidden = false;
      const index = panels.findIndex(panel => panel.dataset.vaultPanel === key);
      steps.querySelector('[data-step="-1"]').disabled = index === 0;
      steps.querySelector('[data-step="1"]').disabled = index === panels.length - 1;
      steps.querySelector('[data-step-label]').textContent = `${index + 1} of ${panels.length}`;
    }
    if (updateHash && hashKey() !== key) {
      try { window.history.pushState(null, '', `#${encodeURIComponent(key)}`); }
      catch (_) { window.location.hash = key; }
    }
    if (focus) {
      const heading = document.getElementById(`heading-${key}`);
      heading?.focus({ preventScroll: true });
      const panel = panels.find(item => item.dataset.vaultPanel === key);
      panel?.scrollIntoView({ behavior: 'instant', block: 'start' });
    }
  };
  if (panels.length) {
    root.classList.add('is-enhanced');
    showPanel(hashKey());
    for (const link of links) {
      link.addEventListener('click', (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        showPanel(link.dataset.sectionLink, { focus: true, updateHash: true });
      });
    }
    const navLinks = Array.from(root.querySelectorAll('.vault-section-nav [data-section-link]'));
    for (const [index, link] of navLinks.entries()) {
      link.addEventListener('keydown', (event) => {
        let next;
        if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = (index + 1) % navLinks.length;
        if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = (index - 1 + navLinks.length) % navLinks.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = navLinks.length - 1;
        if (next === undefined) return;
        event.preventDefault();
        navLinks[next].focus();
      });
    }
    select?.addEventListener('change', () => showPanel(select.value, { focus: true, updateHash: true }));
    steps?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-step]');
      if (!button || button.disabled) return;
      const index = panels.findIndex(panel => panel.dataset.vaultPanel === active);
      const panel = panels[index + Number(button.dataset.step)];
      if (panel) showPanel(panel.dataset.vaultPanel, { focus: true, updateHash: true });
    });
    window.addEventListener('hashchange', () => showPanel(hashKey(), { focus: true }));
    window.addEventListener('popstate', () => showPanel(hashKey(), { focus: true }));
  }

  document.querySelectorAll('[data-copy]').forEach(button => {
    button.addEventListener('click', async () => {
      const value = button.dataset.copy || '';
      try {
        if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
        else {
          const input = document.createElement('textarea');
          input.value = value;
          input.setAttribute('readonly', '');
          input.setAttribute('aria-label', 'Value to copy');
          input.style.position = 'fixed';
          input.style.opacity = '0';
          document.body.appendChild(input);
          input.select();
          let copied = false;
          try { copied = document.execCommand('copy'); }
          finally { input.remove(); button.focus(); }
          if (!copied) throw new Error('Clipboard unavailable');
        }
        toast(button.dataset.copyLabel || 'Copied');
      } catch (_) { toast('Clipboard is unavailable. Select and copy the visible link or code.'); }
    });
  });
  document.querySelectorAll('[data-open-details]').forEach(link => {
    link.addEventListener('click', () => {
      const target = document.getElementById(link.dataset.openDetails);
      if (!target) return;
      target.open = true;
      target.querySelector('input:not([type="hidden"]), select, textarea')?.focus();
    });
  });
  document.querySelectorAll('[data-filter-select]').forEach(input => {
    const target = document.getElementById(input.dataset.filterSelect);
    if (!target) return;
    const options = Array.from(target.options).map(option => ({ value: option.value, text: option.text }));
    input.addEventListener('input', () => {
      const query = input.value.trim().toLocaleLowerCase();
      const selected = target.value;
      const matching = options.filter(option => !option.value || option.text.toLocaleLowerCase().includes(query) || option.value === selected);
      target.replaceChildren(...matching.map(option => new Option(option.text, option.value, false, option.value === selected)));
    });
  });
  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', (event) => {
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
      }
      // URL fragments remain local to the browser and help retain the active section.
      if (root && root.contains(form) && active) {
        const action = new URL(form.action, window.location.href);
        action.hash = active;
        form.action = action.href;
      }
    });
  });

  // A failed server validation may return the posted fields. Restore only the
  // corresponding form, using values/textContent; never execute or store markup.
  const failedData = document.getElementById('vault-form-data');
  if (failedData) {
    try {
      const raw = JSON.parse(failedData.textContent);
      const data = Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, Array.isArray(value) ? value[value.length - 1] : value]));
      const form = Array.from(document.querySelectorAll('form')).find(candidate => {
        if (candidate.elements.namedItem('action')?.value !== data.action) return false;
        for (const key of ['section', 'entry_id', 'question_id']) {
          if (data[key] && candidate.elements.namedItem(key)?.value !== data[key]) return false;
        }
        const kind = candidate.elements.namedItem('kind');
        return !data.kind || kind?.value === data.kind || (data.kind === 'audio' && kind?.tagName === 'SELECT');
      });
      if (form) {
        const allowed = new Set(['body', 'url', 'send_when', 'text', 'answer', 'title', 'date', 'duration_min', 'attendees', 'summary', 'origin', 'source_date', 'kind', 'allowed_for_ai_sharing', 'share_recording']);
        for (const input of Array.from(form.elements)) {
          if (!allowed.has(input.name) || input.type === 'file' || input.type === 'hidden') continue;
          if (input.type === 'checkbox') input.checked = data[input.name] === 'on';
          else if (data[input.name] !== undefined) input.value = String(data[input.name]);
        }
        let ancestor = form.closest('details');
        while (ancestor) { ancestor.open = true; ancestor = ancestor.parentElement?.closest('details'); }
        const panel = form.closest('[data-vault-panel]');
        if (panel) showPanel(panel.dataset.vaultPanel, { updateHash: true });
      }
      document.getElementById('vault-form-error')?.focus();
    } catch (_) { /* The server-rendered error and all forms remain available. */ }
    failedData.remove();
  }

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SpeechRecognition) {
    let running = null;
    document.querySelectorAll('[data-dictation-controls]').forEach(controls => {
      controls.hidden = false;
      const button = controls.querySelector('[data-dictate]');
      const textarea = document.getElementById(button.dataset.dictate);
      if (!textarea) return;
      button.addEventListener('click', () => {
        if (running) { running.stop(); return; }
        const recognition = new SpeechRecognition();
        recognition.lang = controls.querySelector('[data-dictation-language]')?.value || 'en-IN';
        recognition.continuous = false;
        recognition.interimResults = false;
        recognition.onstart = () => {
          running = recognition;
          button.textContent = 'Stop dictation';
          button.setAttribute('aria-pressed', 'true');
          toast('Listening. Speak your note, then review the text before saving.');
        };
        recognition.onresult = event => {
          let transcript = '';
          for (let i = event.resultIndex; i < event.results.length; i += 1) {
            if (event.results[i].isFinal) transcript += `${event.results[i][0].transcript} `;
          }
          const text = [textarea.value.trimEnd(), transcript.trim()].filter(Boolean).join('\n');
          const limit = textarea.maxLength > 0 ? textarea.maxLength : text.length;
          textarea.value = text.slice(0, limit);
          textarea.dispatchEvent(new Event('input', { bubbles: true }));
          toast(text.length > limit ? 'Text limit reached. Review your note before saving.' : 'Dictation added. Review your note, then select Save.');
        };
        recognition.onerror = () => toast('Dictation is unavailable. You can type your note or upload an audio recording.');
        recognition.onend = () => {
          running = null;
          button.textContent = 'Dictate note';
          button.setAttribute('aria-pressed', 'false');
        };
        try { recognition.start(); }
        catch (_) { toast('Dictation could not start. You can type your note instead.'); }
      });
    });
    window.addEventListener('pagehide', () => running?.abort());
  }
})();
