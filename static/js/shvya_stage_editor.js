(function () {
    'use strict';

    var saveTimers = new WeakMap();

    function isCRM() {
        return document.documentElement.classList.contains('shvya-crm-apple-v2') ||
            !!document.getElementById('lead-table-container');
    }

    function modalRoot() {
        return document.getElementById('modal-root');
    }

    function currentPipelineId(trigger) {
        var root = trigger && trigger.closest('[data-bulk-root]');
        if (!root) root = document.querySelector('[data-bulk-root]');
        if (root && root.dataset.pipeline) return root.dataset.pipeline;

        var table = document.getElementById('lead-table-container');
        var requestUrl = table && table.getAttribute('hx-get');
        if (!requestUrl) return '';

        try {
            return new URL(requestUrl, window.location.origin).searchParams.get('pipeline') || '';
        } catch (error) {
            return '';
        }
    }

    function currentActiveStageId() {
        var visible = document.querySelector('.stage-panel:not(.hidden)[data-stage-panel]');
        if (visible) return visible.dataset.stagePanel || '';

        var selected = document.querySelector('.stage-tab[data-stage-id][style*="border-bottom: 2px solid"]');
        return selected ? (selected.dataset.stageId || '') : '';
    }

    function openStageEditor(trigger) {
        if (!window.htmx) return;

        var pipelineId = currentPipelineId(trigger);
        if (!pipelineId) return;

        var activeStageId = currentActiveStageId();
        var url = '/dashboard/stage-editor/?pipeline=' + encodeURIComponent(pipelineId);
        if (activeStageId) {
            url += '&active_stage=' + encodeURIComponent(activeStageId);
        }

        window.htmx.ajax('GET', url, {
            target: '#modal-root',
            swap: 'innerHTML'
        });
    }

    function closeStageEditor() {
        var root = modalRoot();
        if (root) root.innerHTML = '';
    }

    function stageTableRequestUrl(pipelineId, activeStageId) {
        var container = document.getElementById('lead-table-container');
        if (!container) return '';

        var request = container.getAttribute('hx-get') || '/dashboard/leads/table/';
        var url;

        try {
            url = new URL(request, window.location.origin);
        } catch (error) {
            url = new URL('/dashboard/leads/table/', window.location.origin);
        }

        if (pipelineId) url.searchParams.set('pipeline', pipelineId);
        if (activeStageId) {
            url.searchParams.set('stage', activeStageId);
        } else {
            url.searchParams.delete('stage');
        }

        return url.pathname + url.search;
    }

    function refreshLeadTable(pipelineId, activeStageId) {
        if (!window.htmx) return;

        var url = stageTableRequestUrl(pipelineId, activeStageId);
        if (!url) return;

        window.htmx.ajax('GET', url, {
            target: '#lead-table-container',
            swap: 'innerHTML'
        });
    }

    function statusNode(form) {
        return form && form.querySelector('[data-stage-editor-save-state]');
    }

    function setStatus(form, state, message) {
        var node = statusNode(form);
        if (!node) return;

        node.classList.remove('is-saving', 'is-error', 'is-saved');
        if (state) node.classList.add('is-' + state);
        node.textContent = message;
    }

    function csrfToken(form) {
        var input = form.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : '';
    }

    async function persistStageForm(form) {
        if (!form || !form.matches('[data-stage-editor-form]')) return;

        var timer = saveTimers.get(form);
        if (timer) {
            window.clearTimeout(timer);
            saveTimers.delete(form);
        }

        if (form.dataset.stageSaving === '1') {
            form.dataset.stageDirty = '1';
            return;
        }

        var nameInput = form.querySelector('input[name="name"]');
        if (nameInput && !nameInput.value.trim()) {
            setStatus(form, 'error', 'Name required');
            return;
        }

        form.dataset.stageSaving = '1';
        form.dataset.stageDirty = '0';
        setStatus(form, 'saving', 'Saving…');

        try {
            var response = await fetch(form.action, {
                method: 'POST',
                body: new FormData(form),
                credentials: 'same-origin',
                cache: 'no-store',
                headers: {
                    'HX-Request': 'true',
                    'X-CSRFToken': csrfToken(form),
                    'Cache-Control': 'no-cache'
                }
            });

            if (!response.ok) {
                var text = (await response.text()).trim();
                throw new Error(text || 'Unable to save stage.');
            }

            setStatus(form, 'saved', 'Saved');

            var pipelineInput = form.querySelector('input[name="pipeline"]');
            var activeStageInput = form.querySelector('input[name="active_stage"]');
            refreshLeadTable(
                pipelineInput ? pipelineInput.value : '',
                activeStageInput ? activeStageInput.value : ''
            );
        } catch (error) {
            setStatus(form, 'error', error.message || 'Save failed');
        } finally {
            form.dataset.stageSaving = '0';

            if (form.dataset.stageDirty === '1') {
                form.dataset.stageDirty = '0';
                scheduleStageSave(form, 220);
            }
        }
    }

    function scheduleStageSave(form, delay) {
        if (!form) return;

        var existing = saveTimers.get(form);
        if (existing) window.clearTimeout(existing);

        setStatus(form, 'saving', 'Unsaved');

        var timer = window.setTimeout(function () {
            saveTimers.delete(form);
            persistStageForm(form);
        }, typeof delay === 'number' ? delay : 650);

        saveTimers.set(form, timer);
    }

    function handleDocumentClick(event) {
        if (!isCRM()) return;

        var trigger = event.target.closest && event.target.closest('.stage-manager-trigger');
        if (trigger) {
            event.preventDefault();
            event.stopPropagation();
            openStageEditor(trigger);
            return;
        }

        var close = event.target.closest && event.target.closest('[data-stage-editor-close]');
        if (close) {
            event.preventDefault();
            closeStageEditor();
            return;
        }

        var overlay = event.target.closest && event.target.closest('[data-stage-editor-overlay]');
        if (overlay && event.target === overlay) {
            closeStageEditor();
        }
    }

    function handleFieldInput(event) {
        var field = event.target;
        if (!field || !field.closest) return;

        var form = field.closest('[data-stage-editor-form]');
        if (!form) return;

        if (field.name !== 'name' && field.name !== 'description') return;
        scheduleStageSave(form, 650);
    }

    function handleFieldBlur(event) {
        var field = event.target;
        if (!field || !field.closest) return;

        var form = field.closest('[data-stage-editor-form]');
        if (!form) return;
        if (field.name !== 'name' && field.name !== 'description') return;

        scheduleStageSave(form, 80);
    }

    function handleSubmit(event) {
        var form = event.target;
        if (!form || !form.matches || !form.matches('[data-stage-editor-form]')) return;

        event.preventDefault();
        event.stopPropagation();
        persistStageForm(form);
    }

    function handleEscape(event) {
        if (event.key !== 'Escape') return;
        if (!document.querySelector('[data-stage-editor-overlay]')) return;
        closeStageEditor();
    }

    function init() {
        document.addEventListener('click', handleDocumentClick, true);
        document.addEventListener('input', handleFieldInput, true);
        document.addEventListener('blur', handleFieldBlur, true);
        document.addEventListener('submit', handleSubmit, true);
        document.addEventListener('keydown', handleEscape, true);

        document.body.addEventListener('stageEditorChanged', function (event) {
            var detail = event.detail || {};
            refreshLeadTable(detail.pipeline_id || '', detail.active_stage_id || '');
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();
