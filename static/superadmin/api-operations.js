(() => {
  "use strict";
  const refresh = document.querySelector("[data-api-refresh]");
  const toggle = document.querySelector("[data-api-auto-refresh]");
  const controls = document.querySelector("[data-api-refresh-controls]");
  const status = document.querySelector("[data-api-refresh-status]");
  const key = "shvya-api-operations-auto-refresh";
  const interval = 60000;
  let timer = null;
  let leaving = false;

  // Compare with server-rendered defaults, not a load-time snapshot. Browsers
  // may restore edited values before this script runs or when navigating Back.
  const hasUnappliedChanges = () => Array.from(
    document.querySelectorAll(".api-workspace form")
  ).some(form => Array.from(form.elements).some(field => {
    if (!field.name || field.disabled) return false;
    if (["submit", "reset", "button", "hidden"].includes(field.type)) return false;
    if (["checkbox", "radio"].includes(field.type)) return field.checked !== field.defaultChecked;
    if (field.type === "file") return field.files.length > 0;
    if (field.options) {
      const options = Array.from(field.options);
      let defaults = options.map(option => option.defaultSelected);
      // A single-select with no explicit default selects its first enabled item.
      if (!field.multiple && !defaults.some(Boolean)) {
        const first = options.findIndex(option => !option.disabled && !option.parentElement?.disabled);
        defaults = options.map((_, index) => index === first);
      }
      return options.some((option, index) => option.selected !== defaults[index]);
    }
    return "defaultValue" in field && field.value !== field.defaultValue;
  }));

  const editing = () => {
    const active = document.activeElement;
    return active !== toggle && Boolean(active && (
      active.isContentEditable || active.matches("input, select, textarea")
    ));
  };
  const setStatus = text => {
    if (status && status.textContent !== text) status.textContent = text;
  };
  const stop = () => {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  };
  const pauseReason = () => {
    if (leaving) return "Paused while the page is loading.";
    if (document.hidden) return "Paused while this tab is hidden.";
    if (window.navigator.onLine === false) return "Paused while you are offline.";
    if (hasUnappliedChanges()) return "Paused: apply or reset your changes.";
    if (editing()) return "Paused while you are editing.";
    return "";
  };
  const schedule = () => {
    stop();
    if (!toggle || !toggle.checked) {
      setStatus("Auto-refresh is off.");
      return;
    }
    const reason = pauseReason();
    setStatus(reason || "Refreshes every 60 seconds while this view is idle.");
    // Hidden/offline pages do not poll. An idle timer can recheck script-only
    // field changes; a paused view never reloads until it is safe again.
    if (leaving || document.hidden || window.navigator.onLine === false) return;
    timer = window.setTimeout(() => {
      timer = null;
      // Recheck at execution time: script/autofill changes may not emit events.
      const blocked = pauseReason();
      if (!toggle.checked || blocked) {
        schedule();
        return;
      }
      leaving = true;
      setStatus("Refreshing recorded activity...");
      window.location.reload();
    }, interval);
  };

  if (refresh) refresh.addEventListener("click", () => {
    if (hasUnappliedChanges() && !window.confirm("Reload and discard your unapplied changes?")) return;
    stop();
    leaving = true;
    window.location.reload();
  });
  // Detail/investigation pages have no toggle and must never auto-reload.
  if (!toggle) return;
  try { toggle.checked = window.sessionStorage.getItem(key) === "on"; }
  catch (_) { toggle.checked = false; }
  if (controls) controls.hidden = false;
  toggle.addEventListener("change", () => {
    try { window.sessionStorage.setItem(key, toggle.checked ? "on" : "off"); }
    catch (_) { /* Restricted storage: the current page still works. */ }
    schedule();
  });
  document.addEventListener("visibilitychange", schedule);
  document.addEventListener("input", schedule);
  document.addEventListener("change", schedule);
  document.addEventListener("focusin", schedule);
  document.addEventListener("focusout", () => window.setTimeout(schedule, 0));
  document.addEventListener("reset", () => window.setTimeout(schedule, 0));
  document.addEventListener("submit", event => {
    if (!event.defaultPrevented && event.target.closest(".api-workspace")) {
      leaving = true;
      schedule();
    }
  });
  window.addEventListener("offline", schedule);
  window.addEventListener("online", schedule);
  window.addEventListener("pagehide", () => { leaving = true; stop(); });
  window.addEventListener("pageshow", () => { leaving = false; schedule(); });
  schedule();
})();
