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

        document.body.addEventListener('htmx:afterSwap', function (event) {
            var target = event.detail && event.detail.target;
            enhance(target || document);
        });

        new MutationObserver(function (mutations) {
            var needsHeaderRefresh = mutations.some(function (mutation) {
                return Array.prototype.some.call(mutation.addedNodes || [], function (node) {
                    return node && node.nodeType === 1 && (
                        node.matches && node.matches('[data-ai-credit-badge]') ||
                        node.querySelector && node.querySelector('[data-ai-credit-badge]')
                    );
                });
            });
            if (needsHeaderRefresh) {
                simplifyAICoins();
                enhanceHeader();
            }
        }).observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();
