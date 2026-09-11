(function () {
    'use strict';

    var path = window.location.pathname || '';
    var root = document.documentElement;

    function pageMeta() {
        if (path === '/dashboard/sales-desk/' || path.indexOf('/dashboard/sales-desk/') === 0) {
            return {
                key: 'sales-desk',
                title: 'Sales Desk',
                eyebrow: 'Sales workspace',
                description: 'Prioritize leads that need attention and take the next best action.'
            };
        }

        if (path === '/dashboard/insights/' || path.indexOf('/dashboard/insights/') === 0) {
            return {
                key: 'insights',
                title: 'Insights',
                eyebrow: 'Performance',
                description: 'See pipeline performance, activity, and conversion signals at a glance.'
            };
        }

        if (path.indexOf('/dashboard/cadence/') === 0) {
            if (path.indexOf('/sequences/') !== -1) {
                var tail = path.split('/sequences/')[1] || '';
                if (!tail) {
                    return {
                        key: 'cadence',
                        title: 'Sequences',
                        eyebrow: 'Cadence',
                        description: 'Build automated follow-up journeys that keep every lead moving.'
                    };
                }
                if (tail.indexOf('new') === 0 || tail.indexOf('create') === 0) {
                    return {
                        key: 'cadence',
                        title: 'New Sequence',
                        eyebrow: 'Cadence',
                        description: 'Create a focused follow-up sequence in a few clear steps.'
                    };
                }
                return {
                    key: 'cadence',
                    title: 'Sequence Builder',
                    eyebrow: 'Cadence',
                    description: 'Shape timing, channels, and touchpoints for this sequence.'
                };
            }
            return {
                key: 'cadence',
                title: 'Cadence',
                eyebrow: 'Automation',
                description: 'Create timely follow-ups that keep conversations moving.'
            };
        }

        if (path.indexOf('/dashboard/playbooks/') === 0) {
            if (path.indexOf('/ai-setup/') !== -1 || path.indexOf('/ai-brain/') !== -1) {
                return {
                    key: 'playbooks',
                    title: 'AI Brain',
                    eyebrow: 'Playbooks',
                    description: 'Configure how your AI understands, qualifies, and responds to leads.'
                };
            }
            if (path.indexOf('/faq/') !== -1) {
                return {
                    key: 'playbooks',
                    title: 'FAQ',
                    eyebrow: 'Playbooks',
                    description: 'Give your AI clear answers to common customer questions.'
                };
            }
            return {
                key: 'playbooks',
                title: 'Playbooks',
                eyebrow: 'AI knowledge',
                description: 'Shape the knowledge and guidance your AI uses with leads.'
            };
        }

        if (path === '/dashboard/workflows/' || path.indexOf('/dashboard/workflows/') === 0) {
            return {
                key: 'workflows',
                title: 'Workflows',
                eyebrow: 'Automation',
                description: 'Automate lead movement and actions with simple rules.'
            };
        }

        if (path.indexOf('/dashboard/whatsapp/accounts/') === 0) {
            return {
                key: 'whatsapp',
                title: 'Connected Numbers',
                eyebrow: 'WhatsApp',
                description: 'Manage the WhatsApp numbers connected to your workspace.'
            };
        }

        if (path.indexOf('/dashboard/whatsapp/templates/') === 0) {
            return {
                key: 'whatsapp',
                title: 'Templates',
                eyebrow: 'WhatsApp',
                description: 'Create and manage approved WhatsApp message templates.'
            };
        }

        if (path.indexOf('/dashboard/whatsapp/connect/hosted/') === 0) {
            return {
                key: 'whatsapp',
                title: 'Hosted Account',
                eyebrow: 'WhatsApp',
                description: 'Connect and manage your hosted WhatsApp automation.'
            };
        }

        if (path === '/dashboard/connect-hub/' || path.indexOf('/dashboard/connect-hub/') === 0) {
            return {
                key: 'connect-hub',
                title: 'Connect Hub',
                eyebrow: 'Integrations',
                description: 'Connect the tools that move leads and data into Shvya.'
            };
        }

        if (path === '/dashboard/teams/' || path.indexOf('/dashboard/teams/') === 0) {
            return {
                key: 'teams',
                title: 'Teams',
                eyebrow: 'Workspace',
                description: 'Manage members, access, and automation responsibilities.'
            };
        }

        return null;
    }

    var meta = pageMeta();
    if (!meta) return;

    root.classList.add('shvya-apple-workspace-v3', 'shvya-page-' + meta.key);

    function safeText(node) {
        return (node && node.textContent ? node.textContent : '').replace(/\s+/g, ' ').trim();
    }

    function replaceTextNodes(container, replacements) {
        if (!container || !replacements || !replacements.length) return;
        var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
            acceptNode: function (node) {
                var parent = node.parentElement;
                if (!parent || /^(SCRIPT|STYLE|TEXTAREA|OPTION)$/i.test(parent.tagName)) {
                    return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        var nodes = [];
        var current;
        while ((current = walker.nextNode())) nodes.push(current);
        nodes.forEach(function (node) {
            var value = node.nodeValue;
            replacements.forEach(function (pair) {
                value = value.split(pair[0]).join(pair[1]);
            });
            if (value !== node.nodeValue) node.nodeValue = value;
        });
    }

    function setShellTitle() {
        var heading = document.querySelector('#app-sidebar + div > header > h1');
        if (heading) heading.textContent = meta.title;
        document.title = meta.title + ' · SHVYA AI';
    }

    function findMain() {
        return document.querySelector('#app-sidebar + div > main') || document.querySelector('main');
    }

    function hideDuplicateHeading(main) {
        if (!main) return;
        var headings = main.querySelectorAll('h1, h2');
        Array.prototype.forEach.call(headings, function (heading) {
            if (heading.closest('.shvya-page-hero')) return;
            var text = safeText(heading).toLowerCase();
            var title = meta.title.toLowerCase();
            var aliases = [title];
            if (meta.key === 'sales-desk') aliases.push('co-pilot', 'co-pilot monitoring');
            if (meta.key === 'insights') aliases.push('sales analytics', 'analytics');
            if (meta.key === 'workflows') aliases.push('smart triggers');
            if (meta.title === 'AI Brain') aliases.push('ai setup');
            if (meta.key === 'cadence') aliases.push('auto follow-ups');

            if (aliases.indexOf(text) !== -1) {
                heading.classList.add('shvya-legacy-page-heading');
                var sibling = heading.nextElementSibling;
                if (sibling && sibling.tagName === 'P') sibling.classList.add('shvya-legacy-page-heading');
            }
        });
    }

    function insertHero() {
        var main = findMain();
        if (!main || main.querySelector(':scope > .shvya-page-hero')) return;

        var hero = document.createElement('section');
        hero.className = 'shvya-page-hero';
        hero.setAttribute('aria-labelledby', 'shvya-workspace-page-title');

        var eyebrow = document.createElement('div');
        eyebrow.className = 'shvya-page-hero__eyebrow';
        eyebrow.textContent = meta.eyebrow;

        var title = document.createElement('h1');
        title.id = 'shvya-workspace-page-title';
        title.textContent = meta.title;

        var description = document.createElement('p');
        description.textContent = meta.description;

        hero.appendChild(eyebrow);
        hero.appendChild(title);
        hero.appendChild(description);
        main.insertBefore(hero, main.firstChild);
        hideDuplicateHeading(main);
    }

    function renameVisibleProductCopy(scope) {
        scope = scope || document;
        var replacements = [];

        if (meta.key === 'sales-desk') {
            replacements = [
                ['Co-Pilot Monitoring', 'Sales Desk'],
                ['Co-Pilot Settings', 'Sales Desk Settings'],
                ['Co-Pilot Active', 'Sales Desk Active'],
                ['Co-Pilot Inactive', 'Sales Desk Inactive'],
                ['Enable Co-Pilot', 'Enable Sales Desk'],
                ['Turn on Co-Pilot', 'Turn on Sales Desk'],
                ['Co-Pilot', 'Sales Desk']
            ];
        } else if (meta.key === 'insights') {
            replacements = [
                ['Sales Analytics', 'Insights'],
                ['Analytics Settings', 'Insights Settings']
            ];
        } else if (meta.key === 'workflows') {
            replacements = [
                ['How Smart Triggers work', 'How Workflows work'],
                ['Smart Triggers', 'Workflows']
            ];
        } else if (meta.key === 'cadence') {
            replacements = [
                ['Auto Follow-ups', 'Cadence'],
                ['Auto Follow-Ups', 'Cadence']
            ];
        } else if (meta.key === 'playbooks' && meta.title === 'AI Brain') {
            replacements = [
                ['Chat Playground', 'AI Sandbox'],
                ['AI Setup', 'AI Brain']
            ];
        }

        replaceTextNodes(scope, replacements);
    }

    function decorateSandbox() {
        if (!(meta.key === 'playbooks' && meta.title === 'AI Brain')) return;
        var messages = document.getElementById('playground-messages');
        if (!messages) return;

        var headings = document.querySelectorAll('#ai-setup-page h2, #ai-setup-page h3, #ai-setup-page h4');
        var title = null;
        Array.prototype.some.call(headings, function (node) {
            if (safeText(node) === 'AI Sandbox' || safeText(node) === 'Chat Playground') {
                title = node;
                return true;
            }
            return false;
        });

        if (title) {
            title.textContent = 'AI Sandbox';
            title.classList.add('shvya-ai-sandbox-title');
            var titleParent = title.parentElement;
            var candidates = titleParent ? titleParent.querySelectorAll('p') : [];
            var description = candidates.length ? candidates[0] : null;
            if (description) {
                description.textContent = 'Configure, test, and refine your AI before going live.';
                description.classList.add('shvya-ai-sandbox-description');
            }
        }

        var node = messages.parentElement;
        var card = null;
        while (node && node.id !== 'ai-setup-page') {
            var className = typeof node.className === 'string' ? node.className : '';
            if ((className.indexOf('rounded') !== -1 || node.tagName === 'SECTION') &&
                node.querySelector && node.querySelector('#playground-message-input')) {
                card = node;
                if (className.indexOf('border') !== -1 || node.tagName === 'SECTION') break;
            }
            node = node.parentElement;
        }

        if (!card && title) card = title.closest('section') || title.parentElement;
        if (card) {
            card.classList.add('shvya-ai-sandbox-card');
            if (card.parentElement) card.parentElement.classList.add('shvya-ai-brain-layout');
        }
    }

    function removeCallingFromConnectHub() {
        if (meta.key !== 'connect-hub') return;
        var groups = document.querySelectorAll('[data-connect-group]');
        Array.prototype.forEach.call(groups, function (group) {
            var heading = group.querySelector('h2, h3, h4');
            var label = safeText(heading).toLowerCase();
            if (label === 'calling' || label === 'calls') group.remove();
        });
    }

    function decorateModals(scope) {
        scope = scope || document;
        var overlays = scope.querySelectorAll ? scope.querySelectorAll('.fixed.inset-0[role="dialog"], #modal-root > .fixed.inset-0, [id*="modal"].fixed.inset-0') : [];
        Array.prototype.forEach.call(overlays, function (overlay) {
            overlay.classList.add('shvya-workspace-modal-backdrop');
            var panel = overlay.firstElementChild;
            if (panel) panel.classList.add('shvya-workspace-modal-panel');
        });
    }

    function shortenKnownDescriptions() {
        if (meta.key === 'connect-hub') {
            var main = findMain();
            if (!main) return;
            var headings = main.querySelectorAll('h2');
            Array.prototype.forEach.call(headings, function (heading) {
                if (safeText(heading) === 'Connect Hub') {
                    var p = heading.nextElementSibling;
                    if (p && p.tagName === 'P') p.textContent = meta.description;
                }
            });
        }
    }

    function apply() {
        setShellTitle();
        insertHero();
        renameVisibleProductCopy(document);
        decorateSandbox();
        removeCallingFromConnectHub();
        decorateModals(document);
        shortenKnownDescriptions();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', apply, { once: true });
    } else {
        apply();
    }

    /* HTMX inserts most dashboard modals after initial load. Keep the visual
       system and product naming consistent without changing any HTMX behavior. */
    var observer = new MutationObserver(function (mutations) {
        var touched = false;
        mutations.forEach(function (mutation) {
            if (mutation.addedNodes && mutation.addedNodes.length) touched = true;
        });
        if (!touched) return;
        window.requestAnimationFrame(function () {
            renameVisibleProductCopy(document);
            decorateSandbox();
            removeCallingFromConnectHub();
            decorateModals(document);
        });
    });

    if (document.documentElement) {
        observer.observe(document.documentElement, { childList: true, subtree: true });
    }
})();
