/* Mobile navigation owns drawer state; desktop owns the saved compact preference. */
(function () {
    'use strict';

    var MOBILE_QUERY = '(max-width: 900px)';
    var OPEN_CLASS = 'shvya-mobile-sidebar-open';
    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function iconButton() {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'shvya-mobile-sidebar-trigger';
        button.setAttribute('aria-label', 'Open navigation');
        button.setAttribute('aria-controls', 'app-sidebar');
        button.setAttribute('aria-expanded', 'false');
        // Navigation must remain recognizable even if the icon font fails to load.
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('width', '24');
        svg.setAttribute('height', '24');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        var path = document.createElementNS(svg.namespaceURI, 'path');
        path.setAttribute('d', 'M4 6h16M4 12h16M4 18h16');
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', 'currentColor');
        path.setAttribute('stroke-width', '2');
        path.setAttribute('stroke-linecap', 'round');
        svg.appendChild(path);
        button.appendChild(svg);
        return button;
    }

    function initMobileSidebar() {
        var sidebar = document.getElementById('app-sidebar');
        if (!sidebar || sidebar.dataset.shvyaMobileMounted) return;
        sidebar.dataset.shvyaMobileMounted = '1';
        var root = document.documentElement;
        var media = window.matchMedia(MOBILE_QUERY);
        var content = sidebar.nextElementSibling;
        var header = content && content.querySelector('header');
        var trigger = iconButton();
        var backdrop = document.createElement('button');
        backdrop.type = 'button';
        backdrop.className = 'shvya-mobile-sidebar-backdrop';
        backdrop.setAttribute('aria-label', 'Close navigation');
        backdrop.setAttribute('tabindex', '-1');
        backdrop.setAttribute('aria-hidden', 'true');
        document.body.appendChild(backdrop);
        if (header) header.insertBefore(trigger, header.firstChild);
        else document.body.appendChild(trigger);

        var original = {};
        ['role', 'aria-label', 'aria-modal', 'aria-hidden', 'tabindex'].forEach(function (name) {
            original[name] = sidebar.getAttribute(name);
        });
        var originalInert = sidebar.hasAttribute('inert');
        var blocked = [];
        var toggle = sidebar.querySelector('[data-shvya-sidebar-toggle], #sidebar-toggle');
        var toggleIcon = toggle && toggle.querySelector('.ti');
        var toggleIconClass = toggleIcon && toggleIcon.className;

        function isOpen() {
            return media.matches && root.classList.contains(OPEN_CLASS);
        }

        function restoreAttribute(node, name, value) {
            if (value === null) node.removeAttribute(name);
            else node.setAttribute(name, value);
        }

        function restoreBackground() {
            blocked.forEach(function (entry) {
                entry.node.toggleAttribute('inert', entry.inert);
            });
            blocked = [];
        }

        function blockBackground() {
            // Block siblings, never an ancestor of the drawer. Keep other modules'
            // pre-existing inert state so closing navigation cannot enable a modal backdrop.
            var branch = sidebar;
            while (branch && branch.parentElement) {
                Array.prototype.forEach.call(branch.parentElement.children, function (node) {
                    if (node === branch || node === backdrop || node === trigger ||
                        /^(SCRIPT|STYLE|LINK|META)$/.test(node.tagName)) return;
                    blocked.push({ node: node, inert: node.hasAttribute('inert') });
                    node.setAttribute('inert', '');
                });
                branch = branch.parentElement;
                if (branch === document.body) break;
            }
        }

        function syncGroups() {
            sidebar.querySelectorAll('.shvya-nav-group').forEach(function (group) {
                var grid = group.querySelector('.shvya-nav-children-grid');
                if (!grid) return;
                // On desktop the existing controller remains the owner of groups.
                if (media.matches) {
                    grid.toggleAttribute('inert', !group.classList.contains('is-open'));
                } else {
                    grid.removeAttribute('inert');
                }
            });
        }

        function syncA11y() {
            var open = isOpen();
            trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
            trigger.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
            backdrop.setAttribute('aria-hidden', open ? 'false' : 'true');
            if (media.matches) {
                sidebar.setAttribute('role', 'dialog');
                sidebar.setAttribute('aria-label', 'Main navigation');
                sidebar.setAttribute('tabindex', '-1');
                sidebar.setAttribute('aria-hidden', open ? 'false' : 'true');
                sidebar.toggleAttribute('inert', !open);
                if (open) sidebar.setAttribute('aria-modal', 'true');
                else sidebar.removeAttribute('aria-modal');
                if (toggle) {
                    toggle.setAttribute('aria-label', 'Close navigation');
                    toggle.setAttribute('title', 'Close navigation');
                    toggle.removeAttribute('aria-expanded');
                    if (toggleIcon) toggleIcon.className = 'ti ti-x';
                }
            } else {
                Object.keys(original).forEach(function (name) {
                    restoreAttribute(sidebar, name, original[name]);
                });
                sidebar.toggleAttribute('inert', originalInert);
                if (toggle) {
                    var collapsed = sidebar.classList.contains('sidebar-collapsed');
                    toggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
                    toggle.setAttribute('title', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
                    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
                    if (toggleIcon) toggleIcon.className = toggleIconClass;
                }
            }
            syncGroups();
        }

        function focusable() {
            return Array.prototype.filter.call(sidebar.querySelectorAll(FOCUSABLE), function (node) {
                return node.tabIndex >= 0 && !node.closest('[inert], [hidden]') &&
                    node.getClientRects().length && window.getComputedStyle(node).visibility !== 'hidden';
            });
        }

        function focusFirst() {
            var target = focusable()[0] || sidebar;
            target.focus({ preventScroll: true });
        }

        function open() {
            if (!media.matches || isOpen()) return;
            root.classList.add(OPEN_CLASS);
            syncA11y();
            // Move focus out of the page before making the page inert.
            focusFirst();
            blockBackground();
        }

        function close(options) {
            options = options || {};
            var wasOpen = root.classList.contains(OPEN_CLASS);
            root.classList.remove(OPEN_CLASS);
            restoreBackground();
            // Move focus before marking the old focused drawer aria-hidden.
            if (wasOpen && media.matches && options.restoreFocus !== false) {
                trigger.focus({ preventScroll: true });
            }
            syncA11y();
        }

        trigger.addEventListener('click', function () {
            if (isOpen()) close();
            else open();
        });
        backdrop.addEventListener('click', function () { close(); });

        sidebar.addEventListener('click', function (event) {
            if (!media.matches || !(event.target instanceof Element)) return;
            if (event.target.closest('[data-shvya-sidebar-toggle], #sidebar-toggle')) {
                event.preventDefault();
                event.stopImmediatePropagation();
                close();
                return;
            }
            var button = event.target.closest('.shvya-nav-group > button.shvya-nav-row');
            if (button) {
                // The desktop handler expands/persists the entire rail on a group
                // click. Mobile must only expand this group, without touching storage.
                event.preventDefault();
                event.stopImmediatePropagation();
                var group = button.parentElement;
                var expanded = !group.classList.contains('is-open');
                group.classList.toggle('is-open', expanded);
                button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
                syncGroups();
                return;
            }
        }, true);

        sidebar.addEventListener('click', function (event) {
            if (!media.matches || !(event.target instanceof Element)) return;
            var link = event.target.closest('a[href]');
            if (link && !event.defaultPrevented && !event.metaKey && !event.ctrlKey &&
                !event.shiftKey && !event.altKey && link.target !== '_blank') close();
        });

        // The command center owns document-capture listeners and stops further
        // propagation. Release the drawer at window capture first, independent
        // of script registration order, without swallowing the search event.
        window.addEventListener('click', function (event) {
            if (!isOpen() || !(event.target instanceof Element)) return;
            if (event.target.closest('.shvya-search-trigger')) close();
        }, true);

        window.addEventListener('keydown', function (event) {
            if (!isOpen()) return;
            if ((event.metaKey || event.ctrlKey) && String(event.key || '').toLowerCase() === 'k') {
                close();
                return; // The existing command palette still handles the shortcut.
            }
            if (event.key === 'Escape') {
                event.preventDefault();
                event.stopImmediatePropagation();
                close();
            } else if (event.key === 'Tab') {
                var targets = focusable();
                var first = targets[0] || sidebar;
                var last = targets[targets.length - 1] || sidebar;
                if (event.shiftKey && (document.activeElement === first || document.activeElement === sidebar)) {
                    event.preventDefault();
                    last.focus({ preventScroll: true });
                } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === sidebar)) {
                    event.preventDefault();
                    first.focus({ preventScroll: true });
                }
            }
        }, true);

        document.addEventListener('focusin', function (event) {
            if (isOpen() && !sidebar.contains(event.target)) focusFirst();
        });

        function handleViewportChange() { close({ restoreFocus: false }); }
        if (typeof media.addEventListener === 'function') media.addEventListener('change', handleViewportChange);
        else if (typeof media.addListener === 'function') media.addListener(handleViewportChange);
        window.addEventListener('pagehide', function () { close({ restoreFocus: false }); });
        window.addEventListener('pageshow', handleViewportChange);
        document.addEventListener('htmx:beforeHistorySave', function () { close(); });

        // Some accounts have no premium navigation payload. In that case retain
        // the existing server-rendered (permission-filtered) navigation, not a blank drawer.
        sidebar.classList.add('shvya-mobile-sidebar-ready');
        if (header) {
            header.querySelectorAll(':scope > div:last-child > a, :scope > div:last-child > button').forEach(function (control) {
                if (!control.hasAttribute('aria-label')) {
                    var copy = control.querySelector('span');
                    var label = copy && copy.textContent.trim();
                    if (label) control.setAttribute('aria-label', label);
                }
            });
        }
        close({ restoreFocus: false });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initMobileSidebar, { once: true });
    else initMobileSidebar();
})();
