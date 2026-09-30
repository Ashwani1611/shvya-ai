/* Connection snapshots are read-only: polling must never submit a new login. */
(() => {
  'use strict';
  const root = document.querySelector('[data-ig-connect-state]');
  if (!root) return;
  // Meta can append #_ to its return URL. It is not a connection result.
  if (location.hash === '#_' || location.hash === '#_=_') {
    history.replaceState(history.state, '', location.pathname + location.search);
  }
  const initialState = root.dataset.igConnectState;
  if (!['authorizing', 'authorized_with_warning'].includes(initialState)) return;
  const poll = document.getElementById('ig-setup-poll');
  const error = document.getElementById('ig-setup-error');
  let started = Date.now();
  let timer, controller, failures = 0, stopped = false, lifecycle = 0;

  function showRetry(message) {
    poll.textContent = message;
    const pending = document.getElementById('ig-pending-action');
    const connect = document.getElementById('ig-start-connect');
    if (pending) pending.hidden = true;
    if (connect) connect.hidden = false;
    root.querySelectorAll('.ig-retry-form button').forEach(button => { button.disabled = false; });
    if (initialState === 'authorizing') {
      document.getElementById('ig-setup-state').textContent = 'Connection is taking longer than expected.';
      document.getElementById('ig-setup-description').textContent = 'We have not been able to confirm the result. Refresh this page for the latest status before starting another sign-in.';
      document.getElementById('ig-status-label').textContent = 'Check connection';
      const badge = document.getElementById('ig-status-badge');
      badge.classList.remove('ig-status-pending');
      badge.classList.add('ig-status-warning');
    }
  }

  async function check() {
    if (stopped) return;
    if (Date.now() - started > 660000) {
      showRetry(initialState === 'authorizing'
        ? 'Connection has not been confirmed. Refresh this page to check the latest status, or try connecting again.'
        : 'Inbox setup has not completed yet. Review the error above and use Retry inbox setup.');
      return;
    }
    if (document.hidden) { timer = setTimeout(check, 5000); return; }
    const epoch = lifecycle;
    const currentController = new AbortController();
    controller = currentController;
    const timeout = setTimeout(() => currentController.abort(), 12000);
    try {
      const response = await fetch(root.dataset.statusUrl, {
        headers: {Accept: 'application/json'}, credentials: 'same-origin',
        cache: 'no-store', signal: currentController.signal,
      });
      if (stopped || epoch !== lifecycle) return;
      if (response.redirected || response.status === 401 || response.status === 403) {
        showRetry('Your SHVYA session needs attention. Refresh this page and sign in again to check your connection.');
        document.getElementById('ig-setup-state').textContent = 'Sign in to check your connection.';
        return;
      }
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Status unavailable');
      const data = await response.json();
      if (typeof data.instagram_oauth_pending !== 'boolean' || typeof data.instagram_connection_state !== 'string') throw new Error('Invalid snapshot');
      if (stopped || epoch !== lifecycle) return;
      failures = 0;
      if (data.instagram_connection_state !== initialState || (initialState === 'authorizing' && !data.instagram_oauth_pending)) {
        location.reload();
        return;
      }
      const message = data.instagram_connection_error || '';
      error.textContent = message;
      error.hidden = !message;
      poll.textContent = data.instagram_oauth_pending
        ? 'Checking your connection automatically. You do not need to sign in again.'
        : 'Account authorized. Checking for inbox setup updates.';
    } catch (_) {
      if (stopped || epoch !== lifecycle) return;
      failures += 1;
      poll.textContent = 'Connection updates are temporarily unavailable. We’ll keep checking; your sign-in has not been restarted.';
    } finally {
      clearTimeout(timeout);
    }
    if (!stopped) timer = setTimeout(check, Math.min(30000, 3000 * 2 ** Math.min(failures, 3)));
  }
  timer = setTimeout(check, 1500);
  addEventListener('pagehide', () => {
    stopped = true;
    lifecycle += 1;
    clearTimeout(timer);
    if (controller) controller.abort();
  });
  addEventListener('pageshow', event => {
    if (!event.persisted) return;
    stopped = false;
    started = Date.now();
    failures = 0;
    clearTimeout(timer);
    timer = setTimeout(check, 0);
  });
})();
