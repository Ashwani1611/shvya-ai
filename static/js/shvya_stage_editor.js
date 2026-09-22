(function () {
    'use strict';

    var snapshots = new WeakMap();
    var pendingActions = 0;
    var lastFocus = null;

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

        lastFocus = trigger;
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
        if (!canLeave()) return;
        if (root) root.innerHTML = '';
        if (lastFocus && lastFocus.isConnected) lastFocus.focus();
        document.querySelectorAll('select[aria-label="Lead stage"]').forEach(function (select) { Array.from(select.options).forEach(function (option) { option.selected = option.defaultSelected; }); });
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

    function fields(form) { return [form.elements.name.value, form.elements.description.value]; }
    function changed(form) { return JSON.stringify(fields(form)) !== JSON.stringify(snapshots.get(form)); }
    function updateDirty(form) {
        form.querySelector('[data-stage-save-actions]').hidden = !changed(form);
        form.dataset.stageDirty = changed(form) ? '1' : '0';
    }
    function canLeave() {
        if (pendingActions || document.querySelector('[data-stage-saving="1"]')) return false;
        return !document.querySelector('[data-stage-dirty="1"]') || window.confirm('Discard unsaved name and description changes?');
    }
    function initForms() {
        document.querySelectorAll('[data-stage-editor-form]').forEach(function (form) {
            if (!snapshots.has(form)) { snapshots.set(form, fields(form)); updateDirty(form); }
        });
    }
    function csrf(form) { return form.querySelector('[name="csrfmiddlewaretoken"]').value; }
    async function post(url, body, token) {
        var response = await fetch(url, {method:'POST',body:body,credentials:'same-origin',headers:{'HX-Request':'true','X-CSRFToken':token}});
        if (!response.ok) { var text = await response.text(); throw new Error(text.indexOf('<') >= 0 ? 'Unable to apply this change. Please try again.' : text); }
        return response;
    }
    async function save(form) {
        if (form.dataset.stageSaving === '1' || !changed(form)) return;
        if (!form.reportValidity()) return;
        var state = form.querySelector('[data-stage-editor-save-state]');
        var sent = fields(form), body = new FormData(form);
        form.dataset.stageSaving = '1'; state.textContent = 'Saving…';
        form.querySelector('[type=submit]').disabled = true;
        try {
            await post(form.action, body, csrf(form));
            snapshots.set(form, sent); updateDirty(form); state.textContent = 'Saved';
            var row = form.closest('[data-stage-editor-row]');
            row.querySelector('[data-stage-title]').textContent = sent[0].trim();
            row.querySelector('[data-stage-description]').textContent = sent[1].trim() || 'Add a description';
            refreshLeadTable(form.elements.pipeline.value, form.elements.active_stage.value);
        } catch (error) {state.textContent = error.message;}
        finally { form.dataset.stageSaving = '0'; form.querySelector('[type=submit]').disabled = false; }
    }
    function refreshFrom(row) {
        var form = row.querySelector('[data-stage-editor-form]');
        refreshLeadTable(form.elements.pipeline.value, form.elements.active_stage.value);
    }
    async function immediate(button) {
        var row=button.closest('[data-stage-editor-row]'), form=row.querySelector('[data-stage-editor-form]');
        var status=row.querySelector('[data-stage-action-status]'), body=new FormData(), isAI=button.hasAttribute('data-stage-ai');
        body.set(isAI?'enabled':'direction',isAI?String(button.getAttribute('aria-checked')!=='true'):button.dataset.stageOrder);
        var controls=Array.from(document.querySelectorAll('[data-stage-order], [data-stage-ai]'));
        pendingActions++; controls.forEach(function(c){c.disabled=true;}); status.textContent='Updating…';
        try {
            var response=await post(button.dataset.url,body,csrf(form)), data=await response.json();
            if(isAI) button.setAttribute('aria-checked',String(data.ai_on));
            else {
                var list=row.parentElement;
                data.order.forEach(function(id){var item=list.querySelector('[data-stage-id="'+id+'"]');if(item)list.appendChild(item);});
            }
            status.textContent=isAI?'Stage AI '+(data.ai_on?'on.':'off.'):'Stage order updated.'; refreshFrom(row);
        } catch(error){status.textContent=error.message;}
        finally {
            pendingActions--; controls.forEach(function(c){c.disabled=false;});
            var rows=Array.from(document.querySelectorAll('[data-stage-editor-row]'));
            rows.forEach(function(r,i){r.querySelector('.stage-order').textContent=String(i+1).padStart(2,'0');r.querySelector('[data-stage-order=up]').disabled=i===0;r.querySelector('[data-stage-order=down]').disabled=i===rows.length-1;});
        }
    }
    document.addEventListener('click',function(event){
        var target=event.target.closest && event.target.closest('.stage-manager-trigger,[data-stage-editor-close],[data-stage-discard],[data-stage-ai],[data-stage-order]');
        if(target){
            if(target.matches('.stage-manager-trigger')){event.preventDefault();event.stopPropagation();openStageEditor(target);}
            else if(target.matches('[data-stage-editor-close]')){event.preventDefault();closeStageEditor();}
            else if(target.matches('[data-stage-discard]')){var f=target.closest('form'),v=snapshots.get(f);f.elements.name.value=v[0];f.elements.description.value=v[1];updateDirty(f);f.querySelector('[data-stage-editor-save-state]').textContent='';}
            else immediate(target);
        } else if(event.target.matches('[data-stage-editor-overlay]')) closeStageEditor();
    },true);
    document.addEventListener('input',function(event){var form=event.target.closest('[data-stage-editor-form]');if(form)updateDirty(form);});
    document.addEventListener('submit',function(event){
        if(event.target.matches('[data-stage-editor-form]')){event.preventDefault();event.stopPropagation();save(event.target);}
    },true);
    document.addEventListener('change',async function(event){
        var form=event.target.closest('[data-stage-requirements]'); if(!form)return;
        var row=form.closest('[data-stage-editor-row]'),status=row.querySelector('[data-stage-action-status]');
        var checks=Array.from(form.querySelectorAll('input[type=checkbox]')),body=new FormData(form);
        pendingActions++; checks.forEach(function(c){c.disabled=true;});status.textContent='Updating requirements…';
        try{await post(form.action,body,csrf(form));status.textContent='Required attributes updated.';}
        catch(error){event.target.checked=!event.target.checked;status.textContent=error.message;}
        finally{pendingActions--; checks.forEach(function(c){c.disabled=false;});}
    });
    document.addEventListener('keydown',function(event){
        var dialog=document.querySelector('[data-stage-editor-surface]');if(!dialog)return;
        if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();closeStageEditor();}
        if(event.key==='Tab'){
            var focusable=Array.from(dialog.querySelectorAll('button:not(:disabled),input:not([type=hidden]),textarea,select,summary')).filter(function(e){return e.getClientRects().length;});
            var first=focusable[0],last=focusable[focusable.length-1];
            if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
            else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
        }
    },true);
    document.body.addEventListener('htmx:beforeRequest',function(event){
        var form=event.detail.elt;
        if(form && form.closest('[data-stage-editor-overlay]') && !form.matches('[data-stage-editor-form]') && !canLeave()) event.preventDefault();
    });
    document.body.addEventListener('htmx:afterSwap',function(event){initForms();if(event.detail.target.id==='modal-root'){var field=event.detail.target.querySelector('input:not([type=hidden]),[data-stage-editor-close]');if(field)field.focus();}});
    document.body.addEventListener('stageEditorChanged',function(event){var d=event.detail||{};refreshLeadTable(d.pipeline_id||'',d.active_stage_id||'');});
    document.body.addEventListener('leadStageUpdated',function(){var root=modalRoot();if(root&&root.querySelector('#stage-entry-title'))root.innerHTML='';});
    window.addEventListener('beforeunload',function(event){if(document.querySelector('[data-stage-dirty="1"]')){event.preventDefault();event.returnValue='';}});
    initForms();
})();
