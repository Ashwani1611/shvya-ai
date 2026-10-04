(() => {
  'use strict';
  const root = document.getElementById('capi-app');
  if (!root) return;
  const $ = id => document.getElementById(id);
  let state = JSON.parse($('capi-initial-state').textContent);
  const endpoint = root.dataset.endpoint;
  const csrf = $('capi-settings').querySelector('[name=csrfmiddlewaretoken]').value;
  const mappingForm = $('capi-mapping-form');
  const testForm = $('capi-test-form');
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function options(select, values, placeholder) {
    select.replaceChildren();
    if (placeholder) select.append(new Option(placeholder, ''));
    values.forEach(([value, label]) => select.append(new Option(label, value)));
  }
  function notice(message, error = false) {
    const target = $('capi-notice');
    target.textContent = message;
    target.classList.toggle('is-error', error);
    target.hidden = !message;
  }
  async function request(data, errorTarget) {
    if (errorTarget) errorTarget.hidden = true;
    const response = await fetch(endpoint, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(data),
    });
    let result;
    try { result = await response.json(); }
    catch { throw new Error('Your session may have expired. Refresh the page and try again.'); }
    if (!response.ok) throw new Error(result.error || 'The change could not be saved. Try again.');
    state = result.state;
    renderState(data.action === 'save_settings' || data.action === 'disconnect');
    notice(result.message);
    return result;
  }
  async function run(form, errorTarget, task) {
    const buttons = [...form.querySelectorAll('button[type=submit]')];
    buttons.forEach(button => { button.disabled = true; button.setAttribute('aria-busy', 'true'); });
    try { await task(); }
    catch (error) {
      const message = error.message || 'Could not connect. Try again.';
      if (errorTarget) { errorTarget.textContent = message; errorTarget.hidden = false; }
      else notice(message, true);
    } finally {
      buttons.forEach(button => { button.disabled = false; button.removeAttribute('aria-busy'); });
    }
  }
  function renderFields() {
    const config = state.configuration;
    const groups = [
      [$('capi-user-fields'), Object.entries(state.fields), config.user_data_fields, 'user_data_fields'],
      [$('capi-custom-fields'), state.attributes.map(a => [a.key, a.name]), config.custom_attribute_keys, 'custom_attribute_keys'],
    ];
    groups.forEach(([container, fields, selected, name]) => {
      container.replaceChildren();
      if (!fields.length) container.append(element('p', 'No shareable custom attributes yet. Add attributes in your CRM to use them here.', 'capi-empty'));
      fields.forEach(([value, label]) => {
        const row = element('label', undefined, 'capi-check');
        const input = element('input');
        input.type = 'checkbox'; input.name = name; input.value = value;
        input.setAttribute('form', 'capi-settings'); input.checked = selected.includes(value);
        row.append(input, element('span', label)); container.append(row);
      });
    });
  }
  function renderSettings() {
    const c = state.configuration;
    $('capi-dataset').value = c.dataset_id;
    $('capi-scope').value = c.event_scope;
    $('capi-mode').value = c.test_mode ? 'test' : 'live';
    $('capi-test-code').value = c.test_event_code;
    $('capi-enabled').checked = c.is_enabled;
    $('capi-token').value = '';
    $('capi-token').type = 'password';
    $('capi-token-visibility').setAttribute('aria-label', 'Show access token');
    $('capi-token-visibility').setAttribute('aria-pressed', 'false');
    $('capi-token').placeholder = c.has_access_token ? 'Token saved · leave blank to keep it' : 'Paste your Conversions API token';
    $('capi-token-help').textContent = c.has_access_token ? 'Encrypted on the server. Your saved token is never returned to this page.' : 'Generate a token in Events Manager · Settings · Conversions API.';
    updateModeHelp(); renderFields();
  }
  function updateModeHelp() {
    $('capi-mode-help').textContent = $('capi-mode').value === 'test' ?
      'Test mode attaches your code to automatic events. Meta may still use these events for measurement.' :
      'Live events are sent without a Test Events code.';
  }
  function renderStatus() {
    const c = state.configuration;
    const verified = Boolean(c.verified_at);
    $('capi-credential-status').textContent = verified ? 'Verified with Meta' : c.has_access_token ? 'Credentials saved' : 'Not connected';
    $('capi-credential-status').classList.toggle('is-verified', verified);
    $('capi-tracking-title').textContent = !c.is_enabled ? 'Tracking paused.' : c.test_mode ? 'Test mode active.' : 'Live events enabled.';
    $('capi-tracking-description').textContent = c.is_enabled ?
      'Mapped lead changes are queued automatically. Delivery history appears below.' :
      'Configure your connection, map stages, and test before enabling automatic delivery.';
    const counts = state.counts;
    $('capi-count-sent').textContent = counts.sent || 0;
    $('capi-count-pending').textContent = (counts.queued || 0) + (counts.retrying || 0) + (counts.sending || 0);
    $('capi-count-failed').textContent = counts.failed || 0;
    const canTest = c.has_access_token && c.dataset_id && c.test_event_code && state.mappings.length;
    $('capi-open-test').disabled = !canTest;
    $('capi-test-help').textContent = canTest ? 'Uses a real lead and your saved Test Events code.' : 'Save a dataset, token and Test Events code to enable testing.';
    $('capi-disconnect').hidden = !c.has_access_token;
  }
  function iconButton(icon, label, action) {
    const button = element('button', undefined, 'capi-icon-button');
    button.type = 'button'; button.title = label; button.setAttribute('aria-label', label);
    const glyph = element('i', undefined, `ti ti-${icon}`); glyph.setAttribute('aria-hidden', 'true');
    button.append(glyph); button.addEventListener('click', action); return button;
  }
  function renderMappings() {
    const rows = $('capi-mapping-rows'); rows.replaceChildren();
    if (!state.mappings.length) {
      const row = element('tr'); const cell = element('td', 'Add a mapping to connect a CRM stage to a Meta event.', 'capi-empty');
      cell.colSpan = 5; row.append(cell); rows.append(row);
    }
    state.mappings.forEach(mapping => {
      const row = element('tr');
      const status = element('td'); const toggle = element('label', undefined, 'capi-switch');
      const input = element('input'); input.type = 'checkbox'; input.checked = mapping.is_enabled;
      input.dataset.mappingId = mapping.id;
      input.setAttribute('aria-label', `Send ${mapping.event_name} events for ${mapping.pipeline_name}, ${mapping.stage_name}`);
      input.addEventListener('change', async () => {
        input.disabled = true;
        try { await request({ ...mapping, action: 'save_mapping', is_enabled: input.checked }); }
        catch (error) { input.checked = mapping.is_enabled; input.disabled = false; notice(error.message, true); }
        rows.querySelector(`[data-mapping-id="${mapping.id}"]`)?.focus();
      });
      toggle.append(input, element('span')); status.append(toggle);
      const stage = element('td'); stage.append(element('div', mapping.stage_name), element('small', mapping.pipeline_name));
      const event = element('td'); const eventLabel = element('div', undefined, 'capi-event-label');
      eventLabel.append(element('span', mapping.event_name));
      if (mapping.is_default) { const lock = element('i', undefined, 'ti ti-lock capi-lock'); lock.setAttribute('aria-label', 'Default New Lead event'); eventLabel.append(lock); }
      event.append(eventLabel, element('small', mapping.is_default ? 'Default mapping' : state.sources[mapping.action_source] || 'CRM update'));
      const value = element('td');
      value.append(element('div', mapping.value_source === 'none' ? 'No value' : mapping.value_source === 'static' ? `${mapping.currency} ${Number(mapping.static_value).toLocaleString(undefined, { maximumFractionDigits: 4 })}` : `${mapping.currency} · ${state.numeric_attributes.find(a => a.key === mapping.value_attribute)?.name || mapping.value_attribute}`));
      if (mapping.value_source !== 'none') value.append(element('small', mapping.value_source === 'static' ? 'Static value' : 'From lead attribute'));
      const actionsCell = element('td'); const actions = element('div', undefined, 'capi-row-actions');
      actions.append(iconButton('edit', `Edit ${mapping.stage_name} mapping`, () => openMapping(mapping)));
      const testButton = iconButton('player-play', `Test ${mapping.event_name} event`, () => openTest(mapping.id));
      testButton.disabled = $('capi-open-test').disabled; actions.append(testButton);
      if (!mapping.is_default) actions.append(iconButton('trash', `Remove ${mapping.stage_name} mapping`, async () => {
        if (!window.confirm(`Remove the event mapping for ${mapping.stage_name}?`)) return;
        try { await request({ action: 'delete_mapping', id: mapping.id }); }
        catch (error) { notice(error.message, true); }
      }));
      actionsCell.append(actions); row.append(status, stage, event, value, actionsCell); rows.append(row);
    });
  }
  function renderDeliveries() {
    const container = $('capi-deliveries'); container.replaceChildren();
    if (!state.deliveries.length) { container.append(element('p', 'No events recorded yet. Send a test event to verify your connection.', 'capi-empty')); return; }
    const labels = { queued: 'Queued', sending: 'Sending', retrying: 'Retrying', sent: 'Accepted', failed: 'Failed', skipped: 'Skipped' };
    state.deliveries.forEach(delivery => {
      const row = element('div', undefined, 'capi-delivery'); const copy = element('div');
      const title = element('div', undefined, 'capi-delivery-title');
      title.append(element('span', delivery.event_name), element('span', delivery.is_test ? 'Test' : 'Live', 'capi-chip'));
      copy.append(title, element('div', `${delivery.lead_name} · ${new Date(delivery.created_at).toLocaleString()} · ${delivery.attempt_count} attempt${delivery.attempt_count === 1 ? '' : 's'}`, 'capi-delivery-copy'));
      if (delivery.trace_id) copy.append(element('div', `Meta trace: ${delivery.trace_id}`, 'capi-delivery-copy'));
      if (delivery.error_message) copy.append(element('p', delivery.error_message, 'capi-delivery-error'));
      const status = element('div', undefined, 'capi-delivery-state');
      status.append(element('span', labels[delivery.status] || delivery.status, `capi-chip is-${delivery.status}`));
      if (delivery.status === 'failed') {
        const retry = element('button', 'Retry', 'capi-text-button'); retry.type = 'button';
        retry.addEventListener('click', async () => { retry.disabled = true; try { await request({ action: 'retry_delivery', id: delivery.id }); } catch (error) { notice(error.message, true); retry.disabled = false; } });
        status.append(retry);
      }
      row.append(copy, status); container.append(row);
    });
  }
  function renderState(resetSettings = false) {
    if (resetSettings) renderSettings();
    renderStatus(); renderMappings(); renderDeliveries();
  }
  function updateStages(selected) {
    const pipeline = state.pipelines.find(p => p.id === mappingForm.elements.pipeline.value);
    options(mappingForm.elements.stage, (pipeline?.stages || []).map(s => [s.id, s.name]), 'Select stage');
    if (selected) mappingForm.elements.stage.value = selected;
  }
  function updateMappingFields() {
    const custom = $('capi-event-type').value === 'custom';
    $('capi-custom-event-field').hidden = !custom;
    mappingForm.elements.event_name.required = custom;
    if (!custom) mappingForm.elements.event_name.value = $('capi-event-type').value;
    const valueSource = mappingForm.elements.value_source.value;
    $('capi-static-field').hidden = valueSource !== 'static';
    $('capi-attribute-field').hidden = valueSource !== 'attribute';
    $('capi-currency-field').hidden = valueSource === 'none';
    mappingForm.elements.static_value.required = valueSource === 'static';
    mappingForm.elements.value_attribute.required = valueSource === 'attribute';
    mappingForm.elements.currency.required = valueSource !== 'none';
  }
  function openMapping(mapping) {
    mappingForm.reset(); $('capi-mapping-error').hidden = true;
    $('capi-mapping-title').textContent = mapping ? 'Edit your event mapping.' : 'Add an event mapping.';
    options(mappingForm.elements.pipeline, state.pipelines.map(p => [p.id, p.name]), 'Select pipeline');
    options(mappingForm.elements.action_source, Object.entries(state.sources));
    options(mappingForm.elements.value_attribute, state.numeric_attributes.map(a => [a.key, a.name]), 'Select numeric attribute');
    options(mappingForm.elements.currency, state.currencies.map(c => [c, c]), 'Select currency');
    mappingForm.elements.id.value = mapping?.id || '';
    mappingForm.elements.pipeline.value = mapping?.pipeline || '';
    updateStages(mapping?.stage);
    ['event_name', 'action_source', 'value_source', 'static_value', 'value_attribute', 'currency'].forEach(key => {
      mappingForm.elements[key].value = mapping?.[key] ?? (key === 'action_source' ? 'system_generated' : key === 'value_source' ? 'none' : '');
    });
    mappingForm.elements.is_enabled.checked = mapping ? mapping.is_enabled : true;
    const knownEvents = [...$('capi-event-type').options].map(o => o.value);
    $('capi-event-type').value = mapping ? (knownEvents.includes(mapping.event_name) ? mapping.event_name : 'custom') : 'Lead';
    const locked = Boolean(mapping?.is_default);
    mappingForm.elements.pipeline.disabled = locked; mappingForm.elements.stage.disabled = locked;
    $('capi-event-type').disabled = locked;
    updateMappingFields(); $('capi-mapping-dialog').showModal();
  }
  async function updateTestLeads() {
    const mappingId = testForm.elements.mapping_id.value;
    options(testForm.elements.lead_id, [], 'Loading eligible leads…');
    testForm.querySelector('button[type=submit]').disabled = true;
    try {
      const response = await fetch(`${endpoint}?lookup=leads&mapping_id=${encodeURIComponent(mappingId)}`, { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error('Could not load leads for this mapping. Try again.');
      const { leads } = await response.json();
      if (testForm.elements.mapping_id.value !== mappingId) return;
      options(testForm.elements.lead_id, leads.map(lead => [lead.id, lead.name]), leads.length ? 'Select a real CRM lead' : 'No eligible leads currently in this stage');
      testForm.querySelector('button[type=submit]').disabled = !leads.length;
    } catch (error) {
      if (testForm.elements.mapping_id.value !== mappingId) return;
      options(testForm.elements.lead_id, [], 'Could not load eligible leads');
      $('capi-test-error').textContent = error.message; $('capi-test-error').hidden = false;
    }
  }
  function openTest(mappingId) {
    if ($('capi-open-test').disabled) { notice('Save the connection and a Test Events code before testing.', true); return; }
    $('capi-test-error').hidden = true;
    options(testForm.elements.mapping_id, state.mappings.map(m => [m.id, `${m.pipeline_name} · ${m.stage_name} · ${m.event_name}`]));
    if (mappingId) testForm.elements.mapping_id.value = mappingId;
    updateTestLeads(); $('capi-test-dialog').showModal();
  }
  $('capi-settings').addEventListener('submit', event => {
    event.preventDefault();
    const form = $('capi-settings');
    const data = Object.fromEntries(new FormData(form));
    data.action = 'save_settings'; data.is_enabled = $('capi-enabled').checked;
    data.test_mode = $('capi-mode').value === 'test';
    data.user_data_fields = [...document.querySelectorAll('[name=user_data_fields]:checked')].map(input => input.value);
    data.custom_attribute_keys = [...document.querySelectorAll('[name=custom_attribute_keys]:checked')].map(input => input.value);
    run(form, null, () => request(data));
  });
  mappingForm.addEventListener('submit', event => {
    event.preventDefault();
    const data = { action: 'save_mapping', is_enabled: mappingForm.elements.is_enabled.checked };
    ['id', 'pipeline', 'stage', 'event_name', 'action_source', 'value_source', 'static_value', 'value_attribute', 'currency'].forEach(key => { data[key] = mappingForm.elements[key].value; });
    run(mappingForm, $('capi-mapping-error'), async () => { await request(data); $('capi-mapping-dialog').close(); });
  });
  testForm.addEventListener('submit', event => {
    event.preventDefault();
    run(testForm, $('capi-test-error'), async () => { await request({ action: 'test_event', mapping_id: testForm.elements.mapping_id.value, lead_id: testForm.elements.lead_id.value }); $('capi-test-dialog').close(); });
  });
  $('capi-token-visibility').addEventListener('click', () => {
    const shown = $('capi-token').type === 'password';
    $('capi-token').type = shown ? 'text' : 'password';
    $('capi-token-visibility').setAttribute('aria-label', shown ? 'Hide access token' : 'Show access token');
    $('capi-token-visibility').setAttribute('aria-pressed', String(shown));
  });
  $('capi-mode').addEventListener('change', updateModeHelp);
  mappingForm.elements.pipeline.addEventListener('change', () => updateStages());
  mappingForm.elements.value_source.addEventListener('change', updateMappingFields);
  $('capi-event-type').addEventListener('change', () => {
    if ($('capi-event-type').value === 'Purchase' && mappingForm.elements.value_source.value === 'none') mappingForm.elements.value_source.value = 'static';
    updateMappingFields();
  });
  testForm.elements.mapping_id.addEventListener('change', updateTestLeads);
  document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  $('capi-add-mapping').addEventListener('click', () => openMapping());
  $('capi-open-test').addEventListener('click', () => openTest());
  $('capi-disconnect').addEventListener('click', async () => {
    if (!window.confirm('Disconnect Meta Conversions API and stop automatic event delivery?')) return;
    try { await request({ action: 'disconnect' }); } catch (error) { notice(error.message, true); }
  });
  async function refresh(showErrors = false) {
    if (document.hidden || document.querySelector('.capi-dialog[open]')) return;
    try {
      const response = await fetch(`${endpoint}?format=json`, { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error('Could not refresh delivery activity. Refresh the page if your session expired.');
      state = await response.json(); renderStatus(); renderDeliveries();
    } catch (error) { if (showErrors) notice(error.message, true); }
  }
  $('capi-refresh').addEventListener('click', () => refresh(true));
  renderState(true);
  window.setInterval(() => refresh(), 10000);
})();
