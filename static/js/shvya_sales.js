(function () {
    "use strict";

    function money(value) {
        var number = Number(value || 0);
        return Number.isFinite(number) ? number : 0;
    }

    function documentBuilder() {
        var form = document.querySelector("[data-sales-document-form]");
        if (!form) return;

        var list = form.querySelector("[data-line-items]");
        var hidden = form.querySelector("[data-line-items-json]");
        var add = form.querySelector("[data-add-line]");
        var totalNode = form.querySelector("[data-live-total]");
        var discount = form.querySelector("[data-discount-total]");
        var currency = form.querySelector('input[name="currency"]');

        function rowTemplate(item) {
            item = item || {};
            var row = document.createElement("div");
            row.className = "sy-line-row";
            row.setAttribute("data-line-row", "");
            row.innerHTML =
                '<div class="sy-line-item-main"><input data-item-name placeholder="Product / service"><input data-item-description placeholder="Optional description"></div>' +
                '<input type="number" min="0" step="0.01" value="1" data-item-qty aria-label="Quantity">' +
                '<input type="number" min="0" step="0.01" value="0" data-item-rate aria-label="Rate">' +
                '<input type="number" min="0" step="0.01" value="0" data-item-tax aria-label="Tax percentage">' +
                '<strong data-item-amount>0.00</strong>' +
                '<button type="button" class="sy-icon-btn sy-remove-line" data-remove-line aria-label="Remove item"><i class="ti ti-trash"></i></button>';
            row.querySelector("[data-item-name]").value = item.name || "";
            row.querySelector("[data-item-description]").value = item.description || "";
            row.querySelector("[data-item-qty]").value = item.qty == null ? "1" : item.qty;
            row.querySelector("[data-item-rate]").value = item.rate == null ? "0" : item.rate;
            row.querySelector("[data-item-tax]").value = item.tax_rate == null ? "0" : item.tax_rate;
            return row;
        }

        function sync() {
            var items = [];
            var subtotal = 0;
            var taxes = 0;
            list.querySelectorAll("[data-line-row]").forEach(function (row) {
                var name = row.querySelector("[data-item-name]").value.trim();
                var description = row.querySelector("[data-item-description]").value.trim();
                var qty = money(row.querySelector("[data-item-qty]").value);
                var rate = money(row.querySelector("[data-item-rate]").value);
                var tax = money(row.querySelector("[data-item-tax]").value);
                var amount = qty * rate;
                subtotal += amount;
                taxes += amount * tax / 100;
                row.querySelector("[data-item-amount]").textContent = amount.toFixed(2);
                if (name) {
                    items.push({
                        name: name,
                        description: description,
                        qty: qty,
                        rate: rate,
                        tax_rate: tax
                    });
                }
            });
            hidden.value = JSON.stringify(items);
            var finalTotal = Math.max(0, subtotal + taxes - money(discount.value));
            totalNode.textContent = (currency.value || "INR").toUpperCase() + " " + finalTotal.toFixed(2);
        }

        function addRow(item, shouldFocus) {
            var row = rowTemplate(item);
            list.appendChild(row);
            if (shouldFocus !== false) row.querySelector("[data-item-name]").focus();
            sync();
        }

        add.addEventListener("click", function () {
            addRow({}, true);
        });
        list.addEventListener("input", sync);
        list.addEventListener("click", function (event) {
            var remove = event.target.closest("[data-remove-line]");
            if (!remove) return;
            var row = remove.closest("[data-line-row]");
            if (row) row.remove();
            sync();
        });
        discount.addEventListener("input", sync);
        currency.addEventListener("input", sync);

        var lead = form.querySelector("[data-lead-select]");
        if (lead) {
            lead.addEventListener("change", function () {
                var option = lead.options[lead.selectedIndex];
                if (!option || !option.value) return;
                var fields = {
                    "[data-recipient-name]": option.dataset.name || "",
                    "[data-recipient-email]": option.dataset.email || "",
                    "[data-recipient-phone]": option.dataset.phone || ""
                };
                Object.keys(fields).forEach(function (selector) {
                    var input = form.querySelector(selector);
                    if (input && !input.value) input.value = fields[selector];
                });
            });
        }

        var documentRich = form.querySelector("[data-document-rich-editor]");
        var documentLayoutSource = form.querySelector("[data-document-layout-source]");

        function syncDocumentLayout() {
            if (!documentRich || !documentLayoutSource) return;
            var visibleText = (documentRich.textContent || "").trim();
            var hasStructuralContent = documentRich.querySelector("table,hr,img,ul,ol,h1,h2,h3");
            documentLayoutSource.value = (
                visibleText || hasStructuralContent
            ) ? documentRich.innerHTML : "";
        }

        function selectionInsideEditor(selection, editor) {
            if (!editor || !selection || selection.rangeCount === 0) return false;
            var range = selection.getRangeAt(0);
            var container = range.commonAncestorContainer;
            if (container.nodeType === Node.TEXT_NODE) container = container.parentNode;
            return container === editor || editor.contains(container);
        }

        form.querySelectorAll("[data-document-rich-command]").forEach(function (button) {
            button.addEventListener("click", function () {
                if (!documentRich) return;
                documentRich.focus();
                document.execCommand(
                    button.dataset.documentRichCommand,
                    false,
                    button.dataset.richValue || null
                );
                syncDocumentLayout();
            });
        });

        form.querySelectorAll("[data-document-merge]").forEach(function (button) {
            button.addEventListener("click", function () {
                if (!documentRich) return;
                var text = button.dataset.documentMerge || "";
                documentRich.focus();
                var selection = window.getSelection();
                if (!selectionInsideEditor(selection, documentRich)) {
                    documentRich.appendChild(document.createTextNode(text));
                } else {
                    var range = selection.getRangeAt(0);
                    range.deleteContents();
                    var node = document.createTextNode(text);
                    range.insertNode(node);
                    range.setStartAfter(node);
                    range.collapse(true);
                    selection.removeAllRanges();
                    selection.addRange(range);
                }
                syncDocumentLayout();
            });
        });
        if (documentRich) documentRich.addEventListener("input", syncDocumentLayout);
        form.addEventListener("submit", syncDocumentLayout);

        var initialNode = document.getElementById("shvya-sales-initial-items");
        var initialItems = [];
        if (initialNode) {
            try {
                initialItems = JSON.parse(initialNode.textContent || "[]");
            } catch (error) {
                initialItems = [];
            }
        }
        if (Array.isArray(initialItems) && initialItems.length) {
            initialItems.forEach(function (item) {
                addRow(item, false);
            });
        } else {
            addRow({}, false);
        }
        sync();
    }

    function sendSheet() {
        var sheet = document.querySelector("[data-send-sheet]");
        if (!sheet) return;
        var open = document.querySelector("[data-open-send]");
        var closes = sheet.querySelectorAll("[data-close-send]");
        var toggles = sheet.querySelectorAll("[data-channel-toggle]");

        function setOpen(value) {
            sheet.hidden = !value;
            document.body.classList.toggle("sy-sheet-open", value);
            if (value) {
                var first = sheet.querySelector('input[name="channels"]:not(:disabled)');
                if (first) first.focus();
            }
        }

        function updateEditors() {
            toggles.forEach(function (toggle) {
                var editor = sheet.querySelector('[data-channel-editor="' + toggle.dataset.channelToggle + '"]');
                if (editor) editor.hidden = !toggle.checked;
            });
        }

        var sendModeInputs = sheet.querySelectorAll('input[name="send_mode"]');
        var schedulePanel = sheet.querySelector("[data-schedule-panel]");
        function updateSchedulePanel() {
            var selected = sheet.querySelector('input[name="send_mode"]:checked');
            if (schedulePanel) {
                schedulePanel.hidden = !selected || selected.value !== "schedule";
            }
        }

        if (open) open.addEventListener("click", function () { setOpen(true); });
        closes.forEach(function (button) {
            button.addEventListener("click", function () { setOpen(false); });
        });
        toggles.forEach(function (toggle) {
            toggle.addEventListener("change", updateEditors);
        });
        sendModeInputs.forEach(function (input) {
            input.addEventListener("change", updateSchedulePanel);
        });
        sheet.addEventListener("click", function (event) {
            if (event.target === sheet) setOpen(false);
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !sheet.hidden) setOpen(false);
        });
        updateEditors();
        updateSchedulePanel();
    }

    function templateBuilder() {
        var form = document.querySelector("[data-template-builder]");
        if (!form) return;
        var preview = form.querySelector("[data-builder-preview]");
        var color = form.querySelector("[data-accent-picker]");
        var header = form.querySelector("[data-preview-header]");
        var footer = form.querySelector("[data-preview-footer]");
        var logo = form.querySelector("[data-preview-logo]");
        var logoUpload = form.querySelector("[data-preview-logo-upload]");
        var source = form.querySelector("[data-layout-editor]");
        var rich = form.querySelector("[data-rich-editor]");
        var objectUrl = "";

        function syncRichSource() {
            if (source && rich) source.value = rich.innerHTML;
        }

        function updateLogoPreview(url) {
            var box = form.querySelector("[data-preview-logo-box]");
            if (!box) return;
            box.innerHTML = "";
            if (url) {
                var image = document.createElement("img");
                image.src = url;
                image.alt = "";
                box.appendChild(image);
            } else {
                box.innerHTML = '<i class="ti ti-building"></i>';
            }
        }

        if (color && preview) {
            color.addEventListener("input", function () {
                preview.style.setProperty("--preview-accent", color.value);
            });
        }
        if (header) {
            header.addEventListener("input", function () {
                var node = form.querySelector("[data-preview-header-text]");
                if (node) node.textContent = header.value || "Your company";
            });
        }
        if (footer) {
            footer.addEventListener("input", function () {
                var node = form.querySelector("[data-preview-footer-text]");
                if (node) node.textContent = footer.value || "Thank you for your business.";
            });
        }
        if (logo) {
            logo.addEventListener("change", function () {
                if (logoUpload && logoUpload.files && logoUpload.files.length) return;
                updateLogoPreview(logo.value.trim());
            });
        }
        if (logoUpload) {
            logoUpload.addEventListener("change", function () {
                if (objectUrl) URL.revokeObjectURL(objectUrl);
                objectUrl = "";
                if (logoUpload.files && logoUpload.files[0]) {
                    objectUrl = URL.createObjectURL(logoUpload.files[0]);
                    updateLogoPreview(objectUrl);
                } else {
                    updateLogoPreview(logo ? logo.value.trim() : "");
                }
            });
        }

        form.querySelectorAll("[data-rich-command]").forEach(function (button) {
            button.addEventListener("click", function () {
                if (!rich) return;
                rich.focus();
                var command = button.dataset.richCommand;
                var value = button.dataset.richValue || null;
                document.execCommand(command, false, value);
                syncRichSource();
            });
        });

        function selectionInsideRich(selection) {
            if (!rich || !selection || selection.rangeCount === 0) return false;
            var range = selection.getRangeAt(0);
            var container = range.commonAncestorContainer;
            if (container.nodeType === Node.TEXT_NODE) container = container.parentNode;
            return container === rich || rich.contains(container);
        }

        function insertMergeField(text) {
            if (!rich) return;
            rich.focus();
            var selection = window.getSelection();
            if (!selectionInsideRich(selection)) {
                rich.appendChild(document.createTextNode(text));
                syncRichSource();
                return;
            }
            var range = selection.getRangeAt(0);
            range.deleteContents();
            var node = document.createTextNode(text);
            range.insertNode(node);
            range.setStartAfter(node);
            range.collapse(true);
            selection.removeAllRanges();
            selection.addRange(range);
            syncRichSource();
        }

        form.querySelectorAll("[data-insert-merge]").forEach(function (button) {
            button.addEventListener("click", function () {
                insertMergeField(button.dataset.insertMerge || "");
            });
        });

        if (rich) rich.addEventListener("input", syncRichSource);
        form.addEventListener("submit", syncRichSource);
        syncRichSource();
    }

    document.addEventListener("DOMContentLoaded", function () {
        documentBuilder();
        sendSheet();
        templateBuilder();
    });
})();
