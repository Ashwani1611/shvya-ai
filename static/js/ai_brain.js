(function () {
    "use strict";

    function initAIBrain() {
        const page = document.querySelector("[data-ai-brain-playbook]");
        if (!page || page.dataset.brainInitialized) return;
        page.dataset.brainInitialized = "true";

        const playbook = page.querySelector("#ai_playbook");
        const settingsForm = page.querySelector("#org-ai-settings-form");
        const saveStatus = page.querySelector("[data-save-status]");
        settingsForm.addEventListener("submit", async function (event) {
            event.preventDefault();
            const button = settingsForm.querySelector('button[type="submit"]');
            if (button.disabled) return;
            const payload = new FormData(settingsForm);
            button.disabled = true;
            saveStatus.textContent = "Saving…";
            try {
                const response = await fetch(settingsForm.action, {
                    method: "POST", credentials: "same-origin",
                    headers: { "Accept": "application/json" }, body: payload
                });
                const result = await response.json();
                if (!response.ok || !result.saved) throw new Error(result.error || "Your changes could not be saved. Please try again.");
                if (result.ai_playbook !== String(payload.get("ai_playbook") || "").trim()) throw new Error("The saved Playbook differs from your draft. Please refresh and check before testing.");
                saveStatus.textContent = "Saved successfully.";
                if (playbook.value.trim() !== result.ai_playbook) {
                    saveStatus.textContent = "Saved the submitted version. You have newer unsaved edits.";
                    return;
                }
                window.location.assign(settingsForm.action);
            } catch (error) {
                saveStatus.textContent = error instanceof SyntaxError ? "Could not confirm the save. Your draft is still here; refresh your session and try again." : error.message;
                saveStatus.setAttribute("role", "alert");
            } finally {
                button.disabled = false;
            }
        });
        const count = page.querySelector("[data-playbook-count]");
        const updateCount = function () {
            count.textContent = playbook.value.length.toLocaleString() + " characters";
        };
        playbook.addEventListener("input", updateCount);
        updateCount();
        page.querySelector("[data-insert-playbook-format]").addEventListener("click", function () {
            if (playbook.value.trim()) {
                // Existing organization instructions must never be silently replaced.
                if (!window.confirm("Replace the current playbook with empty section headings? Your existing instructions will be removed from this draft.")) return;
            }
            playbook.value = page.querySelector("#playbook-standard-format").textContent;
            updateCount();
            playbook.focus();
        });

        const urlList = page.querySelector("[data-knowledge-url-list]");
        const addUrl = page.querySelector("[data-add-knowledge-url]");
        const urlFeedback = page.querySelector("[data-url-feedback]");
        let urlSequence = urlList.querySelectorAll("input").length;
        addUrl.addEventListener("click", function () {
            if (urlList.querySelectorAll("input").length >= 10) {
                urlFeedback.textContent = "You can add up to 10 URLs at a time.";
                return;
            }
            urlSequence += 1;
            const row = document.createElement("div");
            row.className = "brain-url-row";
            const input = document.createElement("input");
            input.type = "url";
            input.name = "knowledge_urls";
            input.id = "knowledge-url-" + urlSequence;
            input.placeholder = "https://your-website.com";
            input.setAttribute("aria-label", "Website URL " + urlSequence);
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "brain-icon-button";
            remove.dataset.removeKnowledgeUrl = "";
            remove.setAttribute("aria-label", "Remove website URL");
            remove.textContent = "×";
            row.append(input, remove);
            urlList.appendChild(row);
            urlFeedback.textContent = "";
            input.focus();
        });
        urlList.addEventListener("click", function (event) {
            const remove = event.target.closest("[data-remove-knowledge-url]");
            if (!remove) return;
            remove.closest(".brain-url-row").remove();
            urlFeedback.textContent = "";
            addUrl.focus();
        });

        page.querySelectorAll("input[type=file][data-max-upload-bytes]").forEach(function (input) {
            input.addEventListener("change", function () {
                const file = input.files[0];
                const maxBytes = Number(input.dataset.maxUploadBytes);
                input.setCustomValidity(file && file.size > maxBytes ? "This file exceeds the upload size limit." : "");
                if (!input.reportValidity()) return;
                const zone = input.closest("[data-upload-zone]");
                if (zone) zone.querySelector("[data-upload-label]").textContent = file ? file.name : "Click to browse or drag and drop your file";
            });
        });
        page.querySelectorAll("[data-upload-zone]").forEach(function (zone) {
            zone.addEventListener("dragenter", function () { zone.classList.add("is-dragging"); });
            zone.addEventListener("dragleave", function () { zone.classList.remove("is-dragging"); });
            zone.addEventListener("drop", function () { zone.classList.remove("is-dragging"); });
        });
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initAIBrain);
    else initAIBrain();
    document.addEventListener("htmx:afterSwap", initAIBrain);
})();
