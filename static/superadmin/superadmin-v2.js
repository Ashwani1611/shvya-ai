(function () {
    "use strict";

    const STORAGE_KEY = "shvya-superadmin-sidebar-collapsed";
    const HISTORY_STORAGE_KEY = "shvya-superadmin-history-collapsed";

    function textOf(element) {
        return (element && element.textContent ? element.textContent : "")
            .replace(/\s+/g, " ")
            .trim();
    }

    function findCardByHeading(label) {
        const headings = document.querySelectorAll(".sa-content h2");
        for (const heading of headings) {
            if (textOf(heading) === label) {
                return heading.closest(".bg-white.border.rounded-lg, section");
            }
        }
        return null;
    }

    function csrfToken() {
        const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : "";
    }

    function makeButton(label, className) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = className || "sa-inline-action";
        button.textContent = label;
        return button;
    }

    function initializeSidebarCollapse() {
        const sidebar = document.getElementById("superadmin-sidebar");
        const button = document.getElementById("superadmin-collapse-button");
        if (!sidebar || !button) return;

        function apply(collapsed, persist) {
            if (window.innerWidth <= 900) collapsed = false;
            document.body.classList.toggle("sa-sidebar-collapsed", collapsed);
            sidebar.classList.toggle("is-collapsed", collapsed);
            button.setAttribute("aria-expanded", collapsed ? "false" : "true");
            button.setAttribute(
                "aria-label",
                collapsed ? "Expand navigation" : "Collapse navigation"
            );
            button.setAttribute(
                "title",
                collapsed ? "Expand sidebar" : "Collapse sidebar"
            );
            if (persist) {
                try {
                    localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
                } catch (error) {
                    // Storage can be unavailable in privacy modes; UI still works.
                }
            }
        }

        let storedCollapsed = false;
        try {
            storedCollapsed = localStorage.getItem(STORAGE_KEY) === "1";
        } catch (error) {
            storedCollapsed = false;
        }
        apply(storedCollapsed, false);

        button.addEventListener("click", function () {
            apply(!document.body.classList.contains("sa-sidebar-collapsed"), true);
        });

        window.addEventListener("resize", function () {
            if (window.innerWidth <= 900) {
                apply(false, false);
            } else {
                let shouldCollapse = false;
                try {
                    shouldCollapse = localStorage.getItem(STORAGE_KEY) === "1";
                } catch (error) {
                    shouldCollapse = false;
                }
                apply(shouldCollapse, false);
            }
        });
    }

    function applyPremiumCards() {
        document
            .querySelectorAll(
                ".sa-content .bg-white.border.rounded-lg, .sa-content > section.border.bg-white"
            )
            .forEach(function (card) {
                if (!card.closest("[id$='Modal']")) {
                    card.classList.add("sa-premium-card");
                }
            });
    }

    function addInformationCompactToggle(card) {
        if (!card) return;
        card.classList.add("sa-org-information-card", "is-compact");
        const header = card.firstElementChild;
        if (!header) return;

        const toolbar = document.createElement("div");
        toolbar.className = "sa-card-toolbar";

        const existingEdit = header.querySelector("a");
        if (existingEdit) {
            existingEdit.classList.add("sa-inline-action");
            toolbar.appendChild(existingEdit);
        }

        const toggle = makeButton("Show all details");
        toggle.setAttribute("aria-expanded", "false");
        toggle.addEventListener("click", function () {
            const compact = card.classList.toggle("is-compact");
            toggle.textContent = compact ? "Show all details" : "Show less";
            toggle.setAttribute("aria-expanded", compact ? "false" : "true");
        });
        toolbar.appendChild(toggle);
        header.appendChild(toolbar);
    }

    function enhanceNotes(card, data) {
        if (!card) return;
        card.classList.add("sa-org-notes-card");
        const header = card.firstElementChild;
        const body = card.lastElementChild;
        if (!header || !body) return;

        const headingWrap = header.querySelector("div") || header;
        const edit = makeButton("Edit note");
        header.classList.add("flex", "items-start", "justify-between", "gap-3");
        header.appendChild(edit);

        const originalText = textOf(body);
        const emptyText = "No operational notes have been added.";
        const noteValue = originalText === emptyText ? "" : originalText;
        const preview = body.firstElementChild || document.createElement("div");
        preview.classList.add("sa-note-preview");

        if (data.noteUpdated) {
            const meta = document.createElement("div");
            meta.className = "sa-note-meta";
            meta.textContent = "Last saved " + data.noteUpdated;
            body.appendChild(meta);
        }

        const form = document.createElement("form");
        form.method = "post";
        form.action = data.notesUrl;
        form.className = "sa-inline-editor";

        const csrf = document.createElement("input");
        csrf.type = "hidden";
        csrf.name = "csrfmiddlewaretoken";
        csrf.value = csrfToken();

        const textarea = document.createElement("textarea");
        textarea.name = "operational_notes";
        textarea.maxLength = 10000;
        textarea.value = noteValue;
        textarea.placeholder = "Add internal context, decisions, blockers, next steps or anything the Superadmin team should know…";
        textarea.setAttribute("aria-label", "Operational notes");

        const hint = document.createElement("div");
        hint.className = "sa-editor-hint";
        hint.textContent = "Only Superadmin users can see these notes. Saving records the latest date and time.";

        const actions = document.createElement("div");
        actions.className = "sa-inline-editor-actions";
        const cancel = makeButton("Cancel");
        const save = document.createElement("button");
        save.type = "submit";
        save.className = "sa-primary-button";
        save.textContent = "Save note";
        actions.append(cancel, save);

        form.append(csrf, textarea, hint, actions);
        body.appendChild(form);

        function setEditing(open) {
            form.classList.toggle("is-open", open);
            edit.textContent = open ? "Close editor" : "Edit note";
            edit.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) {
                window.setTimeout(function () {
                    textarea.focus();
                    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
                }, 20);
            }
        }

        edit.addEventListener("click", function () {
            setEditing(!form.classList.contains("is-open"));
        });
        cancel.addEventListener("click", function () {
            textarea.value = noteValue;
            setEditing(false);
        });
    }

    function enhanceTags(card, data) {
        if (!card) return;
        card.classList.add("sa-org-tags-card");
        const header = card.firstElementChild;
        const body = card.lastElementChild;
        if (!header || !body) return;

        const manage = header.querySelector("button") || makeButton("Edit tags");
        manage.disabled = false;
        manage.removeAttribute("disabled");
        manage.className = "sa-inline-action";
        manage.textContent = "Edit tags";
        if (!manage.parentNode) header.appendChild(manage);

        const tagNames = [];
        body.querySelectorAll("span").forEach(function (chip) {
            const value = textOf(chip);
            if (value && value !== "No tags assigned.") tagNames.push(value);
        });
        const existingWrap = body.querySelector(".flex.flex-wrap");
        if (existingWrap) existingWrap.classList.add("sa-tags-preview");

        const form = document.createElement("form");
        form.method = "post";
        form.action = data.tagsUrl;
        form.className = "sa-inline-editor";

        const csrf = document.createElement("input");
        csrf.type = "hidden";
        csrf.name = "csrfmiddlewaretoken";
        csrf.value = csrfToken();

        const input = document.createElement("input");
        input.type = "text";
        input.name = "tags";
        input.value = tagNames.join(", ");
        input.placeholder = "Trial, Growth Lab, WhatsApp Ban";
        input.setAttribute("aria-label", "Organization tags separated by commas");

        const hint = document.createElement("div");
        hint.className = "sa-editor-hint";
        hint.textContent = "Separate tags with commas. Existing platform tags are reused automatically.";

        const actions = document.createElement("div");
        actions.className = "sa-inline-editor-actions";
        const cancel = makeButton("Cancel");
        const save = document.createElement("button");
        save.type = "submit";
        save.className = "sa-primary-button";
        save.textContent = "Save tags";
        actions.append(cancel, save);

        form.append(csrf, input, hint, actions);
        body.appendChild(form);

        function setEditing(open) {
            form.classList.toggle("is-open", open);
            manage.textContent = open ? "Close editor" : "Edit tags";
            manage.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) window.setTimeout(function () { input.focus(); }, 20);
        }

        manage.addEventListener("click", function () {
            setEditing(!form.classList.contains("is-open"));
        });
        cancel.addEventListener("click", function () {
            input.value = tagNames.join(", ");
            setEditing(false);
        });

        if (new URLSearchParams(window.location.search).get("edit_tags") === "1") {
            setEditing(true);
        }
    }

    function makeAccountInformationCompact(card) {
        if (!card) return;
        card.classList.add("sa-org-account-card");
        const header = card.firstElementChild;
        const body = card.lastElementChild;
        if (!header || !body) return;

        header.classList.add("flex", "items-center", "justify-between", "gap-3");
        const toggle = makeButton("Show account details");
        toggle.setAttribute("aria-expanded", "false");
        header.appendChild(toggle);
        body.hidden = true;

        toggle.addEventListener("click", function () {
            body.hidden = !body.hidden;
            toggle.textContent = body.hidden ? "Show account details" : "Hide details";
            toggle.setAttribute("aria-expanded", body.hidden ? "false" : "true");
        });
    }

    function replaceLastLoginWithChannelActivity(data) {
        const usersCard = findCardByHeading("Organization Users");
        if (!usersCard) return;
        const table = usersCard.querySelector("table");
        if (!table) return;

        const headers = Array.from(table.querySelectorAll("thead th"));
        const index = headers.findIndex(function (header) {
            return textOf(header) === "Last Login";
        });
        if (index < 0) return;

        headers[index].textContent = "WhatsApp Activity";
        table.querySelectorAll("tbody tr").forEach(function (row) {
            const cells = row.querySelectorAll("td");
            const cell = cells[index];
            if (!cell) return;
            cell.innerHTML = "";
            cell.classList.add("sa-channel-activity");

            const badge = document.createElement("span");
            badge.className = "sa-channel-status " + (data.channelActive ? "is-active" : "is-inactive");
            badge.textContent = data.channelLabel;

            const detail = document.createElement("div");
            detail.className = "sa-channel-detail";
            if (!data.channelActive && data.channelSince) {
                detail.textContent = "Inactive since " + data.channelSince;
            } else {
                detail.textContent = data.channelDetail;
            }
            cell.append(badge, detail);
        });
    }

    function reorganizeOrganizationWorkspace() {
        const dataElement = document.getElementById("sa-org-workspace-data");
        if (!dataElement) return;

        document.body.classList.add("sa-org-detail-page");
        const data = {
            notesUrl: dataElement.dataset.notesUrl || "",
            tagsUrl: dataElement.dataset.tagsUrl || "",
            noteUpdated: dataElement.dataset.noteUpdated || "",
            channelActive: dataElement.dataset.channelActive === "true",
            channelLabel: dataElement.dataset.channelLabel || "Never connected",
            channelDetail: dataElement.dataset.channelDetail || "",
            channelSince: dataElement.dataset.channelSince || "",
        };

        const content = document.querySelector(".sa-content");
        const pageH1 = content ? content.querySelector("h1") : null;
        const hero = pageH1 ? pageH1.closest(".mb-6") : null;
        if (hero) hero.classList.add("sa-org-hero");

        const info = findCardByHeading("Organization Information");
        const notes = findCardByHeading("Operational Notes");
        const tags = findCardByHeading("Tags");
        const account = findCardByHeading("Account Information");

        if (info && notes && tags && info.parentNode) {
            const workspace = document.createElement("div");
            workspace.id = "workspace";
            workspace.className = "sa-org-workspace-grid";

            const main = document.createElement("div");
            main.className = "sa-org-workspace-main";
            const side = document.createElement("div");
            side.className = "sa-org-workspace-side";

            info.parentNode.insertBefore(workspace, info);
            workspace.append(main, side);
            main.appendChild(info);
            side.append(notes, tags);
            if (account) side.appendChild(account);
        }

        addInformationCompactToggle(info);
        enhanceNotes(notes, data);
        enhanceTags(tags, data);
        makeAccountInformationCompact(account);
        replaceLastLoginWithChannelActivity(data);

        const controls = findCardByHeading("Account Controls");
        if (controls) controls.id = "account-controls";

        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get("reset_password") === "1") {
            const resetUser = urlParams.get("reset_user");
            const select = document.getElementById("resetPasswordUser");
            if (select && resetUser) select.value = resetUser;
            if (typeof window.openResetPasswordModal === "function") {
                window.setTimeout(window.openResetPasswordModal, 40);
            }
        }

        document.querySelectorAll(".sa-content .mt-6.p-4.border.border-indigo-100").forEach(function (notice) {
            if (textOf(notice).startsWith("Superadmin Control Center")) {
                notice.remove();
            }
        });
    }

    function enhanceAiCreditOverview() {
        if (!window.location.pathname.includes("/superadmin/ai-credits")) return;
        document.body.classList.add("sa-ai-credit-page");
        document.querySelectorAll(".sa-data-table tbody tr").forEach(function (row) {
            if (row.querySelector('a[href*="ai-credits"]')) row.classList.add("sa-wallet-row");
        });
    }

    function buildHistorySummary(section, body) {
        const rows = Array.from(body.querySelectorAll("tbody tr")).filter(function (row) {
            return !row.querySelector("td[colspan]");
        });
        if (!rows.length) return null;

        let positive = 0;
        let negative = 0;
        rows.forEach(function (row) {
            const cells = row.querySelectorAll("td");
            if (cells.length < 3) return;
            const amount = Number(textOf(cells[2]).replace(/[^0-9+\-.]/g, ""));
            if (!Number.isFinite(amount)) return;
            if (amount >= 0) positive += amount;
            else negative += Math.abs(amount);
        });

        const summary = document.createElement("div");
        summary.className = "sa-history-summary";
        const values = [
            ["Visible transactions", String(rows.length)],
            ["Credits added", "+" + positive.toLocaleString()],
            ["Credits consumed", "−" + negative.toLocaleString()],
        ];
        values.forEach(function (item) {
            const tile = document.createElement("div");
            tile.className = "sa-history-summary-item";
            const label = document.createElement("div");
            label.className = "sa-history-summary-label";
            label.textContent = item[0];
            const value = document.createElement("div");
            value.className = "sa-history-summary-value";
            value.textContent = item[1];
            tile.append(label, value);
            summary.appendChild(tile);
        });
        return summary;
    }

    function enhanceTransactionHistory() {
        const section = document.getElementById("history");
        if (!section) return;
        document.body.classList.add("sa-credit-detail-page");

        const header = section.firstElementChild;
        if (!header) return;

        const body = document.createElement("div");
        body.className = "sa-history-body";
        while (header.nextSibling) {
            body.appendChild(header.nextSibling);
        }

        const summary = buildHistorySummary(section, body);
        if (summary) section.appendChild(summary);
        section.appendChild(body);

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "sa-history-toggle";
        toggle.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m7 10 5 5 5-5" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Hide history</span>';
        header.appendChild(toggle);

        let collapsed = false;
        try {
            collapsed = localStorage.getItem(HISTORY_STORAGE_KEY) === "1";
        } catch (error) {
            collapsed = false;
        }

        function apply(isCollapsed) {
            body.classList.toggle("is-collapsed", isCollapsed);
            if (summary) summary.hidden = isCollapsed;
            toggle.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
            const label = toggle.querySelector("span");
            if (label) label.textContent = isCollapsed ? "Show history" : "Hide history";
            try {
                localStorage.setItem(HISTORY_STORAGE_KEY, isCollapsed ? "1" : "0");
            } catch (error) {
                // Ignore storage failures.
            }
        }

        apply(collapsed);
        toggle.addEventListener("click", function () {
            apply(!body.classList.contains("is-collapsed"));
        });
    }

    function enhanceCreditDetail() {
        if (!window.location.pathname.includes("/ai-credits/")) return;
        if (!document.getElementById("overview")) return;
        document.body.classList.add("sa-credit-detail-page");
        document.querySelectorAll(".sa-content section").forEach(function (section) {
            section.classList.add("sa-premium-card");
        });
        enhanceTransactionHistory();
    }

    document.addEventListener("DOMContentLoaded", function () {
        initializeSidebarCollapse();
        applyPremiumCards();
        reorganizeOrganizationWorkspace();
        enhanceAiCreditOverview();
        enhanceCreditDetail();
    });
})();
