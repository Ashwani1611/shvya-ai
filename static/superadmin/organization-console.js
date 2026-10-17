(function () {
  'use strict';
  function init() {
    const root = document.getElementById('organization-console');
    if (!root) return;
    const cards = Array.from(document.querySelectorAll('.sa-content h2'));
    for (const [label, id] of [['Organization Users', 'oc-users'], ['User Settings', 'oc-users'], ['Pipelines', 'oc-pipelines'], ['Pipeline Settings', 'oc-pipelines'], ['Marketplace connections', 'oc-integrations']]) {
      const heading = cards.find(h => h.textContent.trim() === label);
      if (heading) (heading.closest('section, .bg-white.border.rounded-lg') || heading).id = id;
    }
    const summary = document.getElementById('oc-summary');
    const workspace = document.getElementById('workspace');
    if (workspace) workspace.before(summary);
    const integrations = document.getElementById('oc-integrations');
    if (integrations) document.getElementById('oc-pending').after(integrations);
    const search = document.getElementById('oc-org-search');
    const results = document.getElementById('oc-search-results');
    let timer, controller;
    search.addEventListener('input', () => {
      clearTimeout(timer);
      if (controller) controller.abort();
      results.replaceChildren();
      const query = search.value.trim();
      if (query.length < 2) return;
      timer = setTimeout(async () => {
        controller = new AbortController();
        try {
          const url = new URL(search.dataset.lookupUrl, location.origin);
          url.searchParams.set('q', query);
          const response = await fetch(url, {signal: controller.signal, headers: {'Accept': 'application/json'}});
          if (!response.ok) throw new Error('Search unavailable');
          const data = await response.json();
          if (search.value.trim() !== query) return;
          results.replaceChildren();
          for (const row of data.results) {
            const link = document.createElement('a'); link.href = row.url;
            link.textContent = row.name + (row.active ? '' : ' · Disabled'); results.append(link);
          }
          if (!data.results.length) results.textContent = 'No matching organisations.';
        } catch (error) {
          if (error.name !== 'AbortError') results.textContent = 'Press Enter to search the organisation directory.';
        }
      }, 250);
    });
    search.addEventListener('keydown' , event => {
      if (event.key !== 'Enter' || !search.value.trim()) return;
      event.preventDefault();
      const url = new URL(search.dataset.searchUrl, location.origin);
      url.searchParams.set('search', search.value.trim());
      location.assign(url);
    });
    const charts = JSON.parse(document.getElementById('oc-chart-data').textContent);
    document.querySelectorAll('[data-chart-index]').forEach(element => {
      const chart = charts[Number(element.dataset.chartIndex)];
      if (!chart.rows.some(row => row.value > 0)) {
        const empty = document.createElement('p'); empty.className = 'oc-empty'; empty.textContent = 'No recorded activity in this period.'; element.append(empty); return;
      }
      const caption = document.createElement('p'); caption.className = 'oc-hint';
      caption.textContent = `${chart.rows[0].label} – ${chart.rows[chart.rows.length - 1].label} · Total ${chart.rows.reduce((sum, row) => sum + row.value, 0)}`;
      element.after(caption);
      const max = Math.max(...chart.rows.map(row => row.value), 1);
      chart.rows.forEach(row => {
        const bar = document.createElement('div'); bar.className = 'oc-chart-bar';
        bar.style.height = `${row.value / max * 100}%`;
        bar.title = `${row.label}: ${row.value}`; bar.setAttribute('aria-label', bar.title); bar.setAttribute('role', 'img');
        element.append(bar);
      });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
