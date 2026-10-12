(() => {
  "use strict";
  const refresh = document.querySelector("[data-api-refresh]");
  if (refresh) refresh.addEventListener("click", () => window.location.reload());
})();
