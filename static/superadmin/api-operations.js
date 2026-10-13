(() => {
  "use strict";
  const refresh = document.querySelector("[data-api-refresh]");
  if (refresh) refresh.addEventListener("click", () => window.location.reload());

  // Opt-in 60-second refresh. Preserve filters in the URL; never reload while
  // an operator is editing a filter or viewing another browser tab.
  const toggle = document.querySelector("[data-api-auto-refresh]");
  if (!toggle) return;
  const key = "shvya-api-operations-auto-refresh";
  let timer = null;
  try { toggle.checked = sessionStorage.getItem(key) === "on"; } catch (_) { /* restricted storage */ }
  const refreshIfSafe = () => {
    if (document.hidden || document.activeElement?.matches("input, select, textarea, [contenteditable='true']")) return;
    window.location.reload();
  };
  const schedule = () => {
    if (timer) window.clearInterval(timer);
    timer = toggle.checked ? window.setInterval(refreshIfSafe, 60000) : null;
  };
  toggle.addEventListener("change", () => {
    try { sessionStorage.setItem(key, toggle.checked ? "on" : "off"); } catch (_) { /* restricted storage */ }
    schedule();
  });
  schedule();
})();
