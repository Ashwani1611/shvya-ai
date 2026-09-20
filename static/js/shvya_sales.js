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

        function rowTemplate() {
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

        function addRow() {
            var row = rowTemplate();
            list.appendChild(row);
            row.querySelector("[data-item-name]").focus();
            sync();
        }

        add.addEventListener("click", addRow);
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

        addRow();
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

        if (open) open.addEventListener("click", function () { setOpen(true); });
        closes.forEach(function (button) {
            button.addEventListener("click", function () { setOpen(false); });
        });
        toggles.forEach(function (toggle) {
            toggle.addEventListener("change", updateEditors);
        });
        sheet.addEventListener("click", function (event) {
            if (event.target === sheet) setOpen(false);
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !sheet.hidden) setOpen(false);
        });
        updateEditors();
    }

    function templateBuilder() {
        var form = document.querySelector("[data-template-builder]");
        if (!form) return;
        var preview = form.querySelector("[data-builder-preview]");
        var color = form.querySelector("[data-accent-picker]");
        var header = form.querySelector("[data-preview-header]");
        var footer = form.querySelector("[data-preview-footer]");
        var logo = form.querySelector("[data-preview-logo]");
        var editor = form.querySelector("[data-layout-editor]");

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
                var box = form.querySelector("[data-preview-logo-box]");
                if (!box) return;
                box.innerHTML = "";
                if (logo.value.trim()) {
                    var image = document.createElement("img");
                    image.src = logo.value.trim();
                    image.alt = "";
                    box.appendChild(image);
                } else {
                    box.innerHTML = '<i class="ti ti-building"></i>';
                }
            });
        }
        form.querySelectorAll("[data-insert-merge]").forEach(function (button) {
            button.addEventListener("click", function () {
                if (!editor) return;
                var text = button.dataset.insertMerge || "";
                var start = editor.selectionStart || editor.value.length;
                var end = editor.selectionEnd || editor.value.length;
                editor.value = editor.value.slice(0, start) + text + editor.value.slice(end);
                editor.focus();
                editor.selectionStart = editor.selectionEnd = start + text.length;
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        documentBuilder();
        sendSheet();
        templateBuilder();
    });
})();
