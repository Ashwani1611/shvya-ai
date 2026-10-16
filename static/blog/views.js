/* Record a successful visible landing; failures never interrupt the article. */
(() => {
  'use strict';
  const counter = document.getElementById('blog-view-count');
  if (!counter || !counter.dataset.token) return;
  let sent = false;
  function record() {
    if (sent || document.visibilityState !== 'visible') return;
    sent = true;
    document.removeEventListener('visibilitychange', record);
    if (navigator.doNotTrack === '1' || navigator.globalPrivacyControl === true) return;
    fetch(counter.dataset.url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'X-CSRFToken': counter.dataset.csrf},
      body: new URLSearchParams({token: counter.dataset.token}),
    }).then(response => response.ok ? response.json() : null).then(data => {
      if (data && Number.isSafeInteger(data.views) && data.views >= 0) {
        counter.textContent = data.views.toLocaleString('en-IN');
      }
    }).catch(() => { /* Keep the server-rendered count if analytics is unavailable. */ });
  }
  document.addEventListener('visibilitychange', record);
  record();
})();
