/* Scoped to CRM: existing card controls and HTMX handlers remain independent. */
(() => {
    'use strict';
    const dialog = document.getElementById('crm-bulk-dialog');
    if (!dialog || window.crmBulkInitialized) return;
    window.crmBulkInitialized = true;
    const form = document.getElementById('crm-bulk-form');
    const el = name => document.getElementById(`crm-bulk-${name}`);
    const root = () => document.querySelector('[data-bulk-root]');
    const activePanel = () => root()?.querySelector('[data-stage-panel]:not(.hidden)');
    const boxes = panel => [...(panel?.querySelectorAll('[data-lead-select]') || [])];
    let action = '', selection = null, options = null, busy = false, loading = false, generation = 0;
    let trigger = null, refreshUrl = '', endpoint = '';

    function syncSelection() {
        const current = root(), panel = activePanel();
        if (!current) return;
        current.querySelectorAll('[data-stage-panel]').forEach(stage => {
            const inputs = boxes(stage);
            if (stage !== panel) inputs.forEach(input => { input.checked = false; });
            inputs.forEach(input => input.closest('.lead-card')?.classList.toggle('crm-is-selected', input.checked));
            const selected = inputs.filter(input => input.checked).length;
            const all = stage.querySelector('[data-stage-select]');
            if (all) {
                stage.querySelector('[data-stage-selection]').hidden = inputs.length === 0;
                stage.querySelector('[data-stage-select-label]').textContent = `Select all ${inputs.length} lead${inputs.length === 1 ? '' : 's'} in this stage`;
                all.checked = inputs.length > 0 && selected === inputs.length;
                all.indeterminate = selected > 0 && selected < inputs.length;
            }
        });
        const count = boxes(panel).filter(input => input.checked).length;
        current.querySelector('[data-bulk-toolbar]').hidden = count === 0;
        current.querySelector('[data-bulk-count]').textContent = `${count} lead${count === 1 ? '' : 's'} selected`;
    }

    function clearSelection() {
        root()?.querySelectorAll('[data-lead-select]').forEach(input => { input.checked = false; });
        syncSelection();
    }

    function status(message) {
        el('status').textContent = message;
        el('status').hidden = !message;
    }

    function showError(message) {
        el('error').textContent = message;
        el('error').hidden = !message;
    }

    function syncForm() {
        const updating = action === 'update';
        el('destination').disabled = !updating || !el('move').checked;
        el('followup').disabled = !updating || !el('change-sequence').checked;
        el('sequence').disabled = form.elements.sequence_action.value !== 'assign';
        const custom = action === 'export' && form.elements.attribute_mode.value === 'selected';
        el('attributes').hidden = !custom;
        el('attributes').disabled = !custom;
        const noChanges = updating && !el('move').checked && !el('change-sequence').checked;
        const noFields = custom && !el('field-list').querySelector('input:checked');
        el('submit').disabled = busy || loading || !options || noChanges || noFields;
    }

    function setBusy(value) {
        busy = value;
        dialog.setAttribute('aria-busy', String(value));
        dialog.querySelector('.crm-bulk-dialog-body').inert = value;
        dialog.querySelectorAll('[data-bulk-cancel]').forEach(button => { button.disabled = value; });
        el('submit').textContent = value ? 'Processing…' : ({update: 'Save changes', export: 'Export XLSX', delete: 'Delete leads'}[action]);
        syncForm();
    }

    async function request(payload) {
        const response = await fetch(endpoint, {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': form.elements.csrfmiddlewaretoken.value},
            body: JSON.stringify({...selection, ...payload}),
        });
        const contentType = response.headers.get('Content-Type') || '';
        if (response.redirected || (!contentType.includes('json') && !contentType.includes('spreadsheetml'))) {
            throw new Error('The request could not be completed. Check your connection and sign-in, then try again.');
        }
        if (!response.ok) {
            const result = await response.json();
            throw new Error(result.error || 'Unable to complete the action. Please try again.');
        }
        return response;
    }

    function fillSelect(select, items, placeholder) {
        select.replaceChildren(new Option(placeholder, ''));
        items.forEach(item => select.add(new Option(item.name, item.id)));
    }

    function fillStages() {
        const pipeline = options?.pipelines.find(item => item.id === el('pipeline').value);
        fillSelect(el('stage'), pipeline?.stages || [], 'Choose a stage');
        if (pipeline?.id === selection.pipeline) el('stage').value = selection.source_stage;
    }

    async function openDialog(button) {
        const current = root(), panel = activePanel();
        const ids = boxes(panel).filter(input => input.checked).map(input => input.value);
        if (!ids.length) return;
        trigger = button;
        action = button.dataset.bulkAction;
        selection = {lead_ids: ids, pipeline: current.dataset.pipeline, source_stage: panel.dataset.stagePanel};
        endpoint = current.dataset.bulkUrl;
        const url = new URL(current.dataset.refreshUrl, window.location.origin);
        url.searchParams.set('pipeline', selection.pipeline);
        url.searchParams.set('stage', selection.source_stage);
        refreshUrl = url.pathname + url.search;
        options = null;
        form.reset();
        showError('');
        loading = true;
        el('title').textContent = {update: 'Update Leads', export: 'Export Leads', delete: 'Delete Leads'}[action];
        el('description').textContent = `Loading options for ${ids.length} selected lead${ids.length === 1 ? '' : 's'}…`;
        dialog.querySelectorAll('[data-bulk-section]').forEach(section => { section.hidden = true; });
        el('submit').classList.toggle('crm-bulk-danger', action === 'delete');
        el('submit').classList.toggle('crm-bulk-primary', action !== 'delete');
        setBusy(false);
        dialog.showModal();
        const ticket = ++generation;
        try {
            const response = await request({action: 'options'});
            const result = await response.json();
            if (ticket !== generation || !dialog.open) return;
            options = result;
            const rights = result.permissions;
            if ((action === 'delete' && !rights.delete) || (action === 'update' && !rights.move && !rights.edit)) {
                options = null;
                throw new Error('Your permissions have changed. Refresh the CRM to continue.');
            }
            fillSelect(el('pipeline'), result.pipelines, 'Choose a pipeline');
            el('pipeline').value = selection.pipeline;
            fillStages();
            fillSelect(el('sequence'), result.sequences, result.sequences.length ? 'Choose a sequence' : 'No connected sequences available');
            el('move').disabled = !rights.move;
            el('change-sequence').disabled = !rights.edit;
            el('field-count').textContent = `(${result.fields.length} attributes)`;
            el('field-list').replaceChildren();
            result.fields.forEach(field => {
                const label = document.createElement('label');
                label.className = 'crm-bulk-choice';
                const input = document.createElement('input');
                input.type = 'checkbox'; input.value = field.id; input.checked = true;
                label.append(input, document.createTextNode(field.name));
                el('field-list').append(label);
            });
            const noun = `${result.count} selected lead${result.count === 1 ? '' : 's'}`;
            el('description').textContent = {
                update: `Changes will apply to ${noun}. Only enabled updates will be applied.`,
                export: `${noun} will be exported as an XLSX file.`,
                delete: `All ${noun} will be permanently deleted.`,
            }[action];
            dialog.querySelector(`[data-bulk-section="${action}"]`).hidden = false;
            if (action === 'delete') dialog.querySelector('footer [data-bulk-cancel]').focus();
        } catch (error) {
            if (ticket === generation && dialog.open) showError(error.message);
        } finally {
            if (ticket === generation) { loading = false; syncForm(); }
        }
    }

    document.addEventListener('change', event => {
        if (event.target.matches('[data-stage-select]')) {
            boxes(event.target.closest('[data-stage-panel]')).forEach(input => { input.checked = event.target.checked; });
            syncSelection();
        } else if (event.target.matches('[data-lead-select]')) syncSelection();
    });
    document.addEventListener('click', event => {
        if (event.target.closest('.stage-tab')) clearSelection();
        if (event.target.closest('[data-bulk-clear]')) clearSelection();
        const button = event.target.closest('[data-bulk-action]');
        if (button) openDialog(button);
    });
    dialog.addEventListener('click', event => {
        if (event.target.closest('[data-bulk-cancel]') && !busy) dialog.close();
        if (event.target.closest('[data-attributes-all], [data-attributes-none]')) {
            const checked = Boolean(event.target.closest('[data-attributes-all]'));
            el('field-list').querySelectorAll('input').forEach(input => { input.checked = checked; });
            syncForm();
        }
    });
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => { generation++; trigger?.focus(); });
    form.addEventListener('change', event => {
        if (event.target === el('pipeline')) fillStages();
        showError(''); syncForm();
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy || loading || el('submit').disabled || !form.reportValidity()) return;
        showError('');
        const payload = {action};
        if (action === 'update') {
            Object.assign(payload, {
                move: el('move').checked, target_pipeline: el('pipeline').value, target_stage: el('stage').value,
                sequence_action: el('change-sequence').checked ? form.elements.sequence_action.value : 'keep',
                sequence: el('sequence').value,
            });
        } else if (action === 'export') {
            payload.attribute_mode = form.elements.attribute_mode.value;
            payload.attributes = [...el('field-list').querySelectorAll('input:checked')].map(input => input.value);
        } else payload.confirm_delete = true;
        setBusy(true);
        try {
            const response = await request(payload);
            if (action === 'export') {
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const anchor = document.createElement('a');
                anchor.href = url;
                anchor.download = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'leads.xlsx';
                document.body.append(anchor); anchor.click(); anchor.remove();
                setTimeout(() => URL.revokeObjectURL(url), 60000);
                status(`XLSX export prepared for ${selection.lead_ids.length} selected leads.`);
            } else {
                const result = await response.json();
                clearSelection();
                status(`${result.count} lead${result.count === 1 ? '' : 's'} ${action === 'delete' ? 'deleted' : 'updated'} successfully.`);
                // The saved query retains search and custom filters after mutation.
                if (window.htmx) {
                    window.htmx.ajax('GET', refreshUrl, {target: '#lead-table-container', swap: 'innerHTML'}).catch(() => {
                        status('Changes were saved. Refresh the CRM to see the latest leads.');
                    });
                }
            }
            dialog.close();
        } catch (error) {
            showError(error instanceof TypeError
                ? 'The connection was interrupted. Refresh the CRM to check the result before retrying.'
                : error.message);
        } finally { setBusy(false); }
    });

    // The legacy CRM also inserts/moves cards directly, outside HTMX swaps.
    const table = document.getElementById('lead-table-container');
    if (table) new MutationObserver(records => {
        const relevant = records.some(record =>
            (record.type === 'attributes' && record.target.matches('[data-stage-panel]')) ||
            [...record.addedNodes, ...record.removedNodes].some(node => node.nodeType === 1 && (
                node.matches('.lead-card, [data-bulk-root], [data-stage-panel]') || node.querySelector('[data-lead-select]')
            )));
        if (relevant) syncSelection();
    }).observe(table, {childList: true, subtree: true, attributes: true, attributeFilter: ['class']});
    document.body.addEventListener('htmx:afterSwap', syncSelection);
    syncSelection();
})();
