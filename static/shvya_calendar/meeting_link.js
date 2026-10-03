(() => {
  'use strict';
  // Delegation also covers CRM cards and modals inserted by HTMX.
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-copy-meeting-link]');
    if (!button) return;
    const status = button.closest('[data-meeting-link-controls]').querySelector('[data-meeting-copy-status]');
    button.disabled = true;
    try {
      await navigator.clipboard.writeText(button.dataset.copyMeetingLink);
      status.textContent = 'Link copied.';
    } catch {
      status.textContent = 'Unable to copy. Use Join meeting, or right-click it to copy the link.';
    } finally {
      button.disabled = false;
    }
  });
})();
