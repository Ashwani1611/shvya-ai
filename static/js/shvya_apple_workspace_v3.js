(function () {
    'use strict';

    var path = window.location.pathname || '';
    var root = document.documentElement;

    function resolveMeta() {
        if (path.indexOf('/dashboard/sales-desk/') === 0) {
            return { key: 'sales-desk', title: 'Sales Desk', eyebrow: 'Sales workspace', description: 'Prioritize leads that need attention and take the next best action.' };
        }
        if (path.indexOf('/dashboard/insights/') === 0) {
            return { key: 'insights', title: 'Insights', eyebrow: 'Performance', description: 'See pipeline performance, activity, and conversion signals at a glance.' };
        }
        if (path.indexOf('/dashboard/cadence/') === 0) {
            if (path.indexOf('/sequences/') !== -1) {
                var tail = path.split('/sequences/')[1] || '';
                if (!tail) return { key: 'cadence', title: 'Sequences', eyebrow: 'Cadence', description: 'Build automated follow-up journeys that keep every lead moving.' };
                if (tail.indexOf('new') === 0 || tail.indexOf('create') === 0) {
                    return { key: 'cadence', title: 'New Sequence', eyebrow: 'Cadence', description: 'Create a focused follow-up sequence in a few clear steps.' };
                }
                return { key: 'cadence', title: 'Sequence Builder', eyebrow: 'Cadence', description: 'Shape timing, channels, and touchpoints for this sequence.' };
            }
            return { key: 'cadence', title: 'Cadence', eyebrow: 'Automation', description: 'Create timely follow-ups that keep conversations moving.' };
        }
        if (path.indexOf('/dashboard/playbooks/') === 0) {
            if (path.indexOf('/ai-setup/') !== -1 || path.indexOf('/ai-brain/') !== -1) {
                return { key: 'playbooks', title: 'AI Brain', eyebrow: 'Playbooks', description: 'Configure how your AI understands, qualifies, and responds to leads.' };
            }
            if (path.indexOf('/faq/') !== -1) {
                return { key: 'playbooks', title: 'FAQ', eyebrow: 'Playbooks', description: 'Give your AI clear answers to common customer questions.' };
            }
            return { key: 'playbooks', title: 'Playbooks', eyebrow: 'AI knowledge', description: 'Shape the knowledge and guidance your AI uses with leads.' };
        }
        if (path.indexOf('/dashboard/workflows/') === 0) {
            return { key: 'workflows', title: 'Workflows', eyebrow: 'Automation', description: 'Automate lead movement and actions with simple rules.' };
        }
        if (path.indexOf('/dashboard/whatsapp/accounts/') === 0) {
            return { key: 'whatsapp', title: 'Connected Numbers', eyebrow: 'WhatsApp', description: 'Manage the WhatsApp numbers connected to your workspace.' };
        }
        if (path.indexOf('/dashboard/whatsapp/templates/') === 0) {
            return { key: 'whatsapp', title: 'Templates', eyebrow: 'WhatsApp', description: 'Create and manage approved WhatsApp message templates.' };
        }
        if (path.indexOf('/dashboard/whatsapp/connect/hosted/') === 0) {
            return { key: 'whatsapp', title: 'Hosted Account', eyebrow: 'WhatsApp', description: 'Connect and manage your hosted WhatsApp automation.' };
        }
        if (path.indexOf('/dashboard/connect-hub/') === 0) {
            return { key: 'connect-hub', title: 'Connect Hub', eyebrow: 'Integrations', description: 'Connect the tools that move leads and data into Shvya.' };
        }
        if (path.indexOf('/dashboard/teams/') === 0) {
            return { key: 'teams', title: 'Teams', eyebrow: 'Workspace', description: 'Manage members, access, and automation responsibilities.' };
        }
        return null;
    }

    var meta = resolveMeta();
    if (!meta) return;
    root.classList.add('shvya-apple-workspace-v3', 'shvya-page-' + meta.key);

    function text(node) {
        return (node && node.textContent ? node.textContent : '').replace(/\s+/g, ' ').trim();
    }

    function mainNode() {
        return document.querySelector('#app-sidebar + div > main') || document.querySelector('main');
    }

    function replaceText(container, pairs) {
        if (!container || !pairs.length) return;
        var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
            acceptNode: function (node) {
                var parent = node.parentElement;
                if (!parent || /^(SCRIPT|STYLE|TEXTAREA|OPTION)$/i.test(parent.tagName)) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
            }
        });
        var nodes = [], node;
        while ((node = walker.nextNode())) nodes.push(node);
        nodes.forEach(function (item) {
            var value = item.nodeValue;
            pairs.forEach(function (pair) { value = value.split(pair[0]).join(pair[1]); });
            if (value !== item.nodeValue) item.nodeValue = value;
        });
    }

    function renameProductCopy() {
        var pairs = [];
        if (meta.key === 'sales-desk') {
            pairs = [
                ['Co-Pilot Monitoring', 'Sales Desk'],
                ['Co-Pilot Settings', 'Sales Desk Settings'],
                ['Co-Pilot Active', 'Sales Desk Active'],
                ['Co-Pilot Inactive', 'Sales Desk Inactive'],
                ['Enable Co-Pilot', 'Enable Sales Desk'],
                ['Turn on Co-Pilot', 'Turn on Sales Desk'],
                ['Co-Pilot', 'Sales Desk']
            ];
        } else if (meta.key === 'insights') {
            pairs = [['Sales Analytics', 'Insights'], ['Analytics Settings', 'Insights Settings']];
        } else if (meta.key === 'workflows') {
            pairs = [['How Smart Triggers work', 'How Workflows work'], ['Smart Triggers', 'Workflows']];
        } else if (meta.key === 'cadence') {
            pairs = [['Auto Follow-ups', 'Cadence'], ['Auto Follow-Ups', 'Cadence']];
        } else if (meta.key === 'playbooks') {
            pairs = [['AI Setup', 'AI Brain']];
            if (meta.title === 'AI Brain') pairs.unshift(['Chat Playground', 'AI Sandbox']);
        }
        replaceText(document, pairs);
    }

    function setShellTitle() {
        var heading = document.querySelector('#app-sidebar + div > header > h1');
        if (heading && text(heading) !== meta.title) heading.textContent = meta.title;
        document.title = meta.title + ' · SHVYA AI';
    }

    function hideDuplicateHeadings(main) {
        if (!main) return;
        var aliases = [meta.title.toLowerCase()];
        if (meta.key === 'sales-desk') aliases.push('co-pilot', 'co-pilot monitoring', 'sales desk');
        if (meta.key === 'insights') aliases.push('sales analytics', 'analytics');
        if (meta.key === 'workflows') aliases.push('smart triggers');
        if (meta.title === 'AI Brain') aliases.push('ai setup');
        if (meta.key === 'cadence') aliases.push('auto follow-ups', 'cadence');
        Array.prototype.forEach.call(main.querySelectorAll('h1, h2'), function (heading) {
            if (heading.closest('.shvya-page-hero')) return;
            if (aliases.indexOf(text(heading).toLowerCase()) === -1) return;
            heading.classList.add('shvya-legacy-page-heading');
            var sibling = heading.nextElementSibling;
            if (sibling && sibling.tagName === 'P') sibling.classList.add('shvya-legacy-page-heading');
        });
    }

    function ensureHero() {
        var main = mainNode();
        if (!main) return;
        if (!main.querySelector(':scope > .shvya-page-hero')) {
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
        }
        hideDuplicateHeadings(main);
    }

    function ensureSandbox() {
        if (!(meta.key === 'playbooks' && meta.title === 'AI Brain')) return;
        var messages = document.getElementById('playground-messages');
        if (!messages) return;

        var title = null;
        Array.prototype.some.call(document.querySelectorAll('#ai-setup-page h2, #ai-setup-page h3, #ai-setup-page h4'), function (candidate) {
            var value = text(candidate);
            if (value === 'AI Sandbox' || value === 'Chat Playground') {
                title = candidate;
                return true;
            }
            return false;
        });

        if (title) {
            if (text(title) !== 'AI Sandbox') title.textContent = 'AI Sandbox';
            title.classList.add('shvya-ai-sandbox-title');
            var titleParent = title.parentElement;
            var description = titleParent ? titleParent.querySelector('p') : null;
            var desired = 'Configure, test, and refine your AI before going live.';
            if (description) {
                if (text(description) !== desired) description.textContent = desired;
                description.classList.add('shvya-ai-sandbox-description');
            }
        }

        var cursor = messages.parentElement;
        var card = null;
        while (cursor && cursor.id !== 'ai-setup-page') {
            var className = typeof cursor.className === 'string' ? cursor.className : '';
            if (cursor.querySelector && cursor.querySelector('#playground-message-input') &&
                (className.indexOf('rounded') !== -1 || cursor.tagName === 'SECTION')) {
                card = cursor;
                if (className.indexOf('border') !== -1 || cursor.tagName === 'SECTION') break;
            }
            cursor = cursor.parentElement;
        }
        if (!card && title) card = title.closest('section') || title.parentElement;
        if (card) {
            card.classList.add('shvya-ai-sandbox-card');
            if (card.parentElement) card.parentElement.classList.add('shvya-ai-brain-layout');
        }
    }

    function removeCalling() {
        if (meta.key !== 'connect-hub') return;
        Array.prototype.forEach.call(document.querySelectorAll('[data-connect-group]'), function (group) {
            var heading = group.querySelector('h2, h3, h4');
            var label = text(heading).toLowerCase();
            if (label === 'calls' || label === 'calling') group.remove();
        });
    }

    function decorateModals() {
        var selector = '.fixed.inset-0[role="dialog"], #modal-root > .fixed.inset-0, [id*="modal"].fixed.inset-0';
        Array.prototype.forEach.call(document.querySelectorAll(selector), function (overlay) {
            overlay.classList.add('shvya-workspace-modal-backdrop');
            if (overlay.firstElementChild) overlay.firstElementChild.classList.add('shvya-workspace-modal-panel');
        });
    }

    function shortenDescriptions() {
        if (meta.key !== 'connect-hub') return;
        Array.prototype.forEach.call((mainNode() || document).querySelectorAll('h2'), function (heading) {
            if (text(heading) !== 'Connect Hub') return;
            var p = heading.nextElementSibling;
            if (p && p.tagName === 'P' && text(p) !== meta.description) p.textContent = meta.description;
        });
    }

    function apply() {
        setShellTitle();
        ensureHero();
        renameProductCopy();
        ensureSandbox();
        removeCalling();
        decorateModals();
        shortenDescriptions();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply, { once: true });
    else apply();

    var scheduled = false;
    var observer = new MutationObserver(function (mutations) {
        var added = mutations.some(function (mutation) { return mutation.addedNodes && mutation.addedNodes.length; });
        if (!added || scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(function () {
            scheduled = false;
            renameProductCopy();
            ensureSandbox();
            removeCalling();
            decorateModals();
        });
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
})();
