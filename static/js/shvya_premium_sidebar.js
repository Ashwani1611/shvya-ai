(function () {
    'use strict';

    var SIDEBAR_STORAGE_KEY = 'shvya-sidebar-collapsed';
    var RECENT_STORAGE_KEY = 'shvya-command-recent';
    var MAX_RECENTS = 4;

    function parseJsonScript(id, fallback) {
        var node = document.getElementById(id);
        if (!node) return fallback;
        try {
            return JSON.parse(node.textContent || 'null') || fallback;
        } catch (error) {
            console.warn('SHVYA shell: invalid JSON payload', id, error);
            return fallback;
        }
    }

    function getMeta(name, fallback) {
        var node = document.querySelector('meta[name="' + name + '"]');
        return node ? node.getAttribute('content') || fallback : fallback;
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
            // Private browsing/storage restrictions should never break navigation.
        }
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function icon(className, extraClass) {
        var node = el('i', 'ti ' + (className || 'ti-circle') + (extraClass ? ' ' + extraClass : ''));
        node.setAttribute('aria-hidden', 'true');
        return node;
    }

    function normalized(value) {
        return String(value || '').toLowerCase().trim();
    }

    function initials(name) {
        var parts = String(name || 'User')
            .trim()
            .split(/\s+/)
            .filter(Boolean)
            .slice(0, 2);

        return (parts.map(function (part) { return part.charAt(0); }).join('') || 'U').toUpperCase();
    }

    function appendQuery(url, key, value) {
        try {
            var resolved = new URL(url, window.location.origin);
            resolved.searchParams.set(key, value);
            return resolved.pathname + resolved.search + resolved.hash;
        } catch (error) {
            return url;
        }
    }

    function setCollapsed(sidebar, collapsed) {
        sidebar.classList.toggle('sidebar-collapsed', collapsed);
        document.documentElement.classList.toggle('shvya-sidebar-pref-collapsed', collapsed);
        safeStorageSet(SIDEBAR_STORAGE_KEY, collapsed ? '1' : '0');

        var toggle = sidebar.querySelector('[data-shvya-sidebar-toggle]');
        if (toggle) {
            toggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
            toggle.setAttribute('title', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
            toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        }
    }

    function createRow(item, options) {
        options = options || {};
        var hasHref = Boolean(item.href);
        var row = el(hasHref ? 'a' : 'button', 'shvya-nav-row' + (options.child ? ' shvya-nav-child' : ''));
        var label = item.sidebar_label || item.label || '';

        if (hasHref) {
            row.href = item.href;
        } else {
            row.type = 'button';
        }

        if (item.is_active) row.classList.add('is-active');
        row.dataset.tooltip = label;
        row.title = label;
        row.appendChild(icon(item.icon, 'shvya-nav-icon'));
        row.appendChild(el('span', 'shvya-nav-copy', label));

        return row;
    }

    function createGroup(item, sidebar) {
        var group = el('div', 'shvya-nav-group');
        var label = item.sidebar_label || item.label || '';
        group.dataset.navLabel = label;

        if (item.is_active) group.classList.add('is-open');

        var button = el('button', 'shvya-nav-row');
        button.type = 'button';
        button.setAttribute('aria-expanded', item.is_active ? 'true' : 'false');
        button.setAttribute('title', label);
        button.dataset.tooltip = label;
        if (item.is_active) button.classList.add('is-active');
        button.appendChild(icon(item.icon, 'shvya-nav-icon'));
        button.appendChild(el('span', 'shvya-nav-copy', label));
        button.appendChild(icon('ti-chevron-down', 'shvya-nav-chevron'));

        var grid = el('div', 'shvya-nav-children-grid');
        var children = el('div', 'shvya-nav-children');

        (item.children || []).forEach(function (child) {
            var childRow = createRow(child, { child: true });
            children.appendChild(childRow);
        });

        grid.appendChild(children);
        group.appendChild(button);
        group.appendChild(grid);

        button.addEventListener('click', function () {
            if (sidebar.classList.contains('sidebar-collapsed')) {
                setCollapsed(sidebar, false);
                window.setTimeout(function () {
                    group.classList.add('is-open');
                    button.setAttribute('aria-expanded', 'true');
                }, 160);
                return;
            }

            var open = !group.classList.contains('is-open');
            group.classList.toggle('is-open', open);
            button.setAttribute('aria-expanded', open ? 'true' : 'false');
        });

        return group;
    }

    function createSection(section, sidebar) {
        var wrapper = el('section', 'shvya-nav-section');
        var title = el('div', 'shvya-section-title', section.label || '');
        var stack = el('div', 'shvya-nav-stack');
        wrapper.appendChild(title);
        wrapper.appendChild(stack);

        (section.items || []).forEach(function (item) {
            var node = item.children && item.children.length
                ? createGroup(item, sidebar)
                : createRow(item);
            stack.appendChild(node);
        });

        return wrapper;
    }

    function flattenNavigation(sections, utilities) {
        var results = [];

        (sections || []).forEach(function (section) {
            (section.items || []).forEach(function (item) {
                var label = item.sidebar_label || item.label;
                var parentHref = item.href || ((item.children || [])[0] || {}).href;

                if (label && parentHref) {
                    results.push({
                        id: 'nav:' + section.key + ':' + label,
                        title: label,
                        meta: section.label || 'Workspace',
                        icon: item.icon,
                        href: parentHref,
                        keywords: (item.search_keywords || []).join(' '),
                        type: 'navigation'
                    });
                }

                (item.children || []).forEach(function (child) {
                    if (!child.href) return;
                    results.push({
                        id: 'nav:' + label + ':' + child.label,
                        title: child.label,
                        meta: label,
                        icon: child.icon,
                        href: child.href,
                        keywords: [label].concat(child.search_keywords || []).join(' '),
                        type: 'navigation',
                        parent: label
                    });
                });
            });
        });

        (utilities || []).forEach(function (item) {
            if (!item.href) return;
            results.push({
                id: 'utility:' + item.label,
                title: item.sidebar_label || item.label,
                meta: 'Account',
                icon: item.icon,
                href: item.href,
                keywords: (item.search_keywords || []).join(' '),
                type: 'navigation'
            });
        });

        return results;
    }

    function getRecentIds() {
        try {
            var parsed = JSON.parse(safeStorageGet(RECENT_STORAGE_KEY) || '[]');
            return Array.isArray(parsed) ? parsed.slice(0, MAX_RECENTS) : [];
        } catch (error) {
            return [];
        }
    }

    function rememberResult(result) {
        if (!result || !result.id || result.type !== 'navigation') return;
        var ids = getRecentIds().filter(function (id) { return id !== result.id; });
        ids.unshift(result.id);
        safeStorageSet(RECENT_STORAGE_KEY, JSON.stringify(ids.slice(0, MAX_RECENTS)));
    }

    function clickExisting(selector) {
        var node = document.querySelector(selector);
        if (!node) return false;
        node.click();
        return true;
    }

    function createCommandPalette(sections, utilities) {
        var navigation = flattenNavigation(sections, utilities);
        var crmEntry = navigation.find(function (item) { return item.title === 'CRM'; });
        var whatsappChats = navigation.find(function (item) {
            return item.title === 'Chats' && item.parent === 'WhatsApp';
        });

        var quickActions = [
            {
                id: 'action:new-lead',
                title: 'Create new lead',
                meta: 'Quick action',
                icon: 'ti-user-plus',
                keywords: 'new create add lead customer',
                type: 'command',
                command: 'new-lead'
            },
            {
                id: 'action:import',
                title: 'Import leads',
                meta: 'Quick action',
                icon: 'ti-upload',
                keywords: 'upload csv import leads',
                type: 'command',
                command: 'import'
            },
            {
                id: 'action:reminders',
                title: 'Open reminders',
                meta: 'Quick action',
                icon: 'ti-bell',
                keywords: 'tasks reminder follow up pending',
                type: 'command',
                command: 'reminders'
            }
        ];

        var layer = el('div', 'shvya-command-layer');
        layer.setAttribute('aria-hidden', 'true');
        var panel = el('div', 'shvya-command-panel');
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
        panel.setAttribute('aria-label', 'Search or jump to');

        var inputWrap = el('div', 'shvya-command-input-wrap');
        inputWrap.appendChild(icon('ti-search'));
        var input = el('input', 'shvya-command-input');
        input.type = 'search';
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.placeholder = 'Search pages, actions, leads...';
        input.setAttribute('aria-label', 'Search SHVYA');
        inputWrap.appendChild(input);
        inputWrap.appendChild(el('span', 'shvya-command-esc', 'ESC'));

        var resultsNode = el('div', 'shvya-command-results');
        resultsNode.setAttribute('role', 'listbox');

        var footer = el('div', 'shvya-command-footer');
        footer.appendChild(el('span', 'shvya-command-key', '↑↓ Navigate'));
        footer.appendChild(el('span', 'shvya-command-key', '↵ Open'));
        footer.appendChild(el('span', 'shvya-command-key', 'Esc Close'));

        panel.appendChild(inputWrap);
        panel.appendChild(resultsNode);
        panel.appendChild(footer);
        layer.appendChild(panel);
        document.body.appendChild(layer);

        var currentResults = [];
        var selectedIndex = 0;

        function close() {
            layer.classList.remove('is-open');
            layer.setAttribute('aria-hidden', 'true');
            document.body.style.removeProperty('overflow');
            window.setTimeout(function () {
                input.value = '';
                selectedIndex = 0;
                render('');
            }, 160);
        }

        function open() {
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

        function execute(result) {
            if (!result) return;

            if (result.type === 'navigation' && result.href) {
                rememberResult(result);
                window.location.assign(result.href);
                return;
            }

            if (result.type === 'command') {
                close();
                window.setTimeout(function () {
                    if (result.command === 'new-lead') {
                        clickExisting('button[hx-get*="leads/create"], a[hx-get*="leads/create"]');
                    } else if (result.command === 'import') {
                        clickExisting('button[hx-get*="leads/import"], a[hx-get*="leads/import"]');
                    } else if (result.command === 'reminders') {
                        clickExisting('a[hx-get*="reminders"], button[hx-get*="reminders"]');
                    }
                }, 30);
            }
        }

        function matches(result, query) {
            var haystack = normalized([
                result.title,
                result.meta,
                result.keywords
            ].join(' '));
            return haystack.indexOf(query) !== -1;
        }

        function renderResult(result, index) {
            var button = el('button', 'shvya-command-result' + (index === selectedIndex ? ' is-selected' : ''));
            button.type = 'button';
            button.setAttribute('role', 'option');
            button.setAttribute('aria-selected', index === selectedIndex ? 'true' : 'false');
            button.appendChild(icon(result.icon || 'ti-arrow-right', 'shvya-command-result-icon'));

            var copy = el('span', 'shvya-command-result-copy');
            copy.appendChild(el('span', 'shvya-command-result-title', result.title));
            copy.appendChild(el('span', 'shvya-command-result-meta', result.meta || ''));
            button.appendChild(copy);
            button.appendChild(icon('ti-arrow-up-right', 'shvya-command-result-arrow'));

            button.addEventListener('mouseenter', function () {
                selectedIndex = index;
                refreshSelection();
            });
            button.addEventListener('click', function () {
                execute(result);
            });

            return button;
        }

        function refreshSelection() {
            var nodes = resultsNode.querySelectorAll('.shvya-command-result');
            nodes.forEach(function (node, index) {
                var selected = index === selectedIndex;
                node.classList.toggle('is-selected', selected);
                node.setAttribute('aria-selected', selected ? 'true' : 'false');
            });
            if (nodes[selectedIndex]) {
                nodes[selectedIndex].scrollIntoView({ block: 'nearest' });
            }
        }

        function render(queryValue) {
            var query = normalized(queryValue);
            resultsNode.textContent = '';
            currentResults = [];

            if (!query) {
                var recentIds = getRecentIds();
                var recents = recentIds
                    .map(function (id) {
                        return navigation.find(function (item) { return item.id === id; });
                    })
                    .filter(Boolean);

                if (recents.length) {
                    resultsNode.appendChild(el('div', 'shvya-command-heading', 'Recent'));
                    currentResults = currentResults.concat(recents);
                }

                resultsNode.appendChild(el('div', 'shvya-command-heading', 'Quick actions'));
                currentResults = currentResults.concat(quickActions);

                var suggested = navigation.slice(0, 6);
                if (suggested.length) {
                    resultsNode.appendChild(el('div', 'shvya-command-heading', 'Jump to'));
                    currentResults = currentResults.concat(suggested);
                }
            } else {
                currentResults = quickActions.concat(navigation).filter(function (result) {
                    return matches(result, query);
                });

                if (crmEntry && query.length >= 2) {
                    currentResults.push({
                        id: 'dynamic:crm:' + query,
                        title: 'Search CRM for “' + queryValue.trim() + '”',
                        meta: 'Leads · name, phone or email',
                        icon: 'ti-users',
                        href: appendQuery(crmEntry.href, 'search', queryValue.trim()),
                        keywords: query,
                        type: 'navigation'
                    });
                }

                if (whatsappChats && query.length >= 2) {
                    currentResults.push({
                        id: 'dynamic:wa:' + query,
                        title: 'Search WhatsApp chats for “' + queryValue.trim() + '”',
                        meta: 'WhatsApp conversations',
                        icon: 'ti-brand-whatsapp',
                        href: appendQuery(whatsappChats.href, 'q', queryValue.trim()),
                        keywords: query,
                        type: 'navigation'
                    });
                }
            }

            // De-duplicate while preserving order.
            var seen = new Set();
            currentResults = currentResults.filter(function (result) {
                var key = result.id || result.title;
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
            }).slice(0, 12);

            selectedIndex = Math.min(selectedIndex, Math.max(currentResults.length - 1, 0));

            if (!currentResults.length) {
                resultsNode.appendChild(el(
                    'div',
                    'shvya-command-empty',
                    'No direct match. Try a page name, action, lead name, phone number or email.'
                ));
                return;
            }

            currentResults.forEach(function (result, index) {
                resultsNode.appendChild(renderResult(result, index));
            });
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
            } else if (event.key === 'Escape') {
                event.preventDefault();
                close();
            }
        });

        layer.addEventListener('mousedown', function (event) {
            if (event.target === layer) close();
        });

        document.addEventListener('keydown', function (event) {
            var modifier = event.metaKey || event.ctrlKey;
            if (modifier && event.key.toLowerCase() === 'k') {
                event.preventDefault();
                if (layer.classList.contains('is-open')) close();
                else open();
            } else if (event.key === 'Escape' && layer.classList.contains('is-open')) {
                close();
            }
        });

        render('');

        return { open: open, close: close };
    }

    function enhanceSidebar() {
        var sidebar = document.getElementById('app-sidebar');
        if (!sidebar) {
            document.documentElement.classList.remove('shvya-shell-enhancing');
            return;
        }

        var sections = parseJsonScript('shvya-sidebar-sections', []);
        var utilities = parseJsonScript('shvya-sidebar-utilities', []);
        if (!sections.length) {
            document.documentElement.classList.remove('shvya-shell-enhancing');
            return;
        }

        var logoUrl = getMeta('shvya-logo-url', '');
        var profileUrl = getMeta('shvya-profile-url', '#');
        var userName = getMeta('shvya-user-name', 'Account');
        var shortcut = /Mac|iPhone|iPad|iPod/.test(navigator.platform) ? '⌘K' : 'Ctrl K';

        sidebar.textContent = '';
        sidebar.className = 'flex flex-col shrink-0';

        var shell = el('div', 'shvya-sidebar-shell');

        var brand = el('div', 'shvya-sidebar-brand');
        if (logoUrl) {
            var logo = el('img', 'shvya-brand-mark');
            logo.src = logoUrl;
            logo.alt = '';
            logo.width = 34;
            logo.height = 34;
            brand.appendChild(logo);
        }
        brand.appendChild(el('span', 'shvya-brand-name', 'SHVYA AI'));
        var toggle = el('button', 'shvya-sidebar-toggle');
        toggle.type = 'button';
        toggle.dataset.shvyaSidebarToggle = '1';
        toggle.appendChild(icon('ti-layout-sidebar-left-collapse'));
        brand.appendChild(toggle);
        shell.appendChild(brand);

        var search = el('button', 'shvya-search-trigger');
        search.type = 'button';
        search.setAttribute('aria-label', 'Search or jump to');
        search.appendChild(icon('ti-search'));
        search.appendChild(el('span', 'shvya-search-copy', 'Search or jump to...'));
        search.appendChild(el('span', 'shvya-shortcut', shortcut));
        shell.appendChild(search);

        var scroll = el('div', 'shvya-sidebar-scroll');
        sections.forEach(function (section) {
            scroll.appendChild(createSection(section, sidebar));
        });
        shell.appendChild(scroll);

        var footer = el('div', 'shvya-sidebar-footer');
        var utilityStack = el('div', 'shvya-utility-stack');
        utilities.forEach(function (item) {
            utilityStack.appendChild(createRow(item));
        });
        footer.appendChild(utilityStack);

        var profile = el('a', 'shvya-profile-card');
        profile.href = profileUrl || '#';
        profile.title = 'Profile';
        profile.appendChild(el('span', 'shvya-profile-avatar', initials(userName)));
        profile.appendChild(el('span', 'shvya-profile-name', userName || 'Account'));
        profile.appendChild(icon('ti-dots', 'shvya-profile-more'));
        footer.appendChild(profile);
        shell.appendChild(footer);

        sidebar.appendChild(shell);

        var collapsed = safeStorageGet(SIDEBAR_STORAGE_KEY) === '1';
        setCollapsed(sidebar, collapsed);

        toggle.addEventListener('click', function () {
            setCollapsed(sidebar, !sidebar.classList.contains('sidebar-collapsed'));
        });

        var palette = createCommandPalette(sections, utilities);
        search.addEventListener('click', palette.open);

        sidebar.classList.add('shvya-premium-sidebar-ready');
        document.documentElement.classList.remove('shvya-shell-enhancing');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', enhanceSidebar, { once: true });
    } else {
        enhanceSidebar();
    }
})();
