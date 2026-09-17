(() => {
  'use strict';

  const root = document.getElementById('smart-triggers');
  if (!root) return;

  /* The Workflows module predates the global premium mobile sidebar and still
     installs its own body classes/listeners. Neutralize only those legacy
     layout classes so the shared shell remains the single source of truth. */
  const releaseLegacyMobileShell = () => {
    document.body.classList.remove('st-page', 'st-nav-open');
    const legacyButton = document.getElementById('st-mobile-nav');
    const legacyBackdrop = document.getElementById('st-nav-backdrop');
    if (legacyButton) {
      legacyButton.hidden = true;
      legacyButton.setAttribute('aria-hidden', 'true');
      legacyButton.tabIndex = -1;
    }
    if (legacyBackdrop) legacyBackdrop.hidden = true;
  };

  const replacements = [
    ['Smart Triggers', 'Workflows'],
    ['How Workflows work', 'How workflows work'],
    ['Auto Follow-up sequence', 'Cadence sequence'],
    ['Auto Follow-ups', 'Cadence'],
    ['Create a rule', 'Create workflow'],
    ['Edit rule', 'Edit workflow'],
    ['View rule', 'View workflow'],
    ['Save rule', 'Save workflow'],
    ['Delete this rule?', 'Delete workflow?'],
    ['Keep rule', 'Keep workflow'],
    ['Rule name', 'Workflow name'],
    ['Total rules', 'Total workflows'],
    ['Enabled rules', 'Active workflows'],
    ['No matching rules', 'No matching workflows'],
    ['Your first automation starts here', 'Your first workflow starts here'],
    ['Create your first rule', 'Create your first workflow'],
    ['All rules', 'All workflows']
  ];

  const normalizeCopy = () => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent || /^(SCRIPT|STYLE|TEXTAREA|OPTION)$/i.test(parent.tagName)) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });

    const nodes = [];
    let node;
    while ((node = walker.nextNode())) nodes.push(node);

    nodes.forEach((textNode) => {
      let value = textNode.nodeValue;
      replacements.forEach(([from, to]) => {
        value = value.split(from).join(to);
      });
      if (value !== textNode.nodeValue) textNode.nodeValue = value;
    });
  };

  /* Keep keyboard interaction on the existing tabs: click remains the only
     path to the dashboard's view/history handlers. No parallel view state. */
  const tabs = [...root.querySelectorAll('.st-tabs [role="tab"]')];
  const enhanceAccessibility = () => {
    tabs.forEach(tab => { tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1; });
    root.querySelectorAll('.st-table-wrap').forEach(wrap => {
      wrap.tabIndex = 0;
      wrap.setAttribute('role', 'region');
      wrap.setAttribute('aria-label', wrap.closest('#st-history-panel') ? 'Workflow run history' : 'Workflow list');
    });
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', enhanceAccessibility);
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      tabs[next].click();
      tabs[next].focus();
    });
  });

  releaseLegacyMobileShell();
  normalizeCopy();
  enhanceAccessibility();

  let queued = false;
  const observer = new MutationObserver((mutations) => {
    if (queued || !mutations.some((mutation) => mutation.addedNodes.length)) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      releaseLegacyMobileShell();
      normalizeCopy();
      enhanceAccessibility();
    });
  });
  observer.observe(root, { childList: true, subtree: true });

  window.addEventListener('beforeunload', () => observer.disconnect(), { once: true });
})();
