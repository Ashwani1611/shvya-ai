/* SHVYA Bulk Campaigns: server-owned audiences, evidence and send decisions. */
(function () {
    'use strict';
    const configNode = document.getElementById('bulk-campaign-config');
    if (!configNode) return;
    const config = JSON.parse(configNode.textContent);
    const $ = (id) => document.getElementById(id);
    const root = $('bulk-campaign-root');
    const composer = $('bc-composer');
    const inspector = $('bc-inspector');
    const csrf = document.querySelector('input[name="csrfmiddlewaretoken"]')?.value || '';
    const h = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const number = (value) => value == null ? '—' : Number(value).toLocaleString();
    const date = (value, zone) => {
        if (!value) return '—';
        try { return new Intl.DateTimeFormat(undefined, {dateStyle:'medium', timeStyle:'short', ...(zone ? {timeZone:zone} : {})}).format(new Date(value)); }
        catch (_) { return 'Unavailable'; }
    };
    const labels = {pending:'Queued', retry_pending:'Retry scheduled', preparing:'Preparing audience', queued:'Queued', scheduled:'Scheduled', sending:'Sending', accepted:'Accepted by Meta', delivered:'Delivered', read:'Read', replied:'Replied', completed:'Completed', failed:'Failed', skipped:'Skipped', review:'Needs review', retrying:'Retry scheduled', cancelled:'Cancelled', draft:'Legacy draft'};
    const pill = (status) => `<span class="bc-pill" data-status="${h(status)}">${h(labels[status] || status)}</span>`;
    const options = (items, value, empty) => `${empty === undefined ? '' : `<option value="">${h(empty)}</option>`}${items.map((item) => `<option value="${h(item.id ?? item.key)}" ${(item.id ?? item.key) === value ? 'selected' : ''}>${h(item.name ?? item.label)}</option>`).join('')}`;
    const state = {meta:null, report:null, rows:[], step:1, upload:null, audience:null, template:null, templates:[], bindings:{}, preview:null, previewVersion:0, busy:false, page:1, activePage:1, filters:{q:'',account:'',status:'',pipeline:'',stage:''}, selected:new Set(), all:false, requestVersion:0, confirm:null, crmSeed:false};
    let filterTimer, templateTimer, previewTimer;

    function safeURL(value) {
        const url = new URL(value, window.location.origin);
        if (url.origin !== window.location.origin) throw new Error('An unexpected destination was blocked.');
        return url.href;
    }
    async function api(url, data, {blob=false}={}) {
        const response = await fetch(safeURL(url), {
            method:data === undefined ? 'GET' : 'POST', credentials:'same-origin', cache:'no-store',
            headers:data instanceof FormData ? {'X-CSRFToken':csrf} : {'X-CSRFToken':csrf, 'Content-Type':'application/json'},
            ...(data === undefined ? {} : {body:data instanceof FormData ? data : JSON.stringify(data)}),
        });
        if (!response.ok) {
            let message = 'The request could not be completed. Please try again.';
            try { const payload = await response.json(); message = payload.error || message; } catch (_) { /* Preserve actionable fallback. */ }
            if (response.status === 403) message = 'Your session or permission has changed. Refresh the page and sign in again.';
            throw new Error(message);
        }
        if (blob) return response.blob();
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) throw new Error('Your session may have expired. Refresh the page to continue.');
        return response.json();
    }
    function notice(message='', error=false) {
        const node = $('bc-notice'); node.hidden = !message; node.textContent = message; node.classList.toggle('bc-error', error);
    }
    function builderError(message='') {
        const node = $('bc-composer-error'); node.hidden = !message; node.textContent = message;
    }
    function busy(value) {
        state.busy = value; composer.setAttribute('aria-busy', String(value));
        composer.querySelectorAll('[data-close],#bc-back,#bc-next').forEach((node) => { node.disabled = value; });
        refreshNext();
    }
    function progress(value) { return `<progress class="bc-progress" max="100" value="${Math.max(0, Math.min(100, Number(value) || 0))}" aria-label="Campaign dispatch progress"></progress>`; }
    function stat(title, value, detail='') { return `<div class="bc-stat"><small>${h(title)}</small><strong>${number(value)}</strong><small>${h(detail)}</small></div>`; }
    function pageNav(page) { return `<div class="bc-pages"><span class="bc-help">${number(page.total)} records · Page ${number(page.number)} of ${number(page.pages)}</span><div><button class="bc-button bc-small" data-action="previous" ${page.number <= 1 ? 'disabled' : ''}>Previous</button> <button class="bc-button bc-small" data-action="next-page" ${page.number >= page.pages ? 'disabled' : ''}>Next</button></div></div>`; }
    function filterMarkup(detail=false) {
        const pipelines = state.meta?.pipelines || [];
        const stages = state.filters.pipeline ? (pipelines.find((p) => p.id === state.filters.pipeline)?.stages || []) : pipelines.flatMap((p) => p.stages);
        return `<div class="bc-toolbar"><label class="bc-field bc-grow">${detail ? 'Find a recipient' : 'Find a campaign'}<input data-filter="q" type="search" value="${h(state.filters.q)}" placeholder="${detail ? 'Name or phone number' : 'Search campaign name'}"></label>${detail ? `<label class="bc-field">Delivery status<select data-filter="status">${options(Object.entries(labels).filter(([id]) => ['pending','retry_pending','sending','accepted','delivered','read','replied','failed','skipped','review'].includes(id)).map(([id,name]) => ({id,name})),state.filters.status,'All statuses')}</select></label><label class="bc-field">Pipeline<select data-filter="pipeline">${options(pipelines,state.filters.pipeline,'All pipelines')}</select></label><label class="bc-field">Stage<select data-filter="stage">${options(stages,state.filters.stage,'All stages')}</select></label>` : `<label class="bc-field">Sending number<select data-filter="account">${options(state.meta?.accounts || [],state.filters.account,'All sending numbers')}</select></label>`}<button class="bc-button" data-action="clear-filters">Clear filters</button></div>`;
    }
    function saveFilterFocus() {
        const node = document.activeElement;
        return node?.matches('[data-filter]') ? {key:node.dataset.filter, start:node.selectionStart, end:node.selectionEnd} : null;
    }
    function restoreFilterFocus(saved) {
        if (!saved) return;
        const node = root.querySelector(`[data-filter="${saved.key}"]`);
        if (node) { node.focus({preventScroll:true}); if (node.type === 'search' && saved.start != null) node.setSelectionRange(saved.start,saved.end); }
    }
    function renderList(data) {
        const totals = data.summary;
        const active = data.active.map((campaign) => `<article class="bc-card"><div class="bc-card-top">${pill(campaign.status)}<small class="bc-help">${h(campaign.account_phone || campaign.account_name)}</small></div><h3>${h(campaign.name)}</h3><p class="bc-help">${h(campaign.template_name)}</p>${progress(campaign.counts.dispatch_percent)}<div class="bc-between"><span class="bc-help">${number(campaign.counts.delivered)} delivered / ${number(campaign.counts.total)} recipients</span><a class="bc-row-action" href="${h(campaign.url)}" aria-label="View ${h(campaign.name)}">View →</a></div><p class="bc-help">${campaign.status === 'preparing' ? `${number(campaign.preparation?.processed)} / ${number(campaign.preparation?.planned)} audience rows prepared` : h(date(campaign.scheduled_for || campaign.started_at,campaign.timezone))}</p></article>`).join('');
        $('bc-workspace').innerHTML = `<div class="bc-stats">${stat('In progress',data.active_count,'Scheduled and active campaigns')}${stat('Delivered',totals.delivered,'Recorded recipient deliveries')}${stat('Read',totals.read,'Recorded read receipts')}${stat('Replied',totals.replied,'Attributed recipient replies')}${stat('Needs attention',(totals.failed || 0)+(totals.review || 0),'Failed or uncertain recipients')}</div><p class="bc-help">Outcome totals cover managed campaigns matching these filters. Historical campaigns retain only their available evidence.</p>${filterMarkup()}<div class="bc-section-title"><h2>In progress <span class="bc-count">${number(data.active_count)}</span></h2><span class="bc-help">${data.active_count > data.active.length ? `Showing ${number(data.active.length)} active campaigns` : 'Recorded status, not simulated progress'}</span></div>${active ? `<div class="bc-cards">${active}</div>` : '<div class="bc-empty"><strong>A little quiet, for now.</strong>Scheduled and sending campaigns will appear here.</div>'}${data.active_pagination?.pages > 1 ? pageNav(data.active_pagination).replace('data-action="previous"','data-action="previous-active"').replace('data-action="next-page"','data-action="next-active"') : ''}<div class="bc-section-title"><h2>History <span class="bc-count">${number(data.pagination.total)}</span></h2><span class="bc-help">Completed means processing finished, not that every message was delivered.</span></div>${data.history.length ? `<div class="bc-table-shell"><table class="bc-table"><thead><tr><th>Campaign</th><th>Delivery</th><th>Engagement</th><th>Failed</th><th>Status</th><th>Created</th><th>Details</th></tr></thead><tbody>${data.history.map((campaign) => `<tr><td><strong>${h(campaign.name)}</strong><small>${h(campaign.template_name)}${campaign.legacy ? ' · Legacy' : ''}</small></td><td><strong>${number(campaign.counts.delivered)} / ${number(campaign.counts.total)}</strong><small>${h(campaign.counts.percentages.delivered ?? '—')}% delivered</small></td><td><strong>${number(campaign.counts.read)} read</strong><small>${number(campaign.counts.replied)} replied</small></td><td>${number(campaign.counts.failed)}</td><td>${pill(campaign.status)}</td><td>${h(date(campaign.created_at))}</td><td><a class="bc-row-action" href="${h(campaign.url)}">View →</a></td></tr>`).join('')}</tbody></table></div>${pageNav(data.pagination)}` : '<div class="bc-empty"><strong>Your story starts with a campaign.</strong>No campaign history matches these filters.</div>'}<p class="bc-refresh-time">Retrieved ${h(date(data.retrieved_at))} · Updates every 10 seconds while this page is visible</p>`;
    }
    function renderDetail(data) {
        const campaign = data.campaign, counts = campaign.counts;
        state.report = campaign; state.rows = data.recipients; state.filteredCount = data.pagination.total;
        const active = ['preparing','queued','scheduled','sending','retrying'].includes(campaign.status);
        root.querySelector('.bc-hero').innerHTML = `<div><a class="bc-backlink" href="${h(config.urls.list)}">← All campaigns</a><p class="bc-eyebrow">CAMPAIGN REPORT</p><h1>${h(campaign.name)}</h1><p class="bc-subtitle">${h(campaign.account_phone || campaign.account_name)} · ${h(campaign.template_name)}</p></div><div>${pill(campaign.status)}${campaign.can_manage ? `<div style="margin-top:15px"><button class="bc-button bc-small" data-action="rename">Rename</button> ${active ? '<button class="bc-button bc-small bc-danger" data-action="cancel">Cancel remaining</button>' : ''}</div>` : ''}</div>`;
        const ratio = (key) => counts.percentages[key] == null ? 'Evidence unavailable' : `${counts.percentages[key]}% of recipients`;
        const prepare = campaign.preparation;
        $('bc-workspace').innerHTML = `<div class="bc-stats bc-detail-stats">${stat('Recipients',counts.total,prepare && !prepare.processed ? `${number(prepare.planned)} planned` : 'Frozen audience entries')}${stat('Accepted by Meta',counts.accepted,'Acceptance is not delivery')}${stat('Sent receipts',counts.sent,ratio('sent'))}${stat('Delivered',counts.delivered,ratio('delivered'))}${stat('Read',counts.read,ratio('read'))}${stat('Replied',counts.replied,ratio('replied'))}${stat('Failed',counts.failed,ratio('failed'))}${stat('Skipped',counts.skipped,ratio('skipped'))}</div><div class="bc-mini-stats"><span>${number(counts.queued)} queued</span><span>${number(counts.sending)} sending</span><span>${number(counts.retry_pending)} retries scheduled</span><span>${number(counts.review)} need review</span></div>${campaign.legacy ? '<div class="bc-note">Historical campaign: sent-receipt, reply and attempt history were not recorded by the previous sender. Unavailable values are shown as —, not zero. Create a newly reviewed campaign instead of retrying an untracked historical send.</div>' : ''}${prepare?.error ? `<div class="bc-notice bc-error">${h(prepare.error)}</div>` : ''}<div class="bc-detail-grid"><section class="bc-panel"><h2>Campaign at a glance</h2><dl class="bc-facts" style="margin-top:22px"><div><dt>Sending number</dt><dd>${h(campaign.account_phone || 'Not available')}</dd></div><div><dt>Created</dt><dd>${h(date(campaign.created_at,campaign.timezone))}</dd></div><div><dt>Scheduled for</dt><dd>${h(date(campaign.scheduled_for,campaign.timezone))}</dd></div><div><dt>Time zone</dt><dd>${h(campaign.timezone || 'Not recorded')}</dd></div><div><dt>First send started</dt><dd>${h(date(campaign.started_at,campaign.timezone))}</dd></div><div><dt>Processing finished</dt><dd>${h(date(campaign.completed_at,campaign.timezone))}</dd></div><div><dt>Automatic retries</dt><dd>${campaign.retry_policy ? campaign.retry_policy.enabled ? `${campaign.retry_policy.attempts} retries · ${campaign.retry_policy.delay_hours}h wait` : 'Off' : 'Not recorded'}</dd></div><div><dt>Verified campaign cost</dt><dd>Not available</dd></div></dl>${prepare ? `<div class="bc-mini-stats"><span>${number(prepare.stats.rows)} file rows</span><span>${number(prepare.stats.new_leads_created)} new CRM leads</span><span>${number(prepare.stats.existing_leads_updated)} existing leads updated</span><span>${number(prepare.stats.preparation_skipped)} skipped during preparation</span></div>${prepare.errors.length ? `<details><summary class="bc-help">Preparation exceptions</summary><ul class="bc-error-list">${prepare.errors.map((error) => `<li>Row ${number(error.row)}: ${h(error.reason)}</li>`).join('')}</ul></details>` : ''}` : ''}<p class="bc-help">Delivery, read and reply measures overlap; they are not additive. Replies are attributed to a quoted campaign message, or the most recent outbound campaign message on this sending account within seven days, with no intervening outbound message. No delivery guarantee is inferred from processing completion.</p></section><section class="bc-panel"><p class="bc-eyebrow">MESSAGE SNAPSHOT</p><h2>${h(campaign.template_name)}</h2><div class="bc-detail-message">${h(campaign.preview_body)}</div><p class="bc-help">A reviewed recipient preview. Each recipient receives their own frozen personalization values. Pricing is not fabricated when billing evidence is unavailable.</p></section></div><div class="bc-section-title"><h2>Recipient activity <span class="bc-count">${number(data.pagination.total)}</span></h2><button class="bc-button bc-small" data-action="export">Export ${state.all || state.selected.size ? 'selected' : 'matching'} leads ↓</button></div>${filterMarkup(true)}<div id="bc-bulk-tools"></div><div class="bc-table-shell"><table class="bc-table"><thead><tr><th><input type="checkbox" id="bc-select-page" aria-label="Select recipients on this page"></th><th>Lead</th><th>Pipeline & stage</th><th>Status & evidence</th><th>Attempts</th><th>Actions</th></tr></thead><tbody>${data.recipients.length ? data.recipients.map((row) => `<tr><td><input type="checkbox" data-recipient="${h(row.id)}" aria-label="Select ${h(row.name)}" ${state.all || state.selected.has(row.id) ? 'checked' : ''}></td><td><strong>${h(row.name)}</strong><small>${h(row.phone)}</small></td><td><strong>${h(row.pipeline)}</strong><small>${h(row.stage)}</small></td><td>${pill(row.status)}${row.replied ? '<small>Reply recorded</small>' : ''}<small>${h(date(row.recorded_at,campaign.timezone))}</small>${row.error_code || row.error_message ? `<small class="bc-error-text">${h(row.error_code)} ${h(row.error_message)}</small>` : ''}</td><td><button class="bc-row-action" data-action="attempts" data-id="${h(row.id)}" title="View send attempt evidence">${number(row.attempt_count)} attempts</button></td><td><div class="bc-row-actions">${row.whatsapp_url ? `<a class="bc-row-action" href="${h(row.whatsapp_url)}" target="_blank" rel="noopener noreferrer" aria-label="Open WhatsApp for ${h(row.name)}">WhatsApp ↗</a>` : ''}${row.lead_id ? `<button class="bc-row-action" data-action="lead" data-id="${h(row.id)}">Lead</button>` : ''}<button class="bc-row-action" data-action="retry-one" data-id="${h(row.id)}" ${!row.can_retry || !campaign.can_manage ? 'disabled' : ''} title="${h(row.retry_reason)}">↻ Retry</button></div></td></tr>`).join('') : '<tr><td colspan="6"><div class="bc-empty">No recipients match these filters.</div></td></tr>'}</tbody></table></div>${pageNav(data.pagination)}<p class="bc-refresh-time">Retrieved ${h(date(data.retrieved_at))} · No simulated counts</p>`;
        renderSelection();
    }
    function renderSelection() {
        const node = $('bc-bulk-tools'); if (!node) return;
        const count = state.all ? state.filteredCount : state.selected.size;
        node.innerHTML = count ? `<div class="bc-bulk-bar"><strong>${number(count)} selected</strong>${!state.all ? `<button class="bc-link" data-action="select-all">Select all ${number(state.filteredCount)} matching recipients</button>` : '<span class="bc-help">All filtered recipients, across every page</span>'}<button class="bc-button bc-small" data-action="retry" ${!state.report?.can_manage ? 'disabled' : ''}>↻ Retry eligible</button><button class="bc-button bc-small" data-action="export">Export leads ↓</button><button class="bc-link" data-action="clear-selection">Clear</button></div>` : '';
        const page = $('bc-select-page');
        if (page) { page.checked = state.rows.length > 0 && state.rows.every((row) => state.all || state.selected.has(row.id)); page.indeterminate = !page.checked && state.rows.some((row) => state.selected.has(row.id)); }
    }
    function selection(ids) { return ids ? {ids} : state.all ? {all:true,filters:{...state.filters}} : state.selected.size ? {ids:[...state.selected]} : {all:true,filters:{...state.filters}}; }
    async function loadWorkspace(silent=false) {
        const version = ++state.requestVersion;
        const params = new URLSearchParams({...state.filters,page:String(state.page),active_page:String(state.activePage)});
        try {
            const data = await api(`${config.urls.data}?${params}`);
            if (version !== state.requestVersion) return;
            const focus = saveFilterFocus();
            if (config.campaign_id) renderDetail(data); else renderList(data);
            restoreFilterFocus(focus); notice(); root.setAttribute('aria-busy','false');
        } catch (error) { if (version === state.requestVersion) notice(`${silent ? 'Updates paused; the last retrieved values remain visible. ' : ''}${error.message}`,true); root.setAttribute('aria-busy','false'); }
    }
    function refreshNext() {
        const next = $('bc-next');
        next.textContent = state.busy ? 'Working…' : state.step === 4 ? $('bc-schedule').checked ? 'Schedule campaign' : 'Send now' : 'Continue';
        next.disabled = state.busy || (state.step === 1 && !state.upload) || (state.step === 3 && !state.template) || (state.step === 4 && (!state.preview?.ready || !$('bc-consent').checked || (!$('bc-exclusions-label').hidden && !$('bc-exclusions').checked)));
        $('bc-back').hidden = state.step === 1 || (state.crmSeed && state.step === 3);
    }
    function setStep(step) {
        state.step = step; builderError();
        composer.querySelectorAll('[data-step]').forEach((node) => { node.hidden = Number(node.dataset.step) !== step; });
        composer.querySelectorAll('[data-step-indicator]').forEach((node) => { if (Number(node.dataset.stepIndicator) === step) node.setAttribute('aria-current','step'); else node.removeAttribute('aria-current'); });
        $('bc-step-summary').textContent = state.audience ? `${number(state.audience.stats.eligible)} eligible recipients` : 'No CRM changes until confirmation';
        composer.querySelector('.bc-dialog-body').scrollTop = 0; refreshNext();
    }
    function resetPreview() { state.preview = null; state.previewVersion++; refreshNext(); }
    function setStages() {
        const pipeline = state.meta.pipelines.find((item) => item.id === $('bc-pipeline').value);
        $('bc-stage').innerHTML = options(pipeline?.stages || [],'','Choose a default stage');
    }
    function showComposer() {
        if (!state.meta?.can_create) { notice('You need lead-edit permission on an accessible pipeline to create a campaign.',true); return; }
        if (state.busy) return;
        if (!composer.open) composer.showModal();
        setStep(state.step);
    }
    async function applyCRMSeed(seed) {
        if (!seed?.upload || !seed?.audience) return;
        state.crmSeed = true;
        state.upload = seed.upload;
        state.audience = seed.audience;
        state.step = 3;

        $('bc-mode').value = 'existing_only';
        $('bc-update-existing').checked = false;
        $('bc-move-existing').checked = false;
        $('bc-pipeline').value = seed.pipeline_id || '';
        setStages();
        $('bc-account').value = seed.account_id || '';

        $('bc-file-summary').innerHTML = `<div class="bc-file-ready"><strong>CRM selection</strong>${number(seed.audience.stats.eligible)} eligible of ${number(seed.upload.row_count)} selected leads</div>`;
        $('bc-audience-review').innerHTML = `<div class="bc-mini-stats"><span><strong>${number(seed.audience.stats.eligible)}</strong> eligible</span><span><strong>${number(seed.upload.row_count-seed.audience.stats.eligible)}</strong> excluded</span></div>`;
        $('bc-exclusions-label').hidden = seed.upload.row_count === seed.audience.stats.eligible;
        $('bc-exclusions').checked = false;
        $('bc-exclusions-text').textContent = `I acknowledge ${number(seed.upload.row_count-seed.audience.stats.eligible)} selected leads are excluded and only ${number(seed.audience.stats.eligible)} eligible recipients will be prepared.`;

        await loadTemplates();
    }
    async function uploadFile(file) {
        if (state.busy || !file) return;
        if (!/\.(csv|xls|xlsx)$/i.test(file.name) || !file.size || file.size > 10*1024*1024) { builderError('Choose a non-empty CSV, XLS or XLSX file up to 10 MB.'); return; }
        builderError(); busy(true); state.upload = null; state.audience = null; resetPreview();
        try {
            const form = new FormData(); form.append('file',file);
            state.upload = await api(config.urls.upload,form);
            $('bc-file-summary').innerHTML = `<div class="bc-file-ready"><strong>${h(state.upload.filename)}</strong>${number(state.upload.row_count)} rows · ${number(state.upload.headers.length)} columns</div>`;
            const used = new Set();
            $('bc-mapping').innerHTML = state.meta.fields.map((field) => {
                const match = state.upload.headers.find((header) => !used.has(header) && [field.label,field.key.replace('attr:','')].some((label) => label.toLowerCase().replace(/[^a-z0-9]/g,'') === header.toLowerCase().replace(/[^a-z0-9]/g,'')));
                if (match) used.add(match);
                return `<div class="bc-map-row"><label for="bc-map-${h(field.key)}">${h(field.label)}${field.required ? ' <span class="bc-required">*</span>' : ''}</label><span class="bc-arrow" aria-hidden="true">→</span><select class="bc-select" id="bc-map-${h(field.key)}" data-map="${h(field.key)}">${options(state.upload.headers.map((header) => ({id:header,name:header})),match || '',field.required ? 'Choose a column' : 'Do not import')}</select></div>`;
            }).join('');
            $('bc-audience-review').innerHTML = '<p class="bc-help">Suggested matches are not applied automatically. Review these selections before continuing.</p>';
            setStep(2);
        } catch (error) { builderError(error.message); } finally { busy(false); }
    }
    function mappingPayload() {
        const mapping = {}; composer.querySelectorAll('[data-map]').forEach((node) => { if (node.value) mapping[node.dataset.map] = node.value; });
        return {mapping,mode:$('bc-mode').value,pipeline_id:$('bc-pipeline').value,stage_id:$('bc-stage').value,update_existing:$('bc-update-existing').checked,move_existing:$('bc-move-existing').checked};
    }
    async function reviewAudience() {
        const reviewed = await api(state.upload.review_url,mappingPayload());
        state.audience = reviewed; resetPreview();
        $('bc-audience-review').innerHTML = `<div class="bc-mini-stats">${['rows','eligible','new','existing','invalid','duplicate','excluded','suppressed'].map((key) => `<span><strong>${number(reviewed.stats[key])}</strong> ${h(key)}</span>`).join('')}</div>${reviewed.errors.length ? `<ul class="bc-error-list">${reviewed.errors.slice(0,8).map((item) => `<li>Row ${number(item.row)}: ${h(item.reason)}</li>`).join('')}</ul><button class="bc-link" data-action="download-errors">Export all excluded rows</button>` : ''}`;
        if (!reviewed.stats.eligible) throw new Error('No eligible recipients remain. Check the mapping, lead mode, pipeline permissions and excluded rows.');
        $('bc-exclusions-label').hidden = reviewed.stats.rows === reviewed.stats.eligible;
        $('bc-exclusions').checked = false;
        $('bc-exclusions-text').textContent = `I acknowledge ${number(reviewed.stats.rows-reviewed.stats.eligible)} rows are excluded and only ${number(reviewed.stats.eligible)} eligible recipients will be prepared.`;
        return reviewed;
    }
    async function loadTemplates() {
        const account = $('bc-account').value;
        if (!account) { $('bc-templates').innerHTML = '<div class="bc-empty">Choose a connected sending number to see its approved templates.</div>'; state.template = null; refreshNext(); return; }
        const search = $('bc-template-search').value, category = $('bc-category').value;
        const params = new URLSearchParams({account,q:search,category});
        const data = await api(`${config.urls.templates}?${params}`);
        if (account !== $('bc-account').value || search !== $('bc-template-search').value || category !== $('bc-category').value) return;
        state.templates = data.templates;
        $('bc-templates').innerHTML = data.templates.length ? data.templates.map((item) => `<button type="button" class="bc-template-option" role="radio" aria-checked="${state.template?.id === item.id}" data-template="${h(item.id)}" ${!item.supported ? 'disabled' : ''}><div class="bc-between"><span class="bc-pill">${h(item.category)} · ${h(item.language)}</span><small class="bc-help">Approved</small></div><h4>${h(item.name)}</h4><p>${h(item.body)}</p><small class="bc-help">Updated ${h(date(item.updated_at))}</small>${item.reason ? `<small class="bc-error-text">${h(item.reason)}</small>` : ''}</button>`).join('') : '<div class="bc-empty"><strong>No approved templates found.</strong>Sync or create templates in WhatsApp → Templates, then refresh this list.</div>';
        if (data.truncated) $('bc-templates').insertAdjacentHTML('beforeend','<p class="bc-help">Showing the first 200 matching templates. Refine your search to find another template.</p>');
        refreshNext();
    }
    function preparePersonalization() {
        state.bindings = JSON.parse(JSON.stringify(state.template.bindings)); resetPreview();
        $('bc-bindings').innerHTML = state.template.fields.length ? state.template.fields.map((field) => `<div class="bc-binding"><strong>${h(field.label)}</strong><div class="bc-two-col"><label class="bc-field">CRM value<select data-binding="${h(field.key)}" data-binding-kind="source">${options(state.meta.sources,state.bindings[field.key]?.source || '','Use fallback only')}</select></label><label class="bc-field">Fallback ${field.kind !== 'text' ? '(HTTPS URL or id:…)' : 'value'}<input type="text" maxlength="2048" data-binding="${h(field.key)}" data-binding-kind="default" value="${h(state.bindings[field.key]?.default || '')}" placeholder="${field.kind !== 'text' ? 'https://… or id:123456' : 'Only when the CRM value is missing'}"></label></div></div>`).join('') : '<p class="bc-help">This template does not require variable values.</p>';
        const account = state.meta.accounts.find((item) => item.id === $('bc-account').value);
        $('bc-preview-account').textContent = account?.business_name || 'WhatsApp Business'; $('bc-preview-number').textContent = account?.phone || '';
        $('bc-final-summary').textContent = `${number(state.audience.stats.eligible)} recipients · ${state.template.category} · ${state.template.language}. This account is fixed for the campaign. No verified price estimate is available.`;
    }
    async function updatePreview() {
        if (!state.upload || !state.template) return;
        const version = ++state.previewVersion; state.preview = null; refreshNext();
        $('bc-preview-status').textContent = 'Checking actual recipient values…';
        const payload = {upload_id:state.upload.id,template_id:state.template.id,bindings:state.bindings};
        const data = await api(config.urls.preview,payload);
        if (version !== state.previewVersion) return;
        state.preview = data;
        $('bc-preview-recipient').innerHTML = options(data.previews.map((item,index) => ({id:String(index),name:`${item.name} · row ${item.row}`})),'');
        $('bc-preview-message').textContent = data.previews[0]?.body || 'No complete recipient preview is available yet.';
        $('bc-preview-status').textContent = data.ready ? `All ${number(data.recipient_count)} recipients have complete parameters. Previewing up to five actual audience rows.` : `${number(data.missing_count)} recipients need values. ${data.errors.slice(0,3).map((item) => `Row ${item.row}: ${item.reason}`).join(' ')}`;
        $('bc-preview-status').classList.toggle('bc-error-text',!data.ready); refreshNext();
    }
    async function confirmCampaign() {
        if (!state.preview?.ready) throw new Error('Update the preview and resolve missing values before sending.');
        const data = await api(config.urls.confirm,{
            upload_id:state.upload.id,template_id:state.template.id,bindings:state.bindings,name:$('bc-name').value,
            audience_digest:state.audience.digest,preview_digest:state.preview.digest,confirmed:true,
            consent_confirmed:$('bc-consent').checked,accept_exclusions:$('bc-exclusions').checked,
            schedule_enabled:$('bc-schedule').checked,scheduled_local:$('bc-scheduled-local').value,timezone:$('bc-timezone').value,
            auto_retry:$('bc-auto-retry').checked,retry_attempts:Number($('bc-retries').value),retry_delay_hours:Number($('bc-retry-delay').value),
        });
        window.location.assign(safeURL(data.url));
    }
    async function nextStep() {
        if (state.busy) return; builderError(); busy(true);
        try {
            if (state.step === 1) setStep(2);
            else if (state.step === 2) { await reviewAudience(); setStep(3); await loadTemplates(); }
            else if (state.step === 3) { if (!state.template) throw new Error('Select an approved template.'); preparePersonalization(); setStep(4); await updatePreview(); }
            else await confirmCampaign();
        } catch (error) { builderError(error.message); } finally { busy(false); }
    }
    function openInspector(title, html, confirm=null, text='Confirm') {
        $('bc-inspector-title').textContent = title; $('bc-inspector-body').innerHTML = html;
        const button = $('bc-inspector-confirm'); button.hidden = !confirm; button.textContent = text; button.disabled = false; state.confirm = confirm;
        if (!inspector.open) inspector.showModal();
    }
    async function retryRecipients(ids) {
        const selected = selection(ids);
        const preview = await api(config.urls.actions,{action:'retry_preview',selection:selected});
        openInspector('Review eligible retries',`<p><strong>${number(preview.eligible)}</strong> of ${number(preview.selected)} selected recipients can be retried.</p><p class="bc-help">${number(preview.blocked)} will not be resent. Delivered, read, replied-to, opted-out, permanent-failure and uncertain recipients are protected.</p>${preview.earliest ? `<p class="bc-help">Earliest allowed retry: ${h(date(preview.earliest,state.report.timezone))}. Configured cooldowns are enforced by the worker.</p>` : ''}${preview.reasons?.length ? `<ul class="bc-error-list">${preview.reasons.map((item) => `<li>${h(item.reason)}</li>`).join('')}</ul>` : ''}`,preview.eligible ? async () => {
            const result = await api(config.urls.actions,{action:'retry',selection:selected}); inspector.close(); state.selected.clear(); state.all=false;
            await loadWorkspace(); notice(`${number(result.queued)} retries scheduled; ${number(result.not_queued)} not queued after final eligibility checks.`);
        } : null,'Schedule eligible retries');
    }
    async function download(url,payload,filename) {
        const blob = await api(url,payload,{blob:true}); const objectURL = URL.createObjectURL(blob);
        const anchor = document.createElement('a'); anchor.href=objectURL; anchor.download=filename; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(objectURL),1000);
    }
    async function action(name,node) {
        if (name === 'create') return showComposer();
        if (name === 'sample') return download(config.urls.sample,undefined,'shvya-campaign-sample.csv');
        if (name === 'templates') return loadTemplates();
        if (name === 'preview') return updatePreview();
        if (name === 'download-errors') return download(state.upload.errors_url,{},'campaign-excluded-rows.csv');
        if (name === 'previous-active' || name === 'next-active') { state.activePage += name === 'previous-active' ? -1 : 1; return loadWorkspace(); }
        if (name === 'previous' || name === 'next-page') { state.page += name === 'previous' ? -1 : 1; return loadWorkspace(); }
        if (name === 'clear-filters') { state.filters={q:'',account:'',status:'',pipeline:'',stage:''}; state.page=1; state.activePage=1; state.selected.clear(); state.all=false; return loadWorkspace(); }
        if (name === 'select-all' || name === 'clear-selection') { state.all=name === 'select-all'; state.selected.clear(); root.querySelectorAll('[data-recipient]').forEach((input) => { input.checked=state.all; }); return renderSelection(); }
        if (name === 'retry') return retryRecipients();
        if (name === 'retry-one') return retryRecipients([node.dataset.id]);
        if (name === 'export') return download(config.urls.actions,{action:'export',selection:selection()},`campaign-${config.campaign_id}-leads.csv`);
        if (name === 'cancel') return openInspector('Cancel remaining messages?', '<p>Unsent and scheduled recipients will be skipped. A message already in flight cannot be recalled. Delivered messages and their evidence remain in this report.</p>', async () => { await api(config.urls.actions,{action:'cancel'}); inspector.close(); await loadWorkspace(); }, 'Cancel remaining');
        if (name === 'rename') return openInspector('Rename this campaign',`<label class="bc-field">Campaign name<input id="bc-rename" maxlength="150" value="${h(state.report.name)}"></label>`,async () => { await api(config.urls.actions,{action:'rename',name:$('bc-rename').value}); inspector.close(); await loadWorkspace(); },'Save name');
        const row = state.rows.find((item) => item.id === node.dataset.id);
        if (!row) return;
        if (name === 'attempts') return openInspector('Send attempt evidence',row.attempts.length ? row.attempts.map((attempt) => `<section class="bc-timeline"><strong>Attempt ${number(attempt.number)}</strong><p class="bc-help">Started ${h(date(attempt.started_at))}<br>Accepted ${h(date(attempt.accepted_at))}<br>Delivered ${h(date(attempt.delivered_at))}<br>Read ${h(date(attempt.read_at))}</p><code>${h(attempt.provider_id || 'No provider message ID recorded')}</code>${attempt.error_message ? `<p class="bc-error-text">${h(attempt.error_code)} · ${h(attempt.error_message)}</p>` : ''}${attempt.uncertain ? '<p class="bc-help">Outcome uncertain. Automatic resends are blocked.</p>' : ''}</section>`).join('') : '<p>No send attempt evidence is available for this recipient.</p>');
        if (name === 'lead') {
            const lead = await api(row.lead_url);
            return openInspector(lead.name,`<dl class="bc-facts"><div><dt>Phone</dt><dd>${h(lead.phone)}</dd></div><div><dt>Email</dt><dd>${h(lead.email || 'Not provided')}</dd></div><div><dt>Pipeline</dt><dd>${h(lead.pipeline)}</dd></div><div><dt>Stage</dt><dd>${h(lead.stage)}</dd></div><div><dt>Lead source</dt><dd>${h(lead.source)}</dd></div><div><dt>Created</dt><dd>${h(date(lead.created_at))}</dd></div>${lead.attributes.map((item) => `<div><dt>${h(item.label)}</dt><dd>${h(item.value == null || item.value === '' ? 'Not provided' : item.value)}</dd></div>`).join('')}</dl>`);
        }
    }
    document.addEventListener('click',(event) => {
        const close=event.target.closest('[data-close]');
        if (close && (!state.busy || close.dataset.close !== 'bc-composer')) { $(close.dataset.close)?.close(); return; }
        const node=event.target.closest('[data-action]');
        if (!node || node.disabled || !node.closest('.bc-ui')) return;
        event.preventDefault(); node.disabled=true;
        Promise.resolve(action(node.dataset.action,node)).catch((error) => composer.open ? builderError(error.message) : notice(error.message,true)).finally(() => { if (node.isConnected) node.disabled=false; });
    });
    $('bc-inspector-confirm').addEventListener('click',async () => {
        const button=$('bc-inspector-confirm'); if (!state.confirm || button.disabled) return; button.disabled=true;
        try { await state.confirm(); } catch (error) { $('bc-inspector-body').insertAdjacentHTML('beforeend',`<p class="bc-error-text" role="alert">${h(error.message)}</p>`); } finally { button.disabled=false; }
    });
    root.addEventListener('input',(event) => {
        const key=event.target.dataset.filter; if (!key || event.target.tagName === 'SELECT') return;
        state.filters[key]=event.target.value; state.page=1; state.activePage=1; state.selected.clear(); state.all=false;
        clearTimeout(filterTimer); filterTimer=setTimeout(() => loadWorkspace(),350);
    });
    root.addEventListener('change',(event) => {
        const key=event.target.dataset.filter;
        if (key) { state.filters[key]=event.target.value; if (key === 'pipeline') state.filters.stage=''; state.page=1; state.activePage=1; state.selected.clear(); state.all=false; loadWorkspace(); return; }
        if (event.target.id === 'bc-select-page') {
            if (!event.target.checked && state.all) { state.all=false; state.selected.clear(); }
            state.rows.forEach((row) => event.target.checked ? state.selected.add(row.id) : state.selected.delete(row.id));
            root.querySelectorAll('[data-recipient]').forEach((node) => { node.checked=event.target.checked; }); renderSelection();
        }
        if (event.target.dataset.recipient) {
            if (state.all) { state.all=false; state.selected=new Set(state.rows.map((row) => row.id)); }
            event.target.checked ? state.selected.add(event.target.dataset.recipient) : state.selected.delete(event.target.dataset.recipient); renderSelection();
        }
    });
    $('bc-next').addEventListener('click',nextStep);
    $('bc-back').addEventListener('click',() => { if (!state.busy) setStep(Math.max(1,state.step-1)); });
    composer.addEventListener('cancel',(event) => { if (state.busy) event.preventDefault(); });
    $('bc-file').addEventListener('change',(event) => uploadFile(event.target.files[0]));
    const drop=$('bc-dropzone');
    ['dragenter','dragover'].forEach((type) => drop.addEventListener(type,(event) => { event.preventDefault(); drop.classList.add('is-dragging'); }));
    ['dragleave','drop'].forEach((type) => drop.addEventListener(type,(event) => { event.preventDefault(); drop.classList.remove('is-dragging'); if (type === 'drop') uploadFile(event.dataTransfer.files[0]); }));
    $('bc-pipeline').addEventListener('change',setStages);
    $('bc-update-existing').addEventListener('change',() => { if (!$('bc-update-existing').checked) $('bc-move-existing').checked=false; });
    $('bc-move-existing').addEventListener('change',() => { if ($('bc-move-existing').checked) $('bc-update-existing').checked=true; });
    $('bc-account').addEventListener('change',() => { state.template=null; resetPreview(); loadTemplates().catch((error) => builderError(error.message)); });
    $('bc-category').addEventListener('change',() => loadTemplates().catch((error) => builderError(error.message)));
    $('bc-template-search').addEventListener('input',() => { clearTimeout(templateTimer); templateTimer=setTimeout(() => loadTemplates().catch((error) => builderError(error.message)),300); });
    $('bc-templates').addEventListener('click',(event) => {
        const node=event.target.closest('[data-template]'); if (!node || node.disabled) return;
        state.template=state.templates.find((item) => item.id === node.dataset.template); resetPreview();
        $('bc-templates').querySelectorAll('[data-template]').forEach((item) => item.setAttribute('aria-checked',String(item.dataset.template === state.template.id))); refreshNext();
    });
    $('bc-bindings').addEventListener('input',(event) => {
        if (!event.target.dataset.binding) return;
        const key=event.target.dataset.binding; state.bindings[key] ||= {}; state.bindings[key][event.target.dataset.bindingKind]=event.target.value;
        resetPreview(); clearTimeout(previewTimer); previewTimer=setTimeout(() => updatePreview().catch((error) => builderError(error.message)),450);
    });
    $('bc-preview-recipient').addEventListener('change',() => { $('bc-preview-message').textContent=state.preview?.previews[Number($('bc-preview-recipient').value)]?.body || ''; });
    ['bc-consent','bc-exclusions','bc-schedule','bc-auto-retry'].forEach((id) => $(id).addEventListener('change',() => { $('bc-schedule-fields').hidden=!$('bc-schedule').checked; $('bc-retry-fields').hidden=!$('bc-auto-retry').checked; refreshNext(); }));
    async function initialize() {
        try {
            state.meta=await api(config.urls.options);
            $('bc-pipeline').innerHTML=options(state.meta.pipelines,'','Choose a pipeline'); setStages();
            $('bc-account').innerHTML=options(state.meta.accounts,'','Choose a sending number');
            $('bc-timezone').value=Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
            root.querySelector('[data-action="create"]')?.toggleAttribute('hidden',!state.meta.can_create);
            if (config.seed?.source === 'crm_selection') await applyCRMSeed(config.seed);
            await loadWorkspace();
            if (config.open_composer) showComposer();
        } catch (error) { notice(error.message,true); root.setAttribute('aria-busy','false'); }
    }
    initialize();
    window.setInterval(() => { if (!document.hidden && !composer.open && !inspector.open && state.meta) loadWorkspace(true); },10000);
})();
