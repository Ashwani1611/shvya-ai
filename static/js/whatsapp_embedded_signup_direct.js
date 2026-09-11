(function () {
  'use strict';

  function installDirectEmbeddedSignup() {
    var currentButton = document.getElementById('embedded-signup-btn');
    if (!currentButton || currentButton.dataset.directOauthBound === '1') return;

    // The template's legacy handler calls FB.login(). In current Chromium,
    // Meta/Facebook can route that call through FedCM even when fedCM is not
    // enabled. Login for Business configurations are not supported by that
    // browser path, so it can terminate before an OAuth code reaches SHVYA.
    // Replacing the node removes that handler completely.
    var button = currentButton.cloneNode(true);
    button.dataset.directOauthBound = '1';
    button.disabled = false;
    var label = button.querySelector('span');
    if (label) label.textContent = 'Connect WhatsApp Business API';
    currentButton.parentNode.replaceChild(button, currentButton);

    // The direct OAuth flow does not depend on connect.facebook.net/sdk.js.
    // Suppress only the legacy SDK-load warning; all real connection errors
    // continue to render normally.
    if (typeof window.showSignupError === 'function' && !window.__shvyaDirectOAuthErrorWrapper) {
      var originalShowSignupError = window.showSignupError;
      window.showSignupError = function (message) {
        var text = String(message || '');
        if (text.indexOf("Meta's connection script did not load") !== -1) return;
        return originalShowSignupError(message);
      };
      window.__shvyaDirectOAuthErrorWrapper = true;
    }

    button.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();

      if (typeof window.clearSignupError === 'function') {
        window.clearSignupError();
      }

      button.disabled = true;
      if (label) label.textContent = 'Opening Meta…';

      // Use a first-party server redirect instead of FB.login(). This keeps the
      // same Facebook Login for Business config_id flow while avoiding the
      // browser-initiated FedCM path entirely.
      var startUrl = new URL('direct/start/', window.location.href);
      window.location.assign(startUrl.toString());
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installDirectEmbeddedSignup, {once: true});
  } else {
    installDirectEmbeddedSignup();
  }
})();
