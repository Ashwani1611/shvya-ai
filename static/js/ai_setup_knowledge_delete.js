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

    function safeFilename(value) {
        const normalized = String(value || "pasted-knowledge")
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "-")
            .replace(/^-+|-+$/g, "")
            .slice(0, 60);
        return (normalized || "pasted-knowledge") + "-" + Date.now() + ".txt";
    }

    function installPasteKnowledgeCard() {
        const page = document.querySelector("#ai-setup-page");
        if (!page || page.querySelector("[data-paste-knowledge-card]")) {
            return;
        }

        const uploadAction = page.querySelector('input[name="action"][value="upload_file"]');
        if (!uploadAction) {
            return;
        }

        const uploadForm = uploadAction.closest("form");
        const uploadCard = uploadForm ? uploadForm.parentElement : null;
        const sourceGrid = uploadCard ? uploadCard.parentElement : null;
        if (!sourceGrid) {
            return;
        }

        sourceGrid.classList.remove("lg:grid-cols-2");
        sourceGrid.classList.add("xl:grid-cols-3");

        const card = document.createElement("div");
        card.dataset.pasteKnowledgeCard = "true";
        card.className = "rounded-xl border border-gray-200 p-5 bg-gray-50";
        card.innerHTML = `
            <div class="flex items-center gap-3 mb-4">
                <span class="flex items-center justify-center w-9 h-9 rounded-lg bg-white border border-gray-200 text-gray-600">
                    <i class="ti ti-clipboard-text"></i>
                </span>
                <div>
                    <p class="text-sm font-semibold text-gray-800">Paste Text</p>
                    <p class="text-xs text-gray-500 mt-0.5">Paste pricing, plans, FAQs, product details, or other knowledge.</p>
                </div>
            </div>
            <form data-paste-knowledge-form class="space-y-3">
                <div>
                    <label class="block text-xs font-medium text-gray-700 mb-2" for="pasted-knowledge-name">Source name</label>
                    <input id="pasted-knowledge-name" name="name" type="text" placeholder="Pricing and plans" class="w-full rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-sm text-gray-700 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-500">
                </div>
                <div>
                    <label class="block text-xs font-medium text-gray-700 mb-2" for="pasted-knowledge-content">Knowledge text</label>
                    <textarea id="pasted-knowledge-content" name="content" rows="8" required placeholder="Starter plan: ₹999/month\nPro plan: ₹2,499/month\n..." class="w-full rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-sm text-gray-700 placeholder-gray-400 resize-y focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-500"></textarea>
                </div>
                <button type="submit" class="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-900 text-white text-sm font-medium hover:bg-gray-800">
                    <i class="ti ti-database-plus"></i>
                    <span data-paste-submit-label>Add knowledge</span>
                </button>
                <p class="text-xs text-gray-400">Text is saved as an indexed .txt knowledge document and becomes available after processing finishes.</p>
            </form>
        `;

        sourceGrid.appendChild(card);

        const form = card.querySelector("[data-paste-knowledge-form]");
        form.addEventListener("submit", async function (event) {
            event.preventDefault();

            const content = String(form.elements.content.value || "").trim();
            const name = String(form.elements.name.value || "").trim() || "Pasted knowledge";
            if (!content) {
                window.alert("Paste some knowledge text first.");
                return;
            }

            const csrfToken = getCsrfToken(card);
            if (!csrfToken) {
                window.alert("Unable to verify this request. Please refresh the page and try again.");
                return;
            }

            const button = form.querySelector('button[type="submit"]');
            const label = form.querySelector("[data-paste-submit-label]");
            button.disabled = true;
            label.textContent = "Adding...";

            const payload = new FormData();
            payload.append("csrfmiddlewaretoken", csrfToken);
            payload.append("action", "upload_file");
            payload.append("name", name);
            payload.append(
                "file",
                new File([content + "\n"], safeFilename(name), { type: "text/plain;charset=utf-8" })
            );

            try {
                const response = await fetch(window.location.href, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "X-CSRFToken": csrfToken,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    body: payload,
                });

                if (!response.ok) {
                    throw new Error("Unable to add pasted knowledge.");
                }

                window.location.reload();
            } catch (error) {
                window.alert(error.message || "Unable to add pasted knowledge.");
                button.disabled = false;
                label.textContent = "Add knowledge";
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installPasteKnowledgeCard);
    } else {
        installPasteKnowledgeCard();
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
