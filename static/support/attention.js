(function () {
    'use strict';

    function start() {
        var node = document.getElementById('shvya-support-attention');
        var sidebar = document.getElementById('app-sidebar');
        if (!node || !sidebar || sidebar.dataset.supportAttentionMounted) return;
        var config;
        var endpoint;
        var portal;
        try {
            config = JSON.parse(node.textContent);
            endpoint = new URL(config.endpoint, window.location.origin);
            portal = new URL(config.portal, window.location.origin);
            if (endpoint.origin !== window.location.origin || portal.origin !== window.location.origin) return;
            if (!Number.isSafeInteger(config.count) || config.count < 0) return;
        } catch (error) { return; }
        sidebar.dataset.supportAttentionMounted = '1';

        var count = config.count;
        var timer = null;
        var controller = null;
        var suspended = false;
        var stopped = false;
        var failures = 0;
        var interval = 15000;
        var live = document.createElement('span');
        live.className = 'support-attention-sr';
        live.setAttribute('role', 'status');
        live.setAttribute('aria-live', 'polite');
        live.setAttribute('aria-atomic', 'true');
        document.body.appendChild(live);

        function render() {
            sidebar.querySelectorAll('a[href]').forEach(function (row) {
                var target;
                try { target = new URL(row.href, window.location.origin); }
                catch (error) { return; }
                if (target.origin !== portal.origin || target.pathname !== portal.pathname) return;
                if (!row.dataset.supportOriginalLabel) {
                    row.dataset.supportOriginalLabel = row.getAttribute('aria-label') || row.title || 'Help & Support';
                }
                var active = count > 0;
                var label = row.dataset.supportOriginalLabel;
                var description = active ? label + ' — ' + count + ' ticket' + (count === 1 ? '' : 's') +
                    ' need' + (count === 1 ? 's' : '') + ' your response. Reply or close the ticket.' : label;
                row.classList.toggle('support-needs-response', active);
                row.setAttribute('aria-label', description);
                row.title = description;
                row.dataset.tooltip = description;
                var badge = row.querySelector('.support-attention-badge');
                if (!badge && active) {
                    badge = document.createElement('span');
                    badge.className = 'support-attention-badge';
                    badge.setAttribute('aria-hidden', 'true');
                    row.appendChild(badge);
                }
                if (badge) {
                    badge.hidden = !active;
                    var value = count > 99 ? '99+' : String(count);
                    if (badge.textContent !== value) badge.textContent = value;
                }
            });
        }

        function setCount(value) {
            if (!Number.isSafeInteger(value) || value < 0) throw new Error('Invalid support attention response');
            if (value !== count) {
                live.textContent = value ? 'Help & Support: ' + value + ' ticket' + (value === 1 ? '' : 's') +
                    ' awaiting your response.' : 'Help & Support: no tickets awaiting your response.';
            }
            count = value;
            render();
        }

        function schedule(delay) {
            window.clearTimeout(timer);
            if (!stopped && !suspended && !document.hidden && navigator.onLine !== false) {
                timer = window.setTimeout(poll, delay);
            }
        }

        async function poll() {
            if (controller || stopped || suspended || document.hidden || navigator.onLine === false) return;
            var requestController = new AbortController();
            controller = requestController;
            var timeout = window.setTimeout(function () { requestController.abort(); }, 10000);
            try {
                var response = await fetch(endpoint.href, {
                    credentials: 'same-origin', cache: 'no-store', redirect: 'follow',
                    headers: {Accept: 'application/json'}, signal: requestController.signal
                });
                if (suspended) return;
                if (response.redirected || response.status === 401 || response.status === 403) {
                    stopped = true;
                    setCount(0);
                    return;
                }
                if (!response.ok || !(response.headers.get('Content-Type') || '').includes('application/json')) {
                    throw new Error('Support attention unavailable');
                }
                var payload = await response.json();
                if (!suspended) setCount(payload.count);
                failures = 0;
            } catch (error) {
                // A timeout, offline state or server error is NOT acknowledgement.
                // Preserve the last verified indicator and retry with bounded backoff.
                if (!suspended) failures = Math.min(failures + 1, 3);
            } finally {
                window.clearTimeout(timeout);
                controller = null;
                schedule(interval * Math.pow(2, failures));
            }
        }

        function refresh() {
            if (!controller) schedule(0);
        }

        // The premium/mobile shell may reconstruct navigation. Reapply presentation
        // without adding duplicate badges or coupling to its label/DOM internals.
        new MutationObserver(render).observe(sidebar, {childList: true, subtree: true});
        render();
        schedule(interval);
        window.addEventListener('focus', refresh);
        window.addEventListener('online', refresh);
        window.addEventListener('pageshow', function () { suspended = false; refresh(); });
        window.addEventListener('pagehide', function () {
            suspended = true;
            window.clearTimeout(timer);
            if (controller) controller.abort();
        });
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) window.clearTimeout(timer);
            else refresh();
        });
        document.addEventListener('htmx:afterRequest', function (event) {
            if (!event.detail || !event.detail.failed) refresh();
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
