(function () {
    'use strict';

    function userName() {
        var meta = document.querySelector('meta[name="shvya-user-name"]');
        return (meta && meta.getAttribute('content') || 'User').trim() || 'User';
    }

    function enhanceHeader() {
        var header = document.querySelector('#app-sidebar + div > header');
        if (!header) return;

        /* Profile remains available from the sidebar account section. */
        header.querySelectorAll('a').forEach(function (link) {
            var text = (link.textContent || '').trim().toLowerCase();
            if (text === 'profile' || link.querySelector('.ti-user')) {
                link.remove();
            }
        });

        var heading = header.querySelector(':scope > h1');
        if (heading && !heading.dataset.shvyaWelcomeEnhanced) {
            var currentText = (heading.textContent || '').trim().toLowerCase();
            if (currentText.indexOf('welcome') !== -1) {
                heading.dataset.shvyaWelcomeEnhanced = '1';
                heading.classList.add('shvya-premium-welcome');
                heading.textContent = '';

                var prefix = document.createElement('span');
                prefix.className = 'shvya-welcome-prefix';
                prefix.textContent = 'Welcome,';

                var name = document.createElement('span');
                name.className = 'shvya-welcome-name';
                name.textContent = userName();

                heading.appendChild(prefix);
                heading.appendChild(name);
            }
        }
    }

    function simplifyAICoins() {
        var badge = document.querySelector('[data-ai-credit-badge]');
        if (!badge) return;

        var value = badge.querySelector('.ai-credit-value');
        var formatted = value ? (value.textContent || '').trim() : '';
        if (!formatted) return;

        var icon = badge.querySelector('.ai-credit-icon');
        var total = badge.querySelector('.ai-credit-total');
        var label = badge.querySelector('.ai-credit-label');

        if (icon) icon.remove();
        if (total) total.remove();
        if (label) label.textContent = 'AI coins';

        badge.title = 'Available AI coins: ' + formatted;
        badge.setAttribute('aria-label', 'AI coins ' + formatted);
    }

    function markNotifications() {
        var main = document.querySelector('#app-sidebar + div > main');
        if (!main) return;

        Array.prototype.forEach.call(main.children, function (child) {
            if (
                child.classList &&
                child.classList.contains('mb-4') &&
                child.classList.contains('space-y-2')
            ) {
                child.classList.add('shvya-dashboard-notifications');
            }
        });
    }

    function markCardActions(root) {
        var scope = root || document;
        scope.querySelectorAll('.lead-card').forEach(function (card) {
            card.querySelectorAll('button[hx-get]').forEach(function (button) {
                var href = (button.getAttribute('hx-get') || '').toLowerCase();
                var text = (button.textContent || '').trim().toLowerCase();

                if (href.indexOf('edit') !== -1 || text === 'edit') {
                    button.classList.add('shvya-card-edit');
                }

                if (
                    href.indexOf('attribute') !== -1 ||
                    href.indexOf('note') !== -1 ||
                    href.indexOf('call') !== -1 ||
                    href.indexOf('reminder') !== -1 ||
                    text.indexOf('add attribute') !== -1 ||
                    text.indexOf('add note') !== -1 ||
                    text.indexOf('add call') !== -1
                ) {
                    button.classList.add('shvya-card-action');
                }
            });

            card.querySelectorAll('select[name="stage"]').forEach(function (select) {
                select.classList.add('shvya-stage-select');
            });

            card.querySelectorAll('h3').forEach(function (heading) {
                var label = (heading.textContent || '').trim().toLowerCase();
                if (label !== 'notes' && label !== 'attributes') return;

                var section = heading.closest('.mt-6, .mt-5, .mt-4, section, [data-detail-section]');
                if (!section) section = heading.parentElement && heading.parentElement.parentElement;
                if (!section || section === card) return;

                section.classList.add('shvya-focus-section');
                section.querySelectorAll('p').forEach(function (p) {
                    var isLabel = p.className.indexOf('text-gray-400') !== -1 ||
                        p.className.indexOf('text-[11px]') !== -1;
                    if (!isLabel) p.classList.add('shvya-focus-value');
                });
            });
        });
    }

    function ensureLeadQuickPanelStyles() {
        if (document.getElementById('shvya-lead-quick-panel-styles')) return;

        var style = document.createElement('style');
        style.id = 'shvya-lead-quick-panel-styles';
        style.textContent = `
            html.shvya-crm-apple-v2 .lead-card .shvya-quick-tab {
                min-height: 34px;
                padding: 6px 9px;
                border: 1px solid transparent;
                border-radius: 10px;
                background: transparent;
                color: #8e8e93 !important;
                font-size: 13px;
                font-weight: 500;
                line-height: 1;
                cursor: pointer;
                transition:
                    background-color 140ms ease,
                    border-color 140ms ease,
                    color 140ms ease,
                    box-shadow 140ms ease;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-tab:hover {
                border-color: rgba(15, 23, 42, 0.07);
                background: #f5f5f7;
                color: #3a3a3c !important;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-tab.text-blue-600 {
                border-color: rgba(0, 113, 227, 0.13);
                background: rgba(0, 113, 227, 0.07);
                color: #0071e3 !important;
                box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.7);
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-panel {
                margin: 12px 20px 16px;
                min-height: 150px;
                max-height: 430px;
                overflow: auto;
                padding: 16px;
                border: 1px solid rgba(15, 23, 42, 0.075);
                border-radius: 18px;
                background: rgba(255, 255, 255, 0.96);
                box-shadow:
                    0 1px 2px rgba(15, 23, 42, 0.025),
                    0 10px 28px rgba(15, 23, 42, 0.05);
                scrollbar-width: thin;
                scrollbar-color: rgba(134, 134, 139, 0.26) transparent;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-panel.hidden {
                display: none !important;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-panel::-webkit-scrollbar {
                width: 5px;
                height: 5px;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-panel::-webkit-scrollbar-thumb {
                border-radius: 999px;
                background: rgba(134, 134, 139, 0.26);
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-section {
                margin: 0 !important;
                border: 0 !important;
                border-radius: 0 !important;
                background: transparent !important;
                padding: 0 !important;
                box-shadow: none !important;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-section > div:first-child {
                margin-bottom: 12px !important;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-panel [data-lead-notes] {
                margin-top: 0 !important;
                border: 1px solid rgba(15, 23, 42, 0.07) !important;
                border-radius: 14px !important;
                background: #f7f7f9 !important;
                padding: 12px !important;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-quick-panel [data-lead-notes] > .mb-2 {
                display: none !important;
            }

            html.shvya-crm-apple-v2 .lead-card .shvya-details-single-column {
                grid-template-columns: minmax(0, 1fr) !important;
            }

            @media (max-width: 640px) {
                html.shvya-crm-apple-v2 .lead-card .shvya-quick-panel {
                    margin: 10px 12px 14px;
                    min-height: 130px;
                    max-height: 380px;
                    padding: 14px;
                    border-radius: 16px;
                }

                html.shvya-crm-apple-v2 .lead-card .shvya-quick-tab {
                    padding: 6px 7px;
                    font-size: 12px;
                }
            }
        `;
        document.head.appendChild(style);
    }

    function findSectionByHeading(panel, label) {
        var headings = panel ? panel.querySelectorAll('h4') : [];
        var match = null;

        Array.prototype.some.call(headings, function (heading) {
            if ((heading.textContent || '').trim().toLowerCase() === label) {
                match = heading.closest('section');
                return true;
            }
            return false;
        });

        return match;
    }

    function findQuickStat(controlRow, iconClass, textNeedle) {
        if (!controlRow) return null;

        return Array.prototype.find.call(controlRow.children, function (node) {
            var text = (node.textContent || '').trim().toLowerCase();
            return (
                !node.classList.contains('lead-detail-tab') &&
                !!node.querySelector('.' + iconClass) &&
                text.indexOf(textNeedle) !== -1
            );
        }) || null;
    }

    function replaceQuickStatWithTab(statNode, targetId, tabName) {
        if (!statNode) return null;

        var label = (statNode.textContent || '').replace(/\s+/g, ' ').trim();
        var icon = statNode.querySelector('i');
        var button = document.createElement('button');
        var labelNode = document.createElement('span');

        button.type = 'button';
        button.className =
            'lead-detail-tab shvya-quick-tab inline-flex items-center gap-1.5 text-gray-500';
        button.setAttribute('data-target', targetId);
        button.setAttribute('data-tab-name', tabName);
        button.setAttribute('data-label', label);
        button.setAttribute('data-open-label', label);
        button.setAttribute('aria-label', 'View ' + label);
        button.setAttribute('aria-expanded', 'false');

        if (icon) {
            button.appendChild(icon.cloneNode(true));
        }

        labelNode.className = 'lead-tab-label';
        labelNode.textContent = label;
        button.appendChild(labelNode);

        statNode.replaceWith(button);
        return button;
    }

    function createQuickPanel(id, label) {
        var panel = document.createElement('div');
        panel.id = id;
        panel.className = 'lead-tab-panel shvya-quick-panel hidden';
        panel.setAttribute('role', 'region');
        panel.setAttribute('aria-label', label);
        return panel;
    }

    function restoreQuickPanelState(card) {
        if (!card || !window.shvyaLeadCardState) return;

        var leadId = card.id.replace('lead-card-', '');
        var state = window.shvyaLeadCardState[leadId];
        var targetId = state && state.activePanel;

        if (!targetId || (
            targetId.indexOf('calls-') !== 0 &&
            targetId.indexOf('notes-') !== 0
        )) {
            return;
        }

        var panel = card.querySelector('[id="' + targetId + '"]');
        var tab = card.querySelector('.lead-detail-tab[data-target="' + targetId + '"]');

        if (!panel || !tab) return;

        card.querySelectorAll('.lead-tab-panel').forEach(function (item) {
            item.classList.add('hidden');
        });

        card.querySelectorAll('.lead-detail-tab').forEach(function (item) {
            item.classList.remove('text-blue-600');
            item.classList.add('text-gray-500');
            if (item.classList.contains('shvya-quick-tab')) {
                item.setAttribute('aria-expanded', 'false');
            }
        });

        panel.classList.remove('hidden');
        tab.classList.remove('text-gray-500');
        tab.classList.add('text-blue-600');
        tab.setAttribute('aria-expanded', 'true');
    }

    function upgradeLeadCardPanels(root) {
        ensureLeadQuickPanelStyles();

        var scope = root || document;
        var cards = [];

        if (scope.matches && scope.matches('.lead-card')) {
            cards.push(scope);
        }

        if (scope.querySelectorAll) {
            Array.prototype.push.apply(cards, scope.querySelectorAll('.lead-card'));
        }

        cards.forEach(function (card) {
            if (card.dataset.shvyaQuickPanelsEnhanced === '1') return;

            var leadId = card.id.replace('lead-card-', '');
            if (!leadId) return;

            var detailsTab = card.querySelector('.lead-detail-tab[data-tab-name="details"]');
            var detailsPanel = card.querySelector('#details-' + leadId);
            var controlRow = detailsTab && detailsTab.parentElement;

            if (!detailsPanel || !controlRow) return;

            var callSection = findSectionByHeading(detailsPanel, 'call tracker');
            var notesSection = findSectionByHeading(detailsPanel, 'notes');

            if (!callSection && !notesSection) {
                card.dataset.shvyaQuickPanelsEnhanced = '1';
                return;
            }

            var callGrid = callSection && callSection.parentElement;
            var callsPanel = null;
            var notesPanel = null;

            if (callSection) {
                callsPanel = createQuickPanel('calls-' + leadId, 'Calls');
                detailsPanel.parentNode.insertBefore(callsPanel, detailsPanel);
                callSection.classList.add('shvya-quick-section');

                var callHeading = callSection.querySelector('h4');
                if (callHeading) callHeading.textContent = 'Calls';

                callsPanel.appendChild(callSection);

                if (callGrid) {
                    callGrid.classList.add('shvya-details-single-column');
                }

                replaceQuickStatWithTab(
                    findQuickStat(controlRow, 'ti-phone', 'call'),
                    callsPanel.id,
                    'calls'
                );
            }

            if (notesSection) {
                notesPanel = createQuickPanel('notes-' + leadId, 'Notes');
                detailsPanel.parentNode.insertBefore(notesPanel, detailsPanel);
                notesSection.classList.add('shvya-quick-section');
                notesPanel.appendChild(notesSection);

                replaceQuickStatWithTab(
                    findQuickStat(controlRow, 'ti-note', 'note'),
                    notesPanel.id,
                    'notes'
                );
            }

            card.dataset.shvyaQuickPanelsEnhanced = '1';

            if (window.htmx && typeof window.htmx.process === 'function') {
                if (callsPanel) window.htmx.process(callsPanel);
                if (notesPanel) window.htmx.process(notesPanel);
            }

            restoreQuickPanelState(card);
        });
    }

    function markLeadDetail(root) {
        var scope = root || document;
        scope.querySelectorAll('#lead-detail-panel').forEach(function (panel) {
            panel.classList.add('shvya-lead-detail-apple');
            panel.querySelectorAll('h3').forEach(function (heading) {
                var label = (heading.textContent || '').trim().toLowerCase();
                if (label !== 'notes' && label !== 'attributes') return;
                var section = heading.closest('.mt-6') || heading.parentElement;
                if (!section) return;
                section.classList.add('shvya-focus-section');
                section.querySelectorAll('p').forEach(function (p) {
                    var isLabel = p.className.indexOf('text-gray-400') !== -1 ||
                        p.className.indexOf('text-[11px]') !== -1;
                    if (!isLabel) p.classList.add('shvya-focus-value');
                });
            });
        });
    }

    function decorateModalRoot() {
        var root = document.getElementById('modal-root');
        if (!root) return;

        var open = root.children.length > 0 && (root.textContent || '').trim().length > 0;
        document.documentElement.classList.toggle('shvya-modal-open', open);
        document.body.classList.toggle('shvya-modal-open', open);

        if (!open) return;

        root.querySelectorAll('[class*="fixed"][class*="inset-0"]').forEach(function (overlay) {
            overlay.classList.add('shvya-apple-modal-overlay');
            var surface = overlay.querySelector('[class*="bg-white"][class*="rounded"], .global-reminder-modal');
            if (surface) surface.classList.add('shvya-modal-surface');
        });

        root.querySelectorAll('button, a').forEach(function (node) {
            var text = (node.textContent || '').trim().toLowerCase();
            if (text === 'apply filters' || text === 'save' || text === 'save note' || text === 'add reminder') {
                node.classList.add('shvya-modal-primary-action');
            }
        });
    }

    function detectPage() {
        if (document.getElementById('pipeline-select') || document.getElementById('lead-table-container')) {
            document.documentElement.classList.add('shvya-crm-apple-v2');
        }

        if (window.location.pathname.toLowerCase().indexOf('/profile') !== -1) {
            document.documentElement.classList.add('shvya-profile-apple-v2');
        }
    }

    function enhance(root) {
        detectPage();
        enhanceHeader();
        simplifyAICoins();
        markNotifications();

        if (document.documentElement.classList.contains('shvya-crm-apple-v2')) {
            markCardActions(root || document);
            upgradeLeadCardPanels(root || document);
            markLeadDetail(root || document);
            decorateModalRoot();
        }
    }

    function init() {
        enhance(document);

        var modalRoot = document.getElementById('modal-root');
        if (modalRoot) {
            new MutationObserver(function () {
                decorateModalRoot();
                markCardActions(modalRoot);
                markLeadDetail(modalRoot);
            }).observe(modalRoot, { childList: true, subtree: true });
        }

        document.body.addEventListener('click', function (event) {
            var quickTab = event.target.closest && event.target.closest('.shvya-quick-tab');
            if (!quickTab) return;

            window.setTimeout(function () {
                var card = quickTab.closest('.lead-card');
                var targetId = quickTab.getAttribute('data-target');
                var panel = card && targetId
                    ? card.querySelector('[id="' + targetId + '"]')
                    : null;

                quickTab.setAttribute(
                    'aria-expanded',
                    panel && !panel.classList.contains('hidden') ? 'true' : 'false'
                );

                if (card) {
                    card.querySelectorAll('.shvya-quick-tab').forEach(function (tab) {
                        if (tab !== quickTab) {
                            tab.setAttribute('aria-expanded', 'false');
                        }
                    });
                }
            }, 0);
        });

        document.body.addEventListener('htmx:afterSwap', function (event) {
            var target = event.detail && event.detail.target;
            enhance(target || document);
        });

        new MutationObserver(function (mutations) {
            var needsHeaderRefresh = false;
            var needsCardRefresh = false;

            mutations.forEach(function (mutation) {
                Array.prototype.forEach.call(mutation.addedNodes || [], function (node) {
                    if (!node || node.nodeType !== 1) return;

                    if (
                        node.matches && node.matches('[data-ai-credit-badge]') ||
                        node.querySelector && node.querySelector('[data-ai-credit-badge]')
                    ) {
                        needsHeaderRefresh = true;
                    }

                    if (
                        node.matches && node.matches('.lead-card') ||
                        node.querySelector && node.querySelector('.lead-card')
                    ) {
                        needsCardRefresh = true;
                    }
                });
            });

            if (needsHeaderRefresh) {
                simplifyAICoins();
                enhanceHeader();
            }

            if (
                needsCardRefresh &&
                document.documentElement.classList.contains('shvya-crm-apple-v2')
            ) {
                markCardActions(document);
                upgradeLeadCardPanels(document);
            }
        }).observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();
