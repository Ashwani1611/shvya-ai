(() => {
  'use strict';
  const root = document.querySelector('[data-booking-status-url]');
  if (!root) return;
  const calendar = document.querySelector('[data-google-event-link]');
  const meet = document.querySelector('[data-meeting-link]');
  const message = document.querySelector('[data-google-sync-message]');
  let attempts = 0;
  function safeLink(value) {
    try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; }
    catch { return ''; }
  }
  async function refresh() {
    if (document.hidden) { setTimeout(refresh, 5000); return; }
    attempts++;
    try {
      const response = await fetch(root.dataset.bookingStatusUrl, {cache:'no-store', referrerPolicy:'no-referrer'});
      if (!response.ok) throw new Error('Unable to check sync.');
      const data = await response.json();
      if (data.status === 'cancelled') { location.reload(); return; }
      const eventUrl = safeLink(data.event_url), meetingUrl = safeLink(data.meeting_link);
      if (calendar && eventUrl) { calendar.href = eventUrl; calendar.hidden = false; }
      if (meet && meetingUrl) {
        meet.href = meetingUrl; meet.hidden = false;
        meet.querySelector('[data-meeting-link-text]').textContent = meetingUrl;
      }
      if (data.sync_status === 'synced') { if (message) message.hidden = true; return; }
      if (['failed', 'not_connected'].includes(data.sync_status)) {
        if (message) message.textContent = 'Your booking is confirmed. The team will share the calendar link when it is ready.';
        return;
      }
    } catch { /* Keep the confirmed booking visible during transient failures. */ }
    if (attempts < 120) setTimeout(refresh, 3000);
    else if (message) message.textContent = 'Your booking is confirmed. Refresh this page to check for your calendar link.';
  }
  refresh();
})();
