(function () {
  'use strict';

  if (window.__shvyaReminderNotificationsReady) return;
  window.__shvyaReminderNotificationsReady = true;

  var config = document.getElementById('shvya-reminder-notification-config');
  if (!config) return;

  var feedUrl = config.dataset.feedUrl || '';
  var modalUrl = config.dataset.modalUrl || '';
  var pollMs = Number(config.dataset.pollMs || 15000);
  var root = null;
  var active = Object.create(null);
  var pollTimer = null;
  var loading = false;

  function getCookie(name) {
    var prefix = name + '=';
    var cookies = String(document.cookie || '').split(';');
    for (var i = 0; i < cookies.length; i += 1) {
      var value = cookies[i].trim();
      if (value.indexOf(prefix) === 0) {
        return decodeURIComponent(value.slice(prefix.length));
      }
    }
    return '';
  }

  function ensureRoot() {
    if (root) return root;
    root = document.getElementById('shvya-reminder-notification-root');
    if (!root) {
      root = document.createElement('div');
      root.id = 'shvya-reminder-notification-root';
      root.setAttribute('aria-live', 'polite');
      root.setAttribute('aria-label', 'Reminder notifications');
      document.body.appendChild(root);
    }
    return root;
  }

  function formatDue(value) {
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    try {
      return new Intl.DateTimeFormat(undefined, {
        day: 'numeric',
        month: 'short',
        hour: 'numeric',
        minute: '2-digit'
      }).format(date);
    } catch (error) {
      return date.toLocaleString();
    }
  }

  function updatePendingBadge(count) {
    var badge = document.getElementById('global-reminder-count');
    if (!badge) return;
    var value = Number(count || 0);
    badge.textContent = String(value);
    badge.classList.toggle('hidden', value < 1);
  }

  function openReminderModal() {
    var modalRoot = document.getElementById('modal-root');
    if (!modalRoot || !modalUrl) return;

    if (window.htmx && typeof window.htmx.ajax === 'function') {
      window.htmx.ajax('GET', modalUrl, {
        target: '#modal-root',
        swap: 'innerHTML'
      });
      return;
    }

    fetch(modalUrl, {
      credentials: 'same-origin',
      headers: {'X-Requested-With': 'XMLHttpRequest'}
    })
      .then(function (response) {
        if (!response.ok) throw new Error('Unable to load reminders');
        return response.text();
      })
      .then(function (html) {
        modalRoot.innerHTML = html;
      })
      .catch(function () {
        if (window.shvyaToast) {
          window.shvyaToast(
            'The reminder list could not be opened. Please try again.',
            'error',
            {force: true}
          );
        }
      });
  }

  function removeCard(id) {
    var card = active[id];
    if (!card) return;
    card.classList.add('is-leaving');
    window.setTimeout(function () {
      if (card.parentNode) card.parentNode.removeChild(card);
    }, 220);
    delete active[id];
  }

  function acknowledge(notification, card) {
    if (!notification || !notification.ack_url || card.dataset.busy === '1') return;
    card.dataset.busy = '1';
    card.setAttribute('aria-busy', 'true');

    fetch(notification.ack_url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'X-CSRFToken': getCookie('csrftoken'),
        'X-Requested-With': 'XMLHttpRequest'
      }
    })
      .then(function (response) {
        if (!response.ok) throw new Error('Unable to acknowledge reminder');
        removeCard(notification.id);
        openReminderModal();
        window.setTimeout(refresh, 350);
      })
      .catch(function () {
        card.dataset.busy = '0';
        card.removeAttribute('aria-busy');
        if (window.shvyaToast) {
          window.shvyaToast(
            'The reminder notification could not be acknowledged. Please try again.',
            'error',
            {force: true}
          );
        }
      });
  }

  function buildCard(notification) {
    var card = document.createElement('button');
    card.type = 'button';
    card.className = 'shvya-reminder-alert';
    card.dataset.reminderId = notification.id;
    card.setAttribute(
      'aria-label',
      'Open reminder for ' + (notification.lead_name || 'lead')
    );

    var iconWrap = document.createElement('span');
    iconWrap.className = 'shvya-reminder-alert__icon';
    var icon = document.createElement('i');
    icon.className = 'ti ti-bell-ringing';
    iconWrap.appendChild(icon);

    var body = document.createElement('span');
    body.className = 'shvya-reminder-alert__body';

    var eyebrow = document.createElement('span');
    eyebrow.className = 'shvya-reminder-alert__eyebrow';
    eyebrow.textContent = notification.is_overdue ? 'Reminder overdue' : 'Reminder due';

    var title = document.createElement('span');
    title.className = 'shvya-reminder-alert__title';
    title.textContent = notification.title || 'Follow up';

    var meta = document.createElement('span');
    meta.className = 'shvya-reminder-alert__meta';
    var leadName = notification.lead_name || 'Lead';
    var dueLabel = formatDue(notification.due_at);
    meta.textContent = dueLabel ? leadName + ' · ' + dueLabel : leadName;

    var description = document.createElement('span');
    description.className = 'shvya-reminder-alert__description';
    description.textContent = notification.description || 'Open the reminder to review this follow-up.';

    var action = document.createElement('span');
    action.className = 'shvya-reminder-alert__action';
    action.innerHTML = '<span>View reminder</span><i class="ti ti-chevron-right"></i>';

    body.appendChild(eyebrow);
    body.appendChild(title);
    body.appendChild(meta);
    body.appendChild(description);
    body.appendChild(action);

    card.appendChild(iconWrap);
    card.appendChild(body);
    card.addEventListener('click', function () {
      acknowledge(notification, card);
    });

    return card;
  }

  function syncNotifications(notifications) {
    var container = ensureRoot();
    var seen = Object.create(null);

    (notifications || []).forEach(function (notification) {
      if (!notification || !notification.id) return;
      seen[notification.id] = true;
      if (active[notification.id]) return;

      var card = buildCard(notification);
      active[notification.id] = card;
      container.appendChild(card);
      requestAnimationFrame(function () {
        card.classList.add('is-visible');
      });
    });

    Object.keys(active).forEach(function (id) {
      if (!seen[id]) removeCard(id);
    });

    container.classList.toggle(
      'has-reminders',
      Object.keys(active).length > 0
    );
  }

  function refresh() {
    if (!feedUrl || loading) return;
    loading = true;

    fetch(feedUrl, {
      credentials: 'same-origin',
      headers: {'X-Requested-With': 'XMLHttpRequest'},
      cache: 'no-store'
    })
      .then(function (response) {
        if (!response.ok) throw new Error('Reminder notification feed failed');
        return response.json();
      })
      .then(function (payload) {
        syncNotifications(payload.notifications || []);
        updatePendingBadge(payload.pending_count);
      })
      .catch(function () {
        // Fail quietly. The normal Reminders modal remains the durable fallback.
      })
      .finally(function () {
        loading = false;
      });
  }

  function schedule() {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = window.setInterval(function () {
      if (!document.hidden) refresh();
    }, Math.max(5000, pollMs));
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) refresh();
  });

  document.body.addEventListener('htmx:afterRequest', function (event) {
    var path = '';
    try {
      path = new URL(
        event.detail && event.detail.xhr ? event.detail.xhr.responseURL : '',
        window.location.href
      ).pathname;
    } catch (error) {
      path = '';
    }
    if (path.indexOf('/dashboard/reminders/') !== -1 ||
        path.indexOf('/dashboard/leads/') !== -1) {
      window.setTimeout(refresh, 150);
    }
  });

  refresh();
  schedule();
})();
