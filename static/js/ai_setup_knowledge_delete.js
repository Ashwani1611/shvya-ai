(function () {
    "use strict";

    const DELETE_SELECTOR = "[data-knowledge-delete]";
    const DELETE_BASE = "/api/v1/ai-engagement/dashboard/knowledge/";

    function getCsrfToken(button) {
        const page = button.closest("#ai-setup-page") || document;
        const input = page.querySelector('input[name="csrfmiddlewaretoken"]');

        if (input && input.value) {
            return input.value;
        }

        const cookie = document.cookie
            .split(";")
            .map(function (part) {
                return part.trim();
            })
            .find(function (part) {
                return part.startsWith("csrftoken=");
            });

        return cookie ? decodeURIComponent(cookie.slice("csrftoken=".length)) : "";
    }

    function getDeleteEndpoint(kind, id) {
        if (!id || (kind !== "source" && kind !== "document")) {
            return "";
        }

        return (
            DELETE_BASE +
            encodeURIComponent(kind) +
            "/" +
            encodeURIComponent(id) +
            "/delete/"
        );
    }

    async function getErrorMessage(response) {
        try {
            const payload = await response.json();
            return payload.detail || payload.message || "Unable to delete this knowledge item.";
        } catch (error) {
            return "Unable to delete this knowledge item.";
        }
    }

    document.addEventListener(
        "click",
        async function (event) {
            const button = event.target.closest(DELETE_SELECTOR);

            if (!button) {
                return;
            }

            /*
             * ai_setup.html historically registered a UI-only click handler
             * that removed the row without deleting backend data. Handle the
             * click during capture and stop that legacy handler from running.
             */
            event.preventDefault();
            event.stopImmediatePropagation();

            if (button.dataset.deleteBusy === "true") {
                return;
            }

            const kind = button.dataset.deleteKind || "";
            const id = button.dataset.deleteId || "";
            const name = button.dataset.deleteName || "this item";
            const endpoint = getDeleteEndpoint(kind, id);

            if (!endpoint) {
                window.alert("Unable to identify the knowledge item to delete.");
                return;
            }

            if (!window.confirm("Permanently delete " + name + "?")) {
                return;
            }

            const csrfToken = getCsrfToken(button);

            if (!csrfToken) {
                window.alert("Unable to verify this delete request. Please refresh the page and try again.");
                return;
            }

            const originalHtml = button.innerHTML;
            button.dataset.deleteBusy = "true";
            button.disabled = true;
            button.innerHTML = '<i class="ti ti-loader-2 animate-spin text-[15px]"></i> Deleting...';

            try {
                const response = await fetch(endpoint, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "X-CSRFToken": csrfToken,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                });

                if (!response.ok) {
                    throw new Error(await getErrorMessage(response));
                }

                /* Reload authoritative server state after the hard delete. */
                window.location.reload();
            } catch (error) {
                window.alert(error.message || "Unable to delete this knowledge item.");
                button.disabled = false;
                button.dataset.deleteBusy = "false";
                button.innerHTML = originalHtml;
            }
        },
        true
    );
})();
