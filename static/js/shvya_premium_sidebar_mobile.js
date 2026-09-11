(function () {
    'use strict';

    var MOBILE_QUERY = '(max-width: 900px)';

    function createIconButton() {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'shvya-mobile-sidebar-trigger';
        button.setAttribute('aria-label', 'Open navigation');
        button.setAttribute('aria-controls', 'app-sidebar');
        button.setAttribute('aria-expanded', 'false');

        var icon = document.createElement('i');
        icon.className = 'ti ti-menu-2';
        icon.setAttribute('aria-hidden', 'true');
        button.appendChild(icon);

        return button;
    }

    function createBackdrop() {
        var backdrop = document.createElement('button');
        backdrop.type = 'button';
        backdrop.className = 'shvya-mobile-sidebar-backdrop';
        backdrop.setAttribute('aria-label', 'Close navigation');
        backdrop.setAttribute('tabindex', '-1');
        return backdrop;
    }

    function initMobileSidebar() {
        var sidebar = document.getElementById('app-sidebar');
        if (!sidebar || document.querySelector('.shvya-mobile-sidebar-trigger')) return;

        var media = window.matchMedia(MOBILE_QUERY);
        var trigger = createIconButton();
        var backdrop = createBackdrop();
        document.body.appendChild(backdrop);
        document.body.appendChild(trigger);

        function isOpen() {
            return document.documentElement.classList.contains('shvya-mobile-sidebar-open');
        }

        function syncA11y() {
            var open = media.matches && isOpen();
            trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
            trigger.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
            sidebar.setAttribute('aria-hidden', media.matches && !open ? 'true' : 'false');
        }

        function open() {
            if (!media.matches) return;
            document.documentElement.classList.add('shvya-mobile-sidebar-open');
            syncA11y();
            window.requestAnimationFrame(function () {
                var firstTarget = sidebar.querySelector('.shvya-search-trigger, a, button');
                if (firstTarget) firstTarget.focus({ preventScroll: true });
            });
        }

        function close(options) {
            options = options || {};
            document.documentElement.classList.remove('shvya-mobile-sidebar-open');
            syncA11y();
            if (options.restoreFocus && media.matches) {
                trigger.focus({ preventScroll: true });
            }
        }

        trigger.addEventListener('click', function () {
            if (isOpen()) close({ restoreFocus: true });
            else open();
        });

        backdrop.addEventListener('click', function () {
            close({ restoreFocus: true });
        });

        sidebar.addEventListener('click', function (event) {
            if (!media.matches) return;
            var link = event.target.closest('a[href]');
            if (link) close();
        });

        /* On mobile, the premium sidebar's top-right collapse button becomes
           the drawer close control. Capture the event before the desktop
           compact-mode handler so opening/closing never changes the user's
           saved desktop preference. */
        sidebar.addEventListener('click', function (event) {
            if (!media.matches) return;
            var toggle = event.target.closest('[data-shvya-sidebar-toggle]');
            if (!toggle) return;

            event.preventDefault();
            event.stopImmediatePropagation();
            close({ restoreFocus: true });
        }, true);

        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && media.matches && isOpen()) {
                event.preventDefault();
                close({ restoreFocus: true });
            }
        });

        function handleViewportChange() {
            if (!media.matches) {
                document.documentElement.classList.remove('shvya-mobile-sidebar-open');
            }
            syncA11y();
        }

        if (typeof media.addEventListener === 'function') {
            media.addEventListener('change', handleViewportChange);
        } else if (typeof media.addListener === 'function') {
            media.addListener(handleViewportChange);
        }

        syncA11y();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initMobileSidebar, { once: true });
    } else {
        initMobileSidebar();
    }
})();
