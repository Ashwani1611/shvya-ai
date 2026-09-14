(function () {
    'use strict';

    var form = document.getElementById('settings-form');
    if (!form) return;

    var csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
    var csrf = csrfInput ? csrfInput.value : '';
    var settingsUrl = '';
    var errorBox = document.getElementById('settings-error');
    var instantNames = new Set(['ai_auto_reply', 'auto_follow_up']);

    function showError(message) {
        if (!errorBox) return;
        errorBox.textContent = message || 'Unable to save this setting.';
        errorBox.classList.remove('hidden');
    }

    function clearError() {
        if (!errorBox) return;
        errorBox.textContent = '';
        errorBox.classList.add('hidden');
    }

    document.querySelectorAll('.js-session-settings').forEach(function (button) {
        button.addEventListener('click', function () {
            settingsUrl = button.dataset.url || '';
            clearError();
        });
    });

    form.querySelectorAll('input[type="checkbox"]').forEach(function (toggle) {
        if (!instantNames.has(toggle.name)) return;

        toggle.addEventListener('change', async function () {
            if (!settingsUrl) {
                showError('Unable to identify this Hosted Account. Close Settings and open it again.');
                return;
            }

            var requestedValue = !!toggle.checked;
            toggle.disabled = true;
            clearError();

            try {
                var response = await fetch(settingsUrl, {
                    method: 'POST',
                    headers: {
                        'X-CSRFToken': csrf,
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify((function () {
                        var payload = {};
                        payload[toggle.name] = requestedValue;
                        return payload;
                    })()),
                    // Keep the tiny settings write alive if the user refreshes
                    // immediately after moving the switch.
                    keepalive: true,
                });
                var result = await response.json();
                if (!response.ok || !result.ok) {
                    throw new Error(result.error || 'Unable to save this setting.');
                }

                var saved = result.settings || {};
                if (Object.prototype.hasOwnProperty.call(saved, toggle.name)) {
                    toggle.checked = !!saved[toggle.name];
                } else {
                    toggle.checked = requestedValue;
                }
            } catch (error) {
                toggle.checked = !requestedValue;
                showError(error && error.message ? error.message : 'Unable to save this setting.');
            } finally {
                toggle.disabled = false;
            }
        });
    });
})();
