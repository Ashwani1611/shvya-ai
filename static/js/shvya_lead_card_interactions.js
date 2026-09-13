(function () {
    'use strict';

    var STYLE_ID = 'shvya-lead-card-interaction-styles';
    var INLINE_EDITING = '1';
    var manageRefreshTimer = null;

    function isCRM() {
        return document.documentElement.classList.contains('shvya-crm-apple-v2') ||
            !!document.getElementById('lead-table-container');
    }

    function ensureStyles() {
        if (document.getElementById(STYLE_ID)) return;

        var style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = `
            /* Activity + Sequence history use the same calm Apple tab treatment. */
            html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="activity"],
            html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="sequence-history"] {
                min-height: 34px;
                padding: 6px 9px;
                border: 1px solid transparent;
                border-radius: 10px;
                background: transparent;
                color: #6e6e73 !important;
                line-height: 1;
                cursor: pointer;
                transition:
                    background-color 140ms ease,
                    border-color 140ms ease,
                    color 140ms ease,
                    box-shadow 140ms ease;
            }

            html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="activity"]:hover,
            html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="sequence-history"]:hover {
                border-color: rgba(15, 23, 42, 0.07);
                background: #f5f5f7;
                color: #1d1d1f !important;
            }

            html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="activity"].text-blue-600,
            html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="sequence-history"].text-blue-600 {
                border-color: rgba(0, 113, 227, 0.13);
                background: rgba(0, 113, 227, 0.07);
                color: #0071e3 !important;
                box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.72);
            }

            html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="activity"] .lead-tab-chevron,
            html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="sequence-history"] .lead-tab-chevron {
                transition: transform 140ms ease, color 140ms ease;
            }

            /* Attribute overflow actions should be light, never the global dark card-action fill. */
            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button,
            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button.shvya-card-action,
            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button.shvya-card-edit {
                min-height: 38px !important;
                padding: 8px 10px !important;
                border: 1px solid transparent !important;
                border-radius: 10px !important;
                background: transparent !important;
                color: #3a3a3c !important;
                box-shadow: none !important;
                transform: none !important;
                font-size: 12.5px !important;
                font-weight: 560 !important;
            }

            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button i {
                color: #8e8e93 !important;
                transition: color 140ms ease;
            }

            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button:hover,
            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button.shvya-card-action:hover,
            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button.shvya-card-edit:hover {
                border-color: rgba(0, 113, 227, 0.06) !important;
                background: rgba(0, 113, 227, 0.055) !important;
                color: #1d1d1f !important;
                box-shadow: none !important;
                transform: none !important;
            }

            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button:hover i {
                color: #0071e3 !important;
            }

            html.shvya-crm-apple-v2 .lead-card [data-attribute-menu] button:focus-visible {
                outline: none !important;
                border-color: rgba(0, 113, 227, 0.16) !important;
                background: rgba(0, 113, 227, 0.055) !important;
                box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.10) !important;
            }

            /* Attribute value tiles become directly editable. */
            html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile] {
                position: relative;
                cursor: pointer;
                outline: none;
                padding-right: 38px !important;
            }

            html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile]:hover,
            html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile]:focus-visible {
                border-color: rgba(0, 113, 227, 0.16) !important;
                background: #fbfdff !important;
                box-shadow: 0 5px 16px rgba(0, 113, 227, 0.055) !important;
            }

            html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile]:focus-visible {
                box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.10) !important;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-attribute-edit-hint {
                position: absolute;
                top: 10px;
                right: 10px;
                width: 24px;
                height: 24px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border-radius: 999px;
                background: rgba(0, 113, 227, 0.055);
                color: #8e8e93;
                font-size: 13px;
                opacity: 0;
                transform: translateY(1px);
                transition: opacity 140ms ease, background-color 140ms ease, color 140ms ease, transform 140ms ease;
                pointer-events: none;
            }

            html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile]:hover .shvya-attribute-edit-hint,
            html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile]:focus-visible .shvya-attribute-edit-hint {
                opacity: 1;
                color: #0071e3;
                transform: translateY(0);
            }

            html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile][data-shvya-editing="1"] {
                cursor: default;
                padding-right: 13px !important;
                border-color: rgba(0, 113, 227, 0.18) !important;
                background: #fff !important;
                box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.08) !important;
            }

            html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile][data-shvya-editing="1"] .shvya-attribute-edit-hint {
                display: none !important;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-loading {
                margin-top: 7px;
                color: #86868b;
                font-size: 12px;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-editor {
                margin-top: 8px;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-control {
                width: 100%;
                min-height: 38px;
                padding: 8px 10px;
                border: 1px solid rgba(15, 23, 42, 0.11);
                border-radius: 10px;
                background: #f7f7f9;
                color: #1d1d1f;
                font-size: 13px;
                line-height: 1.3;
                outline: none;
                box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.75);
                transition: background-color 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-control:focus {
                border-color: rgba(0, 113, 227, 0.35);
                background: #fff;
                box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.10);
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-actions {
                display: flex;
                align-items: center;
                justify-content: flex-end;
                gap: 7px;
                margin-top: 8px;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-cancel,
            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-save {
                min-height: 30px;
                padding: 0 11px;
                border-radius: 999px;
                font-size: 11.5px;
                font-weight: 650;
                transition: background-color 140ms ease, border-color 140ms ease, color 140ms ease, box-shadow 140ms ease;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-cancel {
                border: 1px solid rgba(15, 23, 42, 0.08);
                background: #f5f5f7;
                color: #6e6e73;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-cancel:hover {
                background: #ededf0;
                color: #1d1d1f;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-save {
                border: 1px solid #0071e3;
                background: #0071e3;
                color: #fff;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-save:hover {
                background: #0077ed;
                box-shadow: 0 3px 9px rgba(0, 113, 227, 0.14);
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-save:disabled {
                cursor: wait;
                opacity: 0.62;
                box-shadow: none;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-error {
                min-height: 15px;
                margin-top: 6px;
                color: #b42318;
                font-size: 11px;
                line-height: 1.35;
            }

            /* Briefly identify the definition that was just updated when returning to Manage Attributes. */
            #modal-root [data-shvya-just-updated] {
                background: rgba(0, 113, 227, 0.045);
                box-shadow: inset 3px 0 0 rgba(0, 113, 227, 0.34);
                transition: background-color 300ms ease, box-shadow 300ms ease;
            }

            @media (max-width: 640px) {
                html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="activity"],
                html.shvya-crm-apple-v2 .lead-card .lead-detail-tab[data-tab-name="sequence-history"] {
                    padding: 6px 7px;
                    font-size: 12px;
                }

                html.shvya-crm-apple-v2 .lead-card [data-shvya-attribute-tile] {
                    padding-right: 36px !important;
                }

                html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-actions {
                    justify-content: stretch;
                }

                html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-cancel,
                html.shvya-crm-apple-v2 .lead-card .shvya-inline-attribute-save {
                    flex: 1;
                    justify-content: center;
                }
            }
        `;
        document.head.appendChild(style);
    }

    function collectCards(root) {
        var cards = [];
        var scope = root || document;

        if (scope.matches && scope.matches('.lead-card')) cards.push(scope);
        if (scope.querySelectorAll) {
            Array.prototype.push.apply(cards, scope.querySelectorAll('.lead-card'));
        }
        return cards;
    }

    function cleanAttributeMenuButtons(root) {
        var scope = root || document;
        if (!scope.querySelectorAll) return;

        scope.querySelectorAll('[data-attribute-menu] button').forEach(function (button) {
            button.classList.remove('shvya-card-action', 'shvya-card-edit');
        });
    }

    function attributeSection(card) {
        if (!card) return null;
        var leadId = (card.id || '').replace('lead-card-', '');
        var details = leadId ? card.querySelector('#details-' + leadId) : null;
        if (!details) return null;

        var lowerGrid = Array.prototype.find.call(details.children, function (child) {
            return child.classList && child.classList.contains('grid');
        });
        return lowerGrid ? lowerGrid.querySelector(':scope > section:first-child') : null;
    }

    function enhanceAttributeTiles(root) {
        collectCards(root).forEach(function (card) {
            var section = attributeSection(card);
            if (!section) return;

            var valueGrid = Array.prototype.find.call(section.children, function (child) {
                return child.classList && child.classList.contains('grid');
            });
            if (!valueGrid) return;

            Array.prototype.forEach.call(valueGrid.children, function (tile) {
                if (!tile || tile.nodeType !== 1 || tile.dataset.shvyaAttributeTile === '1') return;

                var paragraphs = tile.querySelectorAll(':scope > p');
                if (paragraphs.length < 2) return;

                var label = (paragraphs[0].textContent || '').trim();
                if (!label) return;

                tile.dataset.shvyaAttributeTile = '1';
                tile.dataset.attributeLabel = label;
                tile.tabIndex = 0;
                tile.setAttribute('role', 'button');
                tile.setAttribute('aria-label', 'Edit ' + label);

                var hint = document.createElement('span');
                hint.className = 'shvya-attribute-edit-hint';
                hint.setAttribute('aria-hidden', 'true');
                hint.innerHTML = '<i class="ti ti-pencil"></i>';
                tile.appendChild(hint);
            });
        });
    }

    function normalizeText(value) {
        return String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
    }

    function cardLeadId(tile) {
        var card = tile && tile.closest('.lead-card');
        return card ? (card.id || '').replace('lead-card-', '') : '';
    }

    function fullAttributeEditUrl(card) {
        if (!card) return '';
        var button = card.querySelector('[data-attribute-menu] button[hx-get*="/attributes/edit/"]');
        return button ? (button.getAttribute('hx-get') || '') : '';
    }

    function restoreTile(tile) {
        if (!tile) return;
        var editor = tile.querySelector('.shvya-inline-attribute-editor');
        var loading = tile.querySelector('.shvya-inline-attribute-loading');
        var valueNode = tile.querySelector(':scope > p:nth-of-type(2)');

        if (editor) editor.remove();
        if (loading) loading.remove();
        if (valueNode) valueNode.hidden = false;
        delete tile.dataset.shvyaEditing;
        tile.setAttribute('role', 'button');
        tile.tabIndex = 0;
    }

    function closeOtherEditors(card, keepTile) {
        if (!card) return;
        card.querySelectorAll('[data-shvya-attribute-tile][data-shvya-editing="1"]').forEach(function (tile) {
            if (tile !== keepTile) restoreTile(tile);
        });
    }

    function errorText(responseText) {
        var parser = new DOMParser();
        var parsed = parser.parseFromString(responseText || '', 'text/html');
        return (parsed.body.textContent || 'Could not save this attribute.').replace(/\s+/g, ' ').trim();
    }

    function findAttributeControl(parsed, label) {
        var wanted = normalizeText(label);
        var labels = parsed.querySelectorAll('label[for]');
        var found = null;

        Array.prototype.some.call(labels, function (candidate) {
            if (normalizeText(candidate.textContent) !== wanted) return false;
            var id = candidate.getAttribute('for');
            var control = id ? parsed.getElementById(id) : null;
            if (!control || !/^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) return false;
            found = control;
            return true;
        });

        return found;
    }

    function makeInlineEditor(tile, sourceControl, sourceForm) {
        var form = document.createElement('form');
        form.className = 'shvya-inline-attribute-editor';
        form.noValidate = false;
        form.dataset.saveUrl = sourceForm.getAttribute('hx-post') || sourceForm.getAttribute('action') || '';

        var csrfSource = sourceForm.querySelector('input[name="csrfmiddlewaretoken"]');
        if (csrfSource) {
            var csrf = document.createElement('input');
            csrf.type = 'hidden';
            csrf.name = 'csrfmiddlewaretoken';
            csrf.value = csrfSource.value;
            form.appendChild(csrf);
        }

        var control = sourceControl.cloneNode(true);
        control.removeAttribute('id');
        control.className = 'shvya-inline-attribute-control';
        control.setAttribute('aria-label', tile.dataset.attributeLabel || 'Attribute value');
        control.value = sourceControl.value || '';
        form.appendChild(control);

        var error = document.createElement('div');
        error.className = 'shvya-inline-attribute-error';
        error.setAttribute('role', 'alert');
        form.appendChild(error);

        var actions = document.createElement('div');
        actions.className = 'shvya-inline-attribute-actions';

        var cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'shvya-inline-attribute-cancel';
        cancel.dataset.shvyaInlineCancel = '1';
        cancel.textContent = 'Cancel';

        var save = document.createElement('button');
        save.type = 'submit';
        save.className = 'shvya-inline-attribute-save';
        save.textContent = 'Save';

        actions.appendChild(cancel);
        actions.appendChild(save);
        form.appendChild(actions);
        return form;
    }

    async function openInlineEditor(tile) {
        if (!tile || tile.dataset.shvyaEditing === INLINE_EDITING) return;

        var card = tile.closest('.lead-card');
        var editUrl = fullAttributeEditUrl(card);
        if (!card || !editUrl) return;

        closeOtherEditors(card, tile);
        tile.dataset.shvyaEditing = INLINE_EDITING;
        tile.removeAttribute('role');
        tile.tabIndex = -1;

        var valueNode = tile.querySelector(':scope > p:nth-of-type(2)');
        if (valueNode) valueNode.hidden = true;

        var loading = document.createElement('div');
        loading.className = 'shvya-inline-attribute-loading';
        loading.textContent = 'Loading editor…';
        tile.appendChild(loading);

        try {
            var response = await fetch(editUrl, {
                method: 'GET',
                cache: 'no-store',
                credentials: 'same-origin',
                headers: {
                    'HX-Request': 'true',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Cache-Control': 'no-cache'
                }
            });

            if (!response.ok) throw new Error('Could not load this attribute.');

            var html = await response.text();
            var parsed = new DOMParser().parseFromString(html, 'text/html');
            var sourceControl = findAttributeControl(parsed, tile.dataset.attributeLabel || '');
            var sourceForm = sourceControl && sourceControl.closest('form');

            if (!sourceControl || !sourceForm) {
                throw new Error('This attribute editor is unavailable.');
            }

            var editor = makeInlineEditor(tile, sourceControl, sourceForm);
            loading.remove();
            tile.appendChild(editor);

            var control = editor.querySelector('.shvya-inline-attribute-control');
            if (control) {
                control.focus();
                if (control.tagName === 'INPUT' && /^(text|number)$/i.test(control.type || 'text')) {
                    try { control.select(); } catch (ignore) { /* selection is optional */ }
                }
            }
        } catch (error) {
            restoreTile(tile);
            var transient = document.createElement('div');
            transient.className = 'shvya-inline-attribute-error';
            transient.textContent = error.message || 'Could not load this attribute.';
            tile.appendChild(transient);
            window.setTimeout(function () {
                if (transient.parentNode) transient.remove();
            }, 2800);
        }
    }

    function triggerLeadCardRefresh(leadId) {
        if (!leadId) return;

        window.shvyaLeadCardState = window.shvyaLeadCardState || {};
        window.shvyaLeadCardState[leadId] = {
            activePanel: 'details-' + leadId
        };

        if (window.htmx && typeof window.htmx.trigger === 'function') {
            window.htmx.trigger(document.body, 'leadCardUpdated', { lead_id: leadId });
        } else {
            document.body.dispatchEvent(new CustomEvent('leadCardUpdated', {
                detail: { lead_id: leadId }
            }));
        }
    }

    async function saveInlineEditor(form) {
        var tile = form && form.closest('[data-shvya-attribute-tile]');
        if (!tile) return;

        var leadId = cardLeadId(tile);
        var url = form.dataset.saveUrl || '';
        var control = form.querySelector('.shvya-inline-attribute-control');
        var error = form.querySelector('.shvya-inline-attribute-error');
        var save = form.querySelector('.shvya-inline-attribute-save');

        if (!url || !control || !control.name) {
            if (error) error.textContent = 'This attribute cannot be saved right now.';
            return;
        }

        if (save) {
            save.disabled = true;
            save.textContent = 'Saving…';
        }
        if (error) error.textContent = '';

        var data = new FormData();
        var csrf = form.querySelector('input[name="csrfmiddlewaretoken"]');
        if (csrf) data.append('csrfmiddlewaretoken', csrf.value);
        data.append(control.name, control.value);

        try {
            var response = await fetch(url, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'HX-Request': 'true',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: data
            });

            if (!response.ok) {
                var responseText = await response.text();
                throw new Error(errorText(responseText));
            }

            var valueNode = tile.querySelector(':scope > p:nth-of-type(2)');
            if (valueNode) valueNode.textContent = control.value.trim() || '—';
            restoreTile(tile);
            triggerLeadCardRefresh(leadId);
        } catch (saveError) {
            if (error) error.textContent = saveError.message || 'Could not save this attribute.';
            if (save) {
                save.disabled = false;
                save.textContent = 'Save';
            }
        }
    }

    async function reopenManageAttributes(leadId, attributeId) {
        if (!leadId) return;

        var root = document.getElementById('modal-root');
        if (!root) return;

        try {
            var response = await fetch(
                '/dashboard/attributes/manage/?lead_id=' + encodeURIComponent(leadId),
                {
                    method: 'GET',
                    cache: 'no-store',
                    credentials: 'same-origin',
                    headers: {
                        'HX-Request': 'true',
                        'X-Requested-With': 'XMLHttpRequest',
                        'Cache-Control': 'no-cache'
                    }
                }
            );

            if (!response.ok) return;
            root.innerHTML = await response.text();

            if (window.htmx && typeof window.htmx.process === 'function') {
                window.htmx.process(root);
            }

            cleanAttributeMenuButtons(root);

            if (attributeId) {
                var editButton = root.querySelector(
                    'button[hx-get*="/attributes/' + attributeId + '/edit/"]'
                );
                var row = editButton && editButton.closest('.px-5.py-4');
                if (row) {
                    row.setAttribute('data-shvya-just-updated', '1');
                    try { row.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (ignore) { /* optional */ }
                    window.setTimeout(function () {
                        row.removeAttribute('data-shvya-just-updated');
                    }, 1500);
                }
            }
        } catch (ignore) {
            // The attribute save itself succeeded; a refresh failure should not undo it.
        }
    }

    function scheduleManageReopen(event) {
        var detail = event.detail || {};
        var leadId = detail.lead_id || '';
        if (!leadId) return;

        window.clearTimeout(manageRefreshTimer);
        manageRefreshTimer = window.setTimeout(function () {
            reopenManageAttributes(leadId, detail.attribute_id || '');
        }, 220);
    }

    function enhance(root) {
        if (!isCRM()) return;
        ensureStyles();
        cleanAttributeMenuButtons(root || document);
        enhanceAttributeTiles(root || document);
    }

    function init() {
        if (!isCRM()) return;
        enhance(document);

        document.body.addEventListener('click', function (event) {
            var cancel = event.target.closest && event.target.closest('[data-shvya-inline-cancel]');
            if (cancel) {
                event.preventDefault();
                event.stopPropagation();
                restoreTile(cancel.closest('[data-shvya-attribute-tile]'));
                return;
            }

            var tile = event.target.closest && event.target.closest('[data-shvya-attribute-tile]');
            if (!tile || tile.dataset.shvyaEditing === INLINE_EDITING) return;
            if (event.target.closest('button, input, select, textarea, a, form')) return;

            event.preventDefault();
            openInlineEditor(tile);
        });

        document.body.addEventListener('keydown', function (event) {
            var tile = event.target.closest && event.target.closest('[data-shvya-attribute-tile]');
            if (!tile) return;

            if (event.key === 'Escape' && tile.dataset.shvyaEditing === INLINE_EDITING) {
                event.preventDefault();
                restoreTile(tile);
                return;
            }

            if (
                (event.key === 'Enter' || event.key === ' ') &&
                event.target === tile &&
                tile.dataset.shvyaEditing !== INLINE_EDITING
            ) {
                event.preventDefault();
                openInlineEditor(tile);
            }
        });

        document.body.addEventListener('submit', function (event) {
            var form = event.target.closest && event.target.closest('.shvya-inline-attribute-editor');
            if (!form) return;
            event.preventDefault();
            event.stopPropagation();
            saveInlineEditor(form);
        });

        document.body.addEventListener('attributeUpdated', scheduleManageReopen);
        document.body.addEventListener('attributeDeleted', scheduleManageReopen);

        document.body.addEventListener('htmx:afterSwap', function (event) {
            var target = event.detail && event.detail.target;
            window.setTimeout(function () {
                enhance(target || document);
            }, 0);
        });

        new MutationObserver(function (mutations) {
            var relevant = false;
            mutations.forEach(function (mutation) {
                Array.prototype.forEach.call(mutation.addedNodes || [], function (node) {
                    if (!node || node.nodeType !== 1) return;
                    if (
                        node.matches && (node.matches('.lead-card') || node.matches('[data-attribute-menu]')) ||
                        node.querySelector && (node.querySelector('.lead-card') || node.querySelector('[data-attribute-menu]'))
                    ) relevant = true;
                });
            });
            if (relevant) enhance(document);
        }).observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();