(function () {
    'use strict';

    function isCRM() {
        return document.documentElement.classList.contains('shvya-crm-apple-v2') ||
            !!document.getElementById('lead-table-container');
    }

    function definitionEditMeta(form) {
        if (!form) return null;

        var action = form.getAttribute('hx-post') || form.getAttribute('action') || '';
        var leadInput = form.querySelector('input[name="lead_id"]');
        var leadId = leadInput ? (leadInput.value || '').trim() : '';
        if (!action || !leadId) return null;

        var path;
        try {
            path = new URL(action, window.location.origin).pathname;
        } catch (error) {
            return null;
        }

        var match = path.match(/^\/dashboard\/attributes\/([0-9a-f-]+)\/edit\/save\/$/i);
        if (!match) return null;

        return {
            action: action,
            leadId: leadId,
            attributeId: match[1]
        };
    }

    function responseMessage(html, fallback) {
        try {
            var parsed = new DOMParser().parseFromString(html || '', 'text/html');
            var text = (parsed.body.textContent || '').replace(/\s+/g, ' ').trim();
            return text || fallback;
        } catch (error) {
            return fallback;
        }
    }

    function showFormError(form, message) {
        var alert = form.querySelector('[data-shvya-definition-save-error]');
        if (!alert) {
            alert = document.createElement('div');
            alert.dataset.shvyaDefinitionSaveError = '1';
            alert.setAttribute('role', 'alert');
            alert.className = 'rounded-xl border border-red-100 bg-red-50 px-3 py-2 text-xs text-red-700';
            form.insertBefore(alert, form.firstChild);
        }
        alert.textContent = message;
    }

    function resetSubmitButton(button, label) {
        if (!button) return;
        button.disabled = false;
        if (label) button.innerHTML = label;
    }

    function restorePanelState(card, leadId, activePanelId) {
        if (!card) return;

        var targetId = activePanelId || ('details-' + leadId);
        card.querySelectorAll('.lead-tab-panel').forEach(function (panel) {
            panel.classList.add('hidden');
        });

        card.querySelectorAll('.lead-detail-tab').forEach(function (tab) {
            tab.classList.remove('text-blue-600');
            tab.classList.add('text-gray-500');

            var icon = tab.querySelector('.lead-tab-chevron');
            if (icon) {
                icon.classList.remove('ti-chevron-up');
                icon.classList.add('ti-chevron-down');
            }

            var label = tab.querySelector('.lead-tab-label');
            if (label) {
                label.textContent = tab.dataset.label ||
                    (tab.dataset.tabName === 'activity' ? 'Activity' :
                        (tab.dataset.tabName === 'sequence-history' ? 'Sequence history' : 'Details'));
            }
        });

        var panel = card.querySelector('[id="' + targetId + '"]');
        var tab = card.querySelector('.lead-detail-tab[data-target="' + targetId + '"]');
        if (!panel || !tab) return;

        panel.classList.remove('hidden');
        tab.classList.remove('text-gray-500');
        tab.classList.add('text-blue-600');

        var activeIcon = tab.querySelector('.lead-tab-chevron');
        if (activeIcon) {
            activeIcon.classList.remove('ti-chevron-down');
            activeIcon.classList.add('ti-chevron-up');
        }

        var activeLabel = tab.querySelector('.lead-tab-label');
        if (activeLabel) {
            activeLabel.textContent = tab.dataset.openLabel ||
                (tab.dataset.tabName === 'activity' ? 'Hide activity' :
                    (tab.dataset.tabName === 'sequence-history' ? 'Hide sequence history' : 'Hide details'));
        }
    }

    async function refreshLeadCardPreservingModal(leadId) {
        var currentCard = document.getElementById('lead-card-' + leadId);
        if (!currentCard) return;

        window.shvyaLeadCardState = window.shvyaLeadCardState || {};
        var savedState = window.shvyaLeadCardState[leadId] || {
            activePanel: 'details-' + leadId
        };
        var activePanelId = savedState.activePanel || ('details-' + leadId);

        var response = await fetch(
            '/dashboard/leads/' + encodeURIComponent(leadId) + '/card/?_=' + Date.now(),
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

        var html = await response.text();
        var parsed = new DOMParser().parseFromString(html, 'text/html');
        var newCard = parsed.querySelector('#lead-card-' + leadId);
        if (!newCard) return;

        currentCard.replaceWith(newCard);
        if (window.htmx && typeof window.htmx.process === 'function') {
            window.htmx.process(newCard);
        }

        window.shvyaLeadCardState[leadId] = { activePanel: activePanelId };

        window.setTimeout(function () {
            var insertedCard = document.getElementById('lead-card-' + leadId);
            restorePanelState(insertedCard, leadId, activePanelId);
        }, 0);
    }

    async function reopenManageAttributes(leadId, attributeId) {
        var root = document.getElementById('modal-root');
        if (!root) throw new Error('Modal container is unavailable.');

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
        if (!response.ok) throw new Error('Could not reopen Manage Attributes.');

        root.innerHTML = await response.text();
        if (window.htmx && typeof window.htmx.process === 'function') {
            window.htmx.process(root);
        }

        if (!attributeId) return;
        var editButton = root.querySelector(
            'button[hx-get*="/attributes/' + attributeId + '/edit/"]'
        );
        var row = editButton && editButton.closest('.px-5.py-4');
        if (!row) return;

        row.setAttribute('data-shvya-just-updated', '1');
        try {
            row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        } catch (error) {
            // Scrolling is a progressive enhancement only.
        }
        window.setTimeout(function () {
            row.removeAttribute('data-shvya-just-updated');
        }, 1500);
    }

    async function saveDefinition(form, meta) {
        var submit = form.querySelector('button[type="submit"]');
        var originalLabel = submit ? submit.innerHTML : '';
        var existingError = form.querySelector('[data-shvya-definition-save-error]');
        if (existingError) existingError.remove();

        if (submit) {
            submit.disabled = true;
            submit.textContent = 'Saving…';
        }

        try {
            var response = await fetch(meta.action, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'HX-Request': 'true',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: new FormData(form)
            });

            if (!response.ok) {
                var errorHtml = await response.text();
                throw new Error(responseMessage(errorHtml, 'Could not save this attribute.'));
            }

            // Keep the overlay open: replace Edit Attribute directly with the
            // refreshed Manage Attributes list instead of allowing HTMX to
            // swap the empty success response into #modal-root.
            await reopenManageAttributes(meta.leadId, meta.attributeId);

            // Refresh only the lead card in place. This intentionally avoids
            // the legacy leadCardUpdated handler because that handler clears
            // #modal-root before doing its card refresh.
            refreshLeadCardPreservingModal(meta.leadId).catch(function () {
                // The definition is already saved; the next card refresh will
                // reconcile any display text if this non-critical refresh fails.
            });
        } catch (error) {
            showFormError(form, error.message || 'Could not save this attribute.');
            resetSubmitButton(submit, originalLabel);
        }
    }

    function init() {
        if (!isCRM()) return;

        // Capture phase is deliberate: stop HTMX from swapping the empty
        // successful definition-update response into #modal-root. That keeps
        // the user inside the Manage Attributes workflow after Save.
        document.addEventListener('submit', function (event) {
            var form = event.target;
            if (!form || form.tagName !== 'FORM') return;

            var meta = definitionEditMeta(form);
            if (!meta) return;

            event.preventDefault();
            event.stopImmediatePropagation();
            saveDefinition(form, meta);
        }, true);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();