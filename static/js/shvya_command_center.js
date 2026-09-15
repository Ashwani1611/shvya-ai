(function () {
    'use strict';

    var STATE_KEY = 'shvya-command-center-state-v1';
    var ACTION_PARAM = 'shvya_action';
    var MAX_RESULTS = 18;
    var MAX_RECENTS = 6;

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function icon(className, extraClass) {
        var node = el('i', 'ti ' + (className || 'ti-arrow-right') + (extraClass ? ' ' + extraClass : ''));
        node.setAttribute('aria-hidden', 'true');
        return node;
    }

    function parseJsonScript(id, fallback) {
        var node = document.getElementById(id);
        if (!node) return fallback;
        try {
            return JSON.parse(node.textContent || 'null') || fallback;
        } catch (error) {
            return fallback;
        }
    }

    function getMeta(name, fallback) {
        var node = document.querySelector('meta[name="' + name + '"]');
        return node ? node.getAttribute('content') || fallback : fallback;
    }

    function normalize(value) {
        var text = String(value || '').toLowerCase();
        if (text.normalize) {
            text = text.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
        }
        return text
            .replace(/&/g, ' and ')
            .replace(/[^a-z0-9+]+/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    function safeStorageGet(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (error) {
            return null;
        }
    }

    function safeStorageSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (error) {
            // Storage restrictions must never break navigation.
        }
    }

    function loadState() {
        try {
            var parsed = JSON.parse(safeStorageGet(STATE_KEY) || '{}');
            return parsed && typeof parsed === 'object' ? parsed : {};
        } catch (error) {
            return {};
        }
    }

    function saveUsage(item) {
        if (!item || !item.id) return;
        var state = loadState();
        var entry = state[item.id] || { count: 0, lastUsed: 0 };
        entry.count += 1;
        entry.lastUsed = Date.now();
        state[item.id] = entry;

        var keys = Object.keys(state);
        if (keys.length > 80) {
            keys.sort(function (a, b) {
                return (state[b].lastUsed || 0) - (state[a].lastUsed || 0);
            });
            keys.slice(80).forEach(function (key) { delete state[key]; });
        }
        safeStorageSet(STATE_KEY, JSON.stringify(state));
    }

    function appendQuery(url, key, value) {
        if (!url) return '';
        try {
            var resolved = new URL(url, window.location.origin);
            resolved.searchParams.set(key, value);
            return resolved.pathname + resolved.search + resolved.hash;
        } catch (error) {
            return url;
        }
    }

    function cleanUrl(url) {
        try {
            var resolved = new URL(url, window.location.origin);
            return resolved.pathname + resolved.search + resolved.hash;
        } catch (error) {
            return url;
        }
    }

    function joinUrl(base, suffix) {
        if (!base) return '';
        try {
            var resolved = new URL(base, window.location.origin);
            var path = resolved.pathname;
            if (!path.endsWith('/')) path += '/';
            resolved.pathname = path + String(suffix || '').replace(/^\/+/, '');
            return resolved.pathname + resolved.search + resolved.hash;
        } catch (error) {
            return base;
        }
    }

    function itemText(item) {
        var keywords = Array.isArray(item.keywords) ? item.keywords.join(' ') : (item.keywords || '');
        return normalize([
            item.title,
            item.meta,
            item.category,
            item.parent,
            keywords
        ].join(' '));
    }

    function flattenNavigation(sections, utilities) {
        var items = [];

        (sections || []).forEach(function (section) {
            (section.items || []).forEach(function (item) {
                var label = item.sidebar_label || item.label || '';
                var parentHref = item.href || ((item.children || [])[0] || {}).href;
                if (label && parentHref) {
                    items.push({
                        id: 'nav:' + section.key + ':' + label,
                        title: label,
                        meta: section.label || 'Workspace',
                        category: 'Pages',
                        icon: item.icon,
                        href: parentHref,
                        keywords: item.search_keywords || [],
                        kind: 'navigation'
                    });
                }

                (item.children || []).forEach(function (child) {
                    if (!child.href) return;
                    items.push({
                        id: 'nav:' + label + ':' + child.label,
                        title: child.sidebar_label || child.label,
                        meta: label,
                        category: label === 'WhatsApp' || label === 'Instagram' ? 'Chats & channels' : 'Pages',
                        icon: child.icon,
                        href: child.href,
                        keywords: [label].concat(child.search_keywords || []),
                        kind: 'navigation',
                        parent: label
                    });
                });
            });
        });

        (utilities || []).forEach(function (item) {
            if (!item.href) return;
            items.push({
                id: 'utility:' + item.label,
                title: item.sidebar_label || item.label,
                meta: 'Account',
                category: 'Settings & account',
                icon: item.icon,
                href: item.href,
                keywords: item.search_keywords || [],
                kind: 'navigation'
            });
        });

        var profileUrl = getMeta('shvya-profile-url', '');
        if (profileUrl) {
            items.push({
                id: 'utility:settings',
                title: 'Settings',
                meta: 'Profile & account settings',
                category: 'Settings & account',
                icon: 'ti-settings',
                href: profileUrl,
                keywords: ['settings', 'profile', 'account', 'preferences'],
                kind: 'navigation'
            });
        }

        return items;
    }

    function findNavigation(navigation, title, parent) {
        return navigation.find(function (item) {
            if (normalize(item.title) !== normalize(title)) return false;
            return !parent || normalize(item.parent) === normalize(parent);
        });
    }

    function buildActionRegistry(navigation) {
        var actions = [];
        var crm = findNavigation(navigation, 'CRM');
        var sequences = findNavigation(navigation, 'Sequences', 'Cadence');
        var workflows = findNavigation(navigation, 'Workflows');
        var whatsappChats = findNavigation(navigation, 'Chats', 'WhatsApp');
        var whatsappTemplates = findNavigation(navigation, 'Templates', 'WhatsApp');
        var broadcasts = findNavigation(navigation, 'Broadcasts', 'WhatsApp');
        var connectedNumbers = findNavigation(navigation, 'Connected Numbers', 'WhatsApp');
        var connectApi = findNavigation(navigation, 'Connect API', 'WhatsApp');
        var hosted = findNavigation(navigation, 'Hosted Account', 'WhatsApp');
        var instagramChats = findNavigation(navigation, 'Chats', 'Instagram');
        var instagramConnect = findNavigation(navigation, 'Connect Instagram', 'Instagram');
        var settings = findNavigation(navigation, 'Settings');

        function add(item) {
            if (!item || !item.href) return;
            actions.push(item);
        }

        if (crm) {
            add({
                id: 'action:new-lead',
                title: 'Create new lead',
                meta: 'CRM · Create',
                category: 'Create',
                icon: 'ti-user-plus',
                href: crm.href,
                actionKey: 'new-lead',
                selector: 'button[hx-get*="leads/create"], a[hx-get*="leads/create"]',
                keywords: ['new', 'create', 'add', 'lead', 'customer', 'prospect'],
                kind: 'action'
            });
            add({
                id: 'action:import-leads',
                title: 'Import leads',
                meta: 'CRM · CSV / spreadsheet',
                category: 'Create',
                icon: 'ti-upload',
                href: crm.href,
                actionKey: 'import-leads',
                selector: 'button[hx-get*="leads/import"], a[hx-get*="leads/import"]',
                keywords: ['import', 'upload', 'csv', 'excel', 'spreadsheet', 'leads'],
                kind: 'action'
            });
            add({
                id: 'action:reminders',
                title: 'Open reminders',
                meta: 'CRM · Follow-ups due',
                category: 'CRM actions',
                icon: 'ti-bell',
                href: crm.href,
                actionKey: 'reminders',
                selector: 'a[hx-get*="reminders"], button[hx-get*="reminders"]',
                keywords: ['reminders', 'tasks', 'follow up', 'pending', 'due'],
                kind: 'action'
            });
        }

        if (sequences) {
            add({
                id: 'action:create-cadence',
                title: 'Create cadence sequence',
                meta: 'Cadence · New sequence',
                category: 'Create',
                icon: 'ti-repeat',
                href: joinUrl(sequences.href, 'new/'),
                keywords: ['cadence', 'sequence', 'follow up', 'followup', 'automation', 'create', 'new'],
                kind: 'action'
            });
        }

        if (workflows) {
            add({
                id: 'action:create-workflow',
                title: 'Create workflow',
                meta: 'Workflows · Automation',
                category: 'Create',
                icon: 'ti-target-arrow',
                href: workflows.href,
                actionKey: 'create-workflow',
                selector: '#st-new',
                keywords: ['workflow', 'automation', 'trigger', 'rule', 'create', 'new'],
                kind: 'action'
            });
        }

        if (whatsappChats) {
            add({
                id: 'chat:whatsapp-ai',
                title: 'WhatsApp API / AI chats',
                meta: 'WhatsApp · Inbox',
                category: 'Chats & channels',
                icon: 'ti-brand-whatsapp',
                href: whatsappChats.href,
                keywords: ['whatsapp', 'wa', 'api', 'ai', 'chat', 'chats', 'inbox', 'messages', 'conversation'],
                kind: 'navigation'
            });
            add({
                id: 'chat:coexistence',
                title: 'Coexistence chats',
                meta: 'WhatsApp Business App · Cloud API inbox',
                category: 'Chats & channels',
                icon: 'ti-message-circle',
                href: whatsappChats.href,
                keywords: ['coexistence', 'coex', 'business app', 'whatsapp business app', 'chat', 'inbox', 'messages'],
                kind: 'navigation'
            });
        }

        if (connectApi) {
            add({
                id: 'action:connect-whatsapp-api',
                title: 'Connect WhatsApp Business API',
                meta: 'WhatsApp · Connection setup',
                category: 'Connect',
                icon: 'ti-plug-connected',
                href: connectApi.href,
                keywords: ['connect', 'whatsapp', 'api', 'cloud api', 'meta', 'number'],
                kind: 'action'
            });
            add({
                id: 'action:connect-coexistence',
                title: 'Set up WhatsApp Coexistence',
                meta: 'WhatsApp Business App · Embedded Signup',
                category: 'Connect',
                icon: 'ti-arrows-exchange',
                href: cleanUrl(connectApi.href).replace(/\/api\/?$/, '/coexistence/'),
                keywords: ['coexistence', 'coex', 'whatsapp business app', 'connect', 'embedded signup', 'meta'],
                kind: 'action'
            });
        }

        if (connectedNumbers) {
            add({
                id: 'action:connected-numbers',
                title: 'Manage connected WhatsApp numbers',
                meta: 'WhatsApp · Accounts',
                category: 'Settings & account',
                icon: 'ti-device-mobile-check',
                href: connectedNumbers.href,
                keywords: ['whatsapp', 'numbers', 'accounts', 'settings', 'automation', 'welcome message'],
                kind: 'navigation'
            });
        }

        if (whatsappTemplates) {
            add({
                id: 'action:create-template',
                title: 'Create WhatsApp template',
                meta: 'WhatsApp · Message template',
                category: 'Create',
                icon: 'ti-file-plus',
                href: joinUrl(whatsappTemplates.href, 'new/'),
                keywords: ['template', 'message template', 'whatsapp', 'create', 'new', 'meta template'],
                kind: 'action'
            });
        }

        if (broadcasts) {
            add({
                id: 'action:create-broadcast',
                title: 'Create WhatsApp broadcast',
                meta: 'WhatsApp · Campaign',
                category: 'Create',
                icon: 'ti-speakerphone',
                href: joinUrl(broadcasts.href, 'new/'),
                keywords: ['broadcast', 'campaign', 'whatsapp', 'create', 'send bulk'],
                kind: 'action'
            });
        }

        if (hosted) {
            add({
                id: 'chat:hosted',
                title: 'Hosted chats',
                meta: 'Hosted Account · Choose a connected session',
                category: 'Chats & channels',
                icon: 'ti-server',
                href: hosted.href,
                keywords: ['hosted', 'hosted account', 'chat', 'chats', 'linked device', 'whatsapp web', 'inbox'],
                kind: 'navigation'
            });
            add({
                id: 'action:connect-hosted',
                title: 'Connect Hosted Account',
                meta: 'WhatsApp · Linked-device setup',
                category: 'Connect',
                icon: 'ti-qrcode',
                href: hosted.href,
                keywords: ['hosted', 'connect', 'qr', 'whatsapp web', 'linked device'],
                kind: 'action'
            });
        }

        if (instagramChats) {
            add({
                id: 'chat:instagram',
                title: 'Instagram chats',
                meta: 'Instagram · Direct messages',
                category: 'Chats & channels',
                icon: 'ti-brand-instagram',
                href: instagramChats.href,
                keywords: ['instagram', 'insta', 'dm', 'dms', 'chat', 'chats', 'inbox', 'messages'],
                kind: 'navigation'
            });
        }

        if (instagramConnect) {
            add({
                id: 'action:connect-instagram',
                title: 'Connect Instagram',
                meta: 'Instagram · Meta OAuth',
                category: 'Connect',
                icon: 'ti-brand-instagram',
                href: instagramConnect.href,
                keywords: ['instagram', 'connect', 'oauth', 'meta', 'account'],
                kind: 'action'
            });
        }

        if (settings) {
            add({
                id: 'action:settings',
                title: 'Open settings',
                meta: 'Profile & account',
                category: 'Settings & account',
                icon: 'ti-settings',
                href: settings.href,
                keywords: ['settings', 'profile', 'account', 'preferences'],
                kind: 'navigation'
            });
        }

        return actions;
    }

    function elementLabel(node) {
        var label = node.getAttribute('data-shvya-command-label')
            || node.getAttribute('aria-label')
            || node.getAttribute('title')
            || node.textContent
            || '';
        return String(label).replace(/\s+/g, ' ').trim().slice(0, 90);
    }

    function isVisible(node) {
        if (!node || !node.getBoundingClientRect) return false;
        var style = window.getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        var rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    function discoverPageActions() {
        var root = document.querySelector('main') || document.querySelector('[role="main"]') || document.body;
        var nodes = root.querySelectorAll('a[href], button, [role="button"], [data-shvya-command-label]');
        var seen = new Set();
        var items = [];

        nodes.forEach(function (node, index) {
            if (node.closest('.shvya-command-center-layer, #app-sidebar, .shvya-command-layer')) return;
            if (!isVisible(node) || node.disabled) return;

            var label = elementLabel(node);
            if (label.length < 2 || label.length > 90) return;
            var normalizedLabel = normalize(label);
            if (!normalizedLabel || normalizedLabel === 'close' || normalizedLabel === 'cancel') return;

            var href = '';
            if (node.tagName === 'A') {
                href = node.getAttribute('href') || '';
                if (!href || href === '#' || href.indexOf('javascript:') === 0) return;
                try {
                    var resolved = new URL(href, window.location.origin);
                    if (resolved.origin !== window.location.origin) return;
                    href = resolved.pathname + resolved.search + resolved.hash;
                } catch (error) {
                    return;
                }
            }

            var signature = normalizedLabel + '|' + href;
            if (seen.has(signature)) return;
            seen.add(signature);

            items.push({
                id: 'page:' + normalize(window.location.pathname) + ':' + index + ':' + normalizedLabel,
                title: label,
                meta: 'On this page',
                category: 'On this page',
                icon: node.querySelector && node.querySelector('.ti')
                    ? Array.from(node.querySelector('.ti').classList).find(function (name) { return name.indexOf('ti-') === 0; })
                    : 'ti-pointer',
                href: href,
                node: node,
                keywords: ['current page', 'page action', label],
                kind: 'page-action'
            });
        });

        return items.slice(0, 60);
    }

    function fuzzyScore(text, query) {
        if (!query) return 0;
        var position = 0;
        var matched = 0;
        for (var i = 0; i < query.length; i += 1) {
            var found = text.indexOf(query.charAt(i), position);
            if (found === -1) break;
            matched += 1;
            position = found + 1;
        }
        if (!matched) return 0;
        return Math.round((matched / query.length) * 18);
    }

    function usageBoost(item, state) {
        var entry = state[item.id];
        if (!entry) return 0;
        var countBoost = Math.min(Number(entry.count || 0), 8) * 2;
        var ageHours = (Date.now() - Number(entry.lastUsed || 0)) / 3600000;
        var recencyBoost = ageHours < 2 ? 18 : ageHours < 24 ? 10 : ageHours < 168 ? 5 : 0;
        return countBoost + recencyBoost;
    }

    function scoreItem(item, rawQuery, state) {
        var query = normalize(rawQuery);
        if (!query) return usageBoost(item, state);

        var title = normalize(item.title);
        var meta = normalize(item.meta);
        var category = normalize(item.category);
        var text = itemText(item);
        var tokens = query.split(' ').filter(Boolean);
        var score = 0;

        if (title === query) score += 160;
        if (title.indexOf(query) === 0) score += 105;
        else if (title.indexOf(query) !== -1) score += 76;
        if (text.indexOf(query) !== -1) score += 45;
        if (meta.indexOf(query) !== -1) score += 24;
        if (category.indexOf(query) !== -1) score += 18;

        var tokenMatches = 0;
        tokens.forEach(function (token) {
            if (title.indexOf(token) !== -1) {
                score += 32;
                tokenMatches += 1;
            } else if (text.indexOf(token) !== -1) {
                score += 18;
                tokenMatches += 1;
            } else {
                score += fuzzyScore(text, token);
            }
        });

        if (tokens.length > 1 && tokenMatches === tokens.length) score += 35;
        if (item.kind === 'action' && /^(create|new|add|build|connect|setup|set up)/.test(query)) score += 15;
        if (item.category === 'Chats & channels' && /chat|message|inbox|dm|conversation/.test(query)) score += 18;
        if (item.category === 'Settings & account' && /setting|profile|account|preference/.test(query)) score += 15;

        score += usageBoost(item, state);
        return score;
    }

    function dedupe(items) {
        var seen = new Set();
        return items.filter(function (item) {
            var key = item.id || (item.title + '|' + item.href);
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }

    function buildSearchActions(queryValue, navigation) {
        var query = String(queryValue || '').trim();
        if (normalize(query).length < 2) return [];

        var items = [];
        var crm = findNavigation(navigation, 'CRM');
        var whatsappChats = findNavigation(navigation, 'Chats', 'WhatsApp');
        var instagramChats = findNavigation(navigation, 'Chats', 'Instagram');

        if (crm) {
            items.push({
                id: 'search:crm:' + normalize(query),
                title: 'Search CRM for “' + query + '”',
                meta: 'Leads · name, phone or email',
                category: 'Search',
                icon: 'ti-users',
                href: appendQuery(crm.href, 'search', query),
                keywords: [query],
                kind: 'search'
            });
        }
        if (whatsappChats) {
            items.push({
                id: 'search:whatsapp:' + normalize(query),
                title: 'Search WhatsApp chats for “' + query + '”',
                meta: 'WhatsApp API & Coexistence conversations',
                category: 'Search',
                icon: 'ti-brand-whatsapp',
                href: appendQuery(whatsappChats.href, 'q', query),
                keywords: [query],
                kind: 'search'
            });
        }
        if (instagramChats) {
            items.push({
                id: 'search:instagram:' + normalize(query),
                title: 'Search Instagram chats for “' + query + '”',
                meta: 'Instagram conversations',
                category: 'Search',
                icon: 'ti-brand-instagram',
                href: appendQuery(instagramChats.href, 'q', query),
                keywords: [query],
                kind: 'search'
            });
        }
        return items;
    }

    function createCommandCenter(navigation) {
        var actionRegistry = buildActionRegistry(navigation);
        var layer = el('div', 'shvya-command-center-layer');
        layer.setAttribute('aria-hidden', 'true');

        var panel = el('div', 'shvya-command-center-panel');
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
        panel.setAttribute('aria-label', 'Search and jump to anything');

        var header = el('div', 'shvya-command-center-header');
        var inputWrap = el('div', 'shvya-command-center-input-wrap');
        inputWrap.appendChild(icon('ti-search', 'shvya-command-center-search-icon'));
        var input = el('input', 'shvya-command-center-input');
        input.type = 'search';
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.placeholder = 'Search pages, chats, settings or actions…';
        input.setAttribute('aria-label', 'Search SHVYA dashboard');
        inputWrap.appendChild(input);
        inputWrap.appendChild(el('span', 'shvya-command-center-esc', 'ESC'));
        header.appendChild(inputWrap);

        var suggestions = el('div', 'shvya-command-center-suggestions');
        suggestions.setAttribute('aria-label', 'Smart suggestions');
        header.appendChild(suggestions);

        var resultsNode = el('div', 'shvya-command-center-results');
        resultsNode.setAttribute('role', 'listbox');

        var footer = el('div', 'shvya-command-center-footer');
        footer.appendChild(el('span', 'shvya-command-center-key', '↑↓ Navigate'));
        footer.appendChild(el('span', 'shvya-command-center-key', '↵ Open'));
        footer.appendChild(el('span', 'shvya-command-center-key', 'Tab Suggest'));
        footer.appendChild(el('span', 'shvya-command-center-key shvya-command-center-footer-count', ''));

        panel.appendChild(header);
        panel.appendChild(resultsNode);
        panel.appendChild(footer);
        layer.appendChild(panel);
        document.body.appendChild(layer);

        var currentResults = [];
        var selectedIndex = 0;
        var openState = false;
        var suggestionItems = [];

        function isOpen() {
            return openState;
        }

        function close() {
            if (!openState) return;
            openState = false;
            layer.classList.remove('is-open');
            layer.setAttribute('aria-hidden', 'true');
            document.body.style.removeProperty('overflow');
        }

        function open() {
            document.querySelectorAll('.shvya-command-layer.is-open').forEach(function (legacy) {
                legacy.classList.remove('is-open');
                legacy.setAttribute('aria-hidden', 'true');
            });
            openState = true;
            layer.classList.add('is-open');
            layer.setAttribute('aria-hidden', 'false');
            document.body.style.overflow = 'hidden';
            selectedIndex = 0;
            render(input.value);
            window.requestAnimationFrame(function () {
                input.focus({ preventScroll: true });
                input.select();
            });
        }

        function targetPath(item) {
            if (!item || !item.href) return '';
            try {
                return new URL(item.href, window.location.origin).pathname;
            } catch (error) {
                return '';
            }
        }

        function clickSelector(selector) {
            if (!selector) return false;
            var node = document.querySelector(selector);
            if (!node || node.disabled) return false;
            node.click();
            return true;
        }

        function execute(item) {
            if (!item) return;
            saveUsage(item);

            if (item.kind === 'page-action' && item.node && document.contains(item.node)) {
                close();
                item.node.click();
                return;
            }

            if (item.selector && item.actionKey) {
                var pathname = targetPath(item);
                if (pathname && pathname === window.location.pathname && clickSelector(item.selector)) {
                    close();
                    return;
                }
                if (item.href) {
                    window.location.assign(appendQuery(item.href, ACTION_PARAM, item.actionKey));
                    return;
                }
            }

            if (item.href) {
                window.location.assign(item.href);
            }
        }

        function recentItems(allItems) {
            var state = loadState();
            return allItems
                .filter(function (item) { return state[item.id] && state[item.id].lastUsed; })
                .sort(function (a, b) {
                    return state[b.id].lastUsed - state[a.id].lastUsed;
                })
                .slice(0, MAX_RECENTS);
        }

        function recommendedItems(allItems) {
            var ids = [
                'action:new-lead',
                'chat:whatsapp-ai',
                'chat:hosted',
                'chat:instagram',
                'action:create-cadence',
                'action:create-workflow',
                'action:create-template',
                'action:settings'
            ];
            return ids.map(function (id) {
                return allItems.find(function (item) { return item.id === id; });
            }).filter(Boolean).slice(0, 8);
        }

        function renderSuggestionChip(item) {
            var chip = el('button', 'shvya-command-center-suggestion');
            chip.type = 'button';
            chip.appendChild(icon(item.icon || 'ti-sparkles'));
            chip.appendChild(el('span', '', item.title));
            chip.addEventListener('click', function () { execute(item); });
            return chip;
        }

        function renderSuggestions(query, ranked, allItems) {
            suggestions.textContent = '';
            suggestionItems = [];

            var label = el('span', 'shvya-command-center-suggestion-label', query ? 'Suggested' : 'Quick picks');
            suggestions.appendChild(label);

            if (query) {
                suggestionItems = ranked.filter(function (item) {
                    return item.category !== 'Search';
                }).slice(0, 4);
            } else {
                suggestionItems = recommendedItems(allItems).slice(0, 4);
            }

            suggestionItems.forEach(function (item) {
                suggestions.appendChild(renderSuggestionChip(item));
            });
        }

        function resultBadge(item) {
            if (item.kind === 'action') return 'Action';
            if (item.kind === 'search') return 'Search';
            if (item.kind === 'page-action') return 'Here';
            return '';
        }

        function renderResult(item, index) {
            var row = el('button', 'shvya-command-center-result' + (index === selectedIndex ? ' is-selected' : ''));
            row.type = 'button';
            row.setAttribute('role', 'option');
            row.setAttribute('aria-selected', index === selectedIndex ? 'true' : 'false');
            row.appendChild(icon(item.icon || 'ti-arrow-right', 'shvya-command-center-result-icon'));

            var copy = el('span', 'shvya-command-center-result-copy');
            var titleRow = el('span', 'shvya-command-center-result-title-row');
            titleRow.appendChild(el('span', 'shvya-command-center-result-title', item.title));
            var badge = resultBadge(item);
            if (badge) titleRow.appendChild(el('span', 'shvya-command-center-result-badge', badge));
            copy.appendChild(titleRow);
            copy.appendChild(el('span', 'shvya-command-center-result-meta', item.meta || item.category || ''));
            row.appendChild(copy);
            row.appendChild(icon(item.kind === 'action' ? 'ti-arrow-right' : 'ti-arrow-up-right', 'shvya-command-center-result-arrow'));

            row.addEventListener('mouseenter', function () {
                selectedIndex = index;
                refreshSelection();
            });
            row.addEventListener('click', function () { execute(item); });
            return row;
        }

        function refreshSelection() {
            var rows = resultsNode.querySelectorAll('.shvya-command-center-result');
            rows.forEach(function (row, index) {
                var selected = index === selectedIndex;
                row.classList.toggle('is-selected', selected);
                row.setAttribute('aria-selected', selected ? 'true' : 'false');
            });
            if (rows[selectedIndex]) rows[selectedIndex].scrollIntoView({ block: 'nearest' });
        }

        function appendGroup(title, items) {
            if (!items.length) return;
            resultsNode.appendChild(el('div', 'shvya-command-center-heading', title));
            items.forEach(function (item) {
                var index = currentResults.length;
                currentResults.push(item);
                resultsNode.appendChild(renderResult(item, index));
            });
        }

        function render(queryValue) {
            var query = normalize(queryValue);
            var state = loadState();
            var pageActions = discoverPageActions();
            var baseItems = dedupe(actionRegistry.concat(navigation).concat(pageActions));
            var searchActions = buildSearchActions(queryValue, navigation);
            var ranked = [];

            resultsNode.textContent = '';
            currentResults = [];

            if (query) {
                ranked = baseItems
                    .map(function (item) {
                        return { item: item, score: scoreItem(item, query, state) };
                    })
                    .filter(function (entry) { return entry.score >= 18; })
                    .sort(function (a, b) {
                        if (b.score !== a.score) return b.score - a.score;
                        return String(a.item.title).localeCompare(String(b.item.title));
                    })
                    .map(function (entry) { return entry.item; });

                renderSuggestions(query, ranked, baseItems);

                var best = ranked.slice(0, Math.max(0, MAX_RESULTS - searchActions.length));
                appendGroup('Smart results', best);
                appendGroup('Search inside SHVYA', searchActions);
            } else {
                var recents = recentItems(baseItems);
                var quick = recommendedItems(baseItems).filter(function (item) {
                    return !recents.some(function (recent) { return recent.id === item.id; });
                });
                var pages = navigation.filter(function (item) {
                    return !quick.some(function (quickItem) { return quickItem.id === item.id; });
                }).slice(0, 7);

                renderSuggestions('', quick, baseItems);
                appendGroup('Recent', recents);
                appendGroup('Recommended', quick.slice(0, 8));
                appendGroup('Jump to', pages);
            }

            currentResults = currentResults.slice(0, MAX_RESULTS);
            selectedIndex = Math.min(selectedIndex, Math.max(currentResults.length - 1, 0));

            var countNode = footer.querySelector('.shvya-command-center-footer-count');
            if (countNode) {
                countNode.textContent = currentResults.length ? currentResults.length + ' results' : '';
            }

            if (!currentResults.length) {
                resultsNode.appendChild(el(
                    'div',
                    'shvya-command-center-empty',
                    'No direct match. Try a page, chat, action, setting, lead name, phone number or email.'
                ));
            }
        }

        input.addEventListener('input', function () {
            selectedIndex = 0;
            render(input.value);
        });

        input.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                if (currentResults.length) {
                    selectedIndex = (selectedIndex + 1) % currentResults.length;
                    refreshSelection();
                }
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                if (currentResults.length) {
                    selectedIndex = (selectedIndex - 1 + currentResults.length) % currentResults.length;
                    refreshSelection();
                }
            } else if (event.key === 'Enter') {
                event.preventDefault();
                execute(currentResults[selectedIndex]);
            } else if (event.key === 'Tab' && suggestionItems.length) {
                event.preventDefault();
                input.value = suggestionItems[0].title;
                selectedIndex = 0;
                render(input.value);
            } else if (event.key === 'Escape') {
                event.preventDefault();
                close();
            }
        });

        layer.addEventListener('mousedown', function (event) {
            if (event.target === layer) close();
        });

        return {
            open: open,
            close: close,
            isOpen: isOpen,
            actions: actionRegistry
        };
    }

    function runPendingAction(actions) {
        var params = new URLSearchParams(window.location.search || '');
        var actionKey = params.get(ACTION_PARAM);
        if (!actionKey) return;

        var action = actions.find(function (item) {
            return item.actionKey === actionKey && item.selector;
        });
        if (!action) return;

        var attempts = 0;
        var delays = [0, 120, 320, 700];

        function cleanParameter() {
            try {
                var url = new URL(window.location.href);
                url.searchParams.delete(ACTION_PARAM);
                window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
            } catch (error) {
                // Keep the current URL if History API is unavailable.
            }
        }

        function tryAction() {
            var node = document.querySelector(action.selector);
            if (node && !node.disabled) {
                cleanParameter();
                node.click();
                return;
            }
            attempts += 1;
            if (attempts < delays.length) window.setTimeout(tryAction, delays[attempts]);
            else cleanParameter();
        }

        window.setTimeout(tryAction, delays[0]);
    }

    function init() {
        var sections = parseJsonScript('shvya-sidebar-sections', []);
        var utilities = parseJsonScript('shvya-sidebar-utilities', []);
        if (!sections.length) return;

        var navigation = flattenNavigation(sections, utilities);
        var center = createCommandCenter(navigation);
        runPendingAction(center.actions);

        document.addEventListener('click', function (event) {
            var trigger = event.target.closest && event.target.closest('.shvya-search-trigger');
            if (!trigger) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            center.open();
        }, true);

        document.addEventListener('keydown', function (event) {
            var modifier = event.metaKey || event.ctrlKey;
            if (modifier && String(event.key || '').toLowerCase() === 'k') {
                event.preventDefault();
                event.stopImmediatePropagation();
                if (center.isOpen()) center.close();
                else center.open();
                return;
            }
            if (event.key === 'Escape' && center.isOpen()) {
                event.preventDefault();
                event.stopImmediatePropagation();
                center.close();
            }
        }, true);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();