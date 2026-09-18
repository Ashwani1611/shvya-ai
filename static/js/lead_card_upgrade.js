(() => {
  'use strict';

  if (window.shvyaLeadPickers) return;
  window.shvyaLeadPickers = true;

  // Keep this enhancement intentionally narrow. Import/edit modals keep their
  // normal selects; only the CRM pipeline switcher and lead-card stage picker
  // become the fast inline dropdown requested for daily navigation.
  const selector = '.lead-card select[name="stage"], #pipeline-select';
  const states = new WeakMap();
  let openState = null;
  let menu = null;
  let uid = 0;

  function optionLabel(option) {
    return (option && option.textContent ? option.textContent : '').trim();
  }

  function selectedOption(select) {
    return select.selectedOptions && select.selectedOptions[0]
      ? select.selectedOptions[0]
      : Array.from(select.options).find(option => option.value === select.value);
  }

  function kindFor(select) {
    return select.id === 'pipeline-select' ? 'pipeline' : 'stage';
  }

  function labelFor(kind) {
    return kind === 'pipeline' ? 'Switch pipeline' : 'Change lead stage';
  }

  function ensureMenu() {
    if (menu) return menu;

    menu = document.createElement('div');
    menu.className = 'lead-picker-popover';
    menu.hidden = true;
    menu.setAttribute('role', 'listbox');
    menu.addEventListener('keydown', onMenuKeydown);
    document.body.appendChild(menu);
    return menu;
  }

  function sync(state) {
    if (!state || !state.select.isConnected) return;

    const option = selectedOption(state.select);
    state.value.textContent = optionLabel(option) || labelFor(state.kind);
    state.trigger.disabled = state.select.disabled;
    state.trigger.setAttribute('aria-disabled', String(state.select.disabled));

    if (openState === state) {
      renderMenu(state);
      positionMenu(state);
    }
  }

  function setPending(state, pending) {
    if (!state || !state.trigger.isConnected) return;

    state.pending = pending;
    state.trigger.classList.toggle('is-loading', pending);
    state.trigger.setAttribute('aria-busy', String(pending));
    state.icon.className = pending
      ? 'ti ti-loader-2 lead-picker-chevron lead-picker-spinner'
      : 'ti ti-chevron-down lead-picker-chevron';
  }

  function flashError(state) {
    if (!state || !state.trigger.isConnected) return;

    state.trigger.classList.add('lead-picker-trigger-error');
    state.trigger.title = 'Could not update. The previous selection was restored.';

    window.clearTimeout(state.errorTimer);
    state.errorTimer = window.setTimeout(() => {
      if (!state.trigger.isConnected) return;
      state.trigger.classList.remove('lead-picker-trigger-error');
      state.trigger.removeAttribute('title');
    }, 2400);
  }

  function enhance(root = document) {
    root.querySelectorAll(selector).forEach(select => {
      if (select.dataset.applePicker) return;

      const kind = kindFor(select);
      select.dataset.applePicker = '1';
      select.hidden = true;

      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'lead-picker-trigger lead-picker-trigger--' + kind;
      trigger.setAttribute('aria-haspopup', 'listbox');
      trigger.setAttribute('aria-expanded', 'false');
      trigger.setAttribute('aria-label', labelFor(kind));

      const value = document.createElement('span');
      value.className = 'lead-picker-value';

      const icon = document.createElement('i');
      icon.className = 'ti ti-chevron-down lead-picker-chevron';
      icon.setAttribute('aria-hidden', 'true');

      trigger.append(value, icon);

      // The templates keep a native chevron for no-JS fallback. Hide only that
      // legacy icon after enhancement so the Apple trigger never shows a
      // doubled arrow.
      const legacyIcon = select.nextElementSibling;
      if (
        legacyIcon &&
        legacyIcon.classList &&
        legacyIcon.classList.contains('ti-chevron-down')
      ) {
        legacyIcon.classList.add('lead-picker-legacy-chevron');
        legacyIcon.setAttribute('aria-hidden', 'true');
      }

      select.after(trigger);

      const state = {
        select,
        trigger,
        value,
        icon,
        kind,
        pending: false,
        previousValue: null,
        errorTimer: null,
      };

      states.set(select, state);
      sync(state);

      select.addEventListener('change', () => sync(state));

      const observer = new MutationObserver(() => sync(state));
      observer.observe(select, {
        childList: true,
        subtree: true,
        attributes: true,
      });

      trigger.addEventListener('click', () => {
        if (state.pending || state.select.disabled) return;
        if (openState === state) closeMenu(true);
        else openMenu(state, 'selected');
      });

      trigger.addEventListener('keydown', event => {
        if (state.pending || state.select.disabled) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          openMenu(state, event.key === 'ArrowUp' ? 'last' : 'selected');
        } else if (event.key === 'Escape' && openState === state) {
          event.preventDefault();
          closeMenu(true);
        }
      });
    });
  }

  function availableOptions(select) {
    return Array.from(select.options).filter(option => {
      const group = option.parentElement;
      return !option.disabled && !(group && group.tagName === 'OPTGROUP' && group.disabled);
    });
  }

  function renderMenu(state) {
    const popover = ensureMenu();
    popover.replaceChildren();
    popover.dataset.kind = state.kind;
    popover.setAttribute('aria-label', labelFor(state.kind));

    availableOptions(state.select).forEach((option, index) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'lead-picker-option';
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(option.value === state.select.value));
      row.dataset.value = option.value;
      row.id = 'lead-picker-option-' + state.kind + '-' + index + '-' + uid;

      const label = document.createElement('span');
      label.className = 'lead-picker-option-label';
      label.textContent = optionLabel(option);

      const check = document.createElement('span');
      check.className = 'lead-picker-check';
      check.setAttribute('aria-hidden', 'true');
      check.textContent = option.value === state.select.value ? '✓' : '';

      row.append(label, check);
      row.addEventListener('click', () => choose(state, option.value));
      popover.appendChild(row);
    });
  }

  function openMenu(state, focusMode) {
    if (!state.select.isConnected || !state.trigger.isConnected) return;

    if (openState && openState !== state) closeMenu(false);

    uid += 1;
    openState = state;
    renderMenu(state);

    const popover = ensureMenu();
    const menuId = 'lead-picker-popover-' + uid;
    popover.id = menuId;
    state.trigger.setAttribute('aria-controls', menuId);
    state.trigger.setAttribute('aria-expanded', 'true');
    state.trigger.classList.add('is-open');

    popover.hidden = false;
    popover.style.visibility = 'hidden';
    positionMenu(state);
    popover.style.visibility = 'visible';

    window.requestAnimationFrame(() => {
      if (openState !== state) return;

      const rows = Array.from(popover.querySelectorAll('.lead-picker-option'));
      if (!rows.length) return;

      let target = rows.find(row => row.getAttribute('aria-selected') === 'true') || rows[0];
      if (focusMode === 'last') target = rows[rows.length - 1];
      target.focus({ preventScroll: true });
    });
  }

  function closeMenu(restoreFocus) {
    if (!openState) return;

    const state = openState;
    openState = null;

    if (menu) {
      menu.hidden = true;
      menu.style.visibility = '';
      menu.removeAttribute('id');
    }

    if (state.trigger.isConnected) {
      state.trigger.setAttribute('aria-expanded', 'false');
      state.trigger.removeAttribute('aria-controls');
      state.trigger.classList.remove('is-open');
      if (restoreFocus) state.trigger.focus({ preventScroll: true });
    }
  }

  function positionMenu(state) {
    if (!menu || menu.hidden || !state || !state.trigger.isConnected) return;

    const rect = state.trigger.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
    const viewportHeight = document.documentElement.clientHeight || window.innerHeight;
    const edge = 8;
    const gap = 7;

    const desired = state.kind === 'pipeline'
      ? Math.max(rect.width, 220)
      : Math.max(rect.width, 190);
    const width = Math.max(160, Math.min(desired, viewportWidth - edge * 2));

    menu.style.width = width + 'px';
    menu.style.maxHeight = Math.max(140, Math.min(320, viewportHeight - 24)) + 'px';

    let left = rect.left;
    if (left + width > viewportWidth - edge) {
      left = viewportWidth - width - edge;
    }
    left = Math.max(edge, left);

    const height = menu.offsetHeight;
    let top = rect.bottom + gap;

    if (top + height > viewportHeight - edge) {
      top = rect.top - height - gap;
    }
    top = Math.max(edge, Math.min(top, viewportHeight - height - edge));

    menu.style.left = Math.round(left) + 'px';
    menu.style.top = Math.round(top) + 'px';
  }

  function choose(state, value) {
    if (!state || state.pending || state.select.disabled) return;

    if (value === state.select.value) {
      closeMenu(true);
      return;
    }

    state.previousValue = state.select.value;
    state.select.value = value;
    sync(state);
    closeMenu(false);

    // Preserve the existing HTMX/form contracts. Both controls already know
    // how to update their backend; this event simply removes the extra modal
    // and Apply step.
    state.select.dispatchEvent(new Event('change', { bubbles: true }));
    state.trigger.focus({ preventScroll: true });
  }

  function stateForRequest(event) {
    const elt = event.detail && event.detail.elt;
    if (!elt) return null;

    if (elt.matches && elt.matches(selector)) {
      return states.get(elt) || null;
    }

    if (elt.querySelector) {
      const select = elt.querySelector('select[data-apple-picker="1"]');
      if (select && select.matches(selector)) return states.get(select) || null;
    }

    return null;
  }

  function syncPipelineContext(state) {
    if (!state || state.kind !== 'pipeline') return;

    const value = state.select.value;
    const table = document.getElementById('lead-table-container');

    if (table) {
      const raw = table.getAttribute('hx-get');
      if (raw) {
        try {
          const url = new URL(raw, window.location.href);
          url.searchParams.set('pipeline', value);
          url.searchParams.delete('stage');
          table.setAttribute('hx-get', url.pathname + url.search);
        } catch (error) {
          // The visible switch already succeeded; URL bookkeeping is optional.
        }
      }
    }

    const filterButton = document.querySelector('.crm-premium-filter[hx-get]');
    if (filterButton) {
      const raw = filterButton.getAttribute('hx-get');
      if (raw) {
        try {
          const url = new URL(raw, window.location.href);
          url.searchParams.set('pipeline', value);
          filterButton.setAttribute('hx-get', url.pathname + url.search);
        } catch (error) {
          // Keep the existing filter URL if it cannot be parsed.
        }
      }
    }

    if (/^https?:$/.test(window.location.protocol)) {
      try {
        const pageUrl = new URL(window.location.href);
        pageUrl.searchParams.set('pipeline', value);
        pageUrl.searchParams.delete('stage');
        window.history.replaceState(
          window.history.state,
          '',
          pageUrl.pathname + pageUrl.search + pageUrl.hash
        );
      } catch (error) {
        // History updates are a convenience and must never break switching.
      }
    }
  }

  function onMenuKeydown(event) {
    if (!openState || !menu) return;

    if (event.key === 'Escape') {
      event.preventDefault();
      closeMenu(true);
      return;
    }

    if (event.key === 'Tab') {
      closeMenu(false);
      return;
    }

    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;

    const rows = Array.from(menu.querySelectorAll('.lead-picker-option'));
    if (!rows.length) return;

    event.preventDefault();
    const index = rows.indexOf(document.activeElement);

    let next;
    if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = rows.length - 1;
    else if (event.key === 'ArrowDown') next = index < 0 ? 0 : (index + 1) % rows.length;
    else next = index < 0 ? rows.length - 1 : (index - 1 + rows.length) % rows.length;

    rows[next].focus({ preventScroll: true });
  }

  document.addEventListener('pointerdown', event => {
    if (!openState || !menu) return;

    const insideMenu = menu.contains(event.target);
    const insideTrigger = openState.trigger.contains(event.target);
    if (!insideMenu && !insideTrigger) closeMenu(false);
  }, true);

  document.addEventListener('htmx:beforeRequest', event => {
    const state = stateForRequest(event);
    if (!state) return;
    setPending(state, true);
  });

  document.addEventListener('htmx:afterRequest', event => {
    const state = stateForRequest(event);
    if (!state) return;

    const successful = Boolean(event.detail && event.detail.successful);
    setPending(state, false);

    if (successful) {
      if (state.kind === 'pipeline') syncPipelineContext(state);
      state.previousValue = null;
      sync(state);
      return;
    }

    if (state.previousValue !== null) {
      state.select.value = state.previousValue;
      state.previousValue = null;
      sync(state);
    }
    flashError(state);
  });

  document.addEventListener('leadStageUpdated', event => {
    const detail = event.detail || {};
    if (!detail.lead_id || !detail.stage_id) return;

    const card = document.getElementById('lead-card-' + detail.lead_id);
    const select = card && card.querySelector('select[name="stage"]');
    const state = select && states.get(select);
    if (!state) return;

    select.value = String(detail.stage_id);
    state.previousValue = null;
    setPending(state, false);
    sync(state);
  });

  document.addEventListener('htmx:afterSwap', event => {
    if (openState && !openState.select.isConnected) closeMenu(false);
    enhance(event.detail && event.detail.target ? event.detail.target : document);
  });

  window.addEventListener('resize', () => {
    if (openState) positionMenu(openState);
  });

  document.addEventListener('scroll', () => {
    if (openState) positionMenu(openState);
  }, true);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => enhance());
  } else {
    enhance();
  }
})();
