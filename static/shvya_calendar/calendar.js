document.addEventListener("DOMContentLoaded", () => {
  const editor = document.querySelector(".sc-editor");
  if (!editor) return;

  const active = editor.dataset.activeTab || "lead";
  document.querySelectorAll("[data-tab-link]").forEach((link) => {
    link.classList.toggle("is-active", link.dataset.tabLink === active);
  });
  document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.tabPanel === active);
  });

  const pipeline = document.getElementById("sc-pipeline");
  const stage = document.getElementById("sc-stage");
  const filterStages = () => {
    if (!pipeline || !stage) return;
    const selected = pipeline.value;
    Array.from(stage.options).forEach((option) => {
      if (!option.dataset.pipeline) return;
      option.hidden = option.dataset.pipeline !== selected;
      if (option.hidden && option.selected) option.selected = false;
    });
  };
  pipeline?.addEventListener("change", filterStages);
  filterStages();

  const colorPicker = document.querySelector("[data-color-picker]");
  const colorText = document.querySelector("[data-color-text]");
  colorPicker?.addEventListener("input", () => {
    if (colorText) colorText.value = colorPicker.value.toUpperCase();
  });
  colorText?.addEventListener("input", () => {
    if (/^#[0-9A-Fa-f]{6}$/.test(colorText.value) && colorPicker) {
      colorPicker.value = colorText.value;
    }
  });

  const schemaNode = document.getElementById("sc-initial-schema");
  const schemaInput = document.getElementById("sc-form-schema");
  const fieldList = document.getElementById("sc-field-list");
  let schema = [];
  if (schemaNode) {
    try { schema = JSON.parse(schemaNode.textContent || "[]"); } catch (_err) { schema = []; }
  }

  const slugKey = (label) => {
    const value = String(label || "").toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 54);
    return value || "field";
  };

  const esc = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

  const renderFields = () => {
    if (!schemaInput || !fieldList) return;
    schemaInput.value = JSON.stringify(schema);
    fieldList.innerHTML = schema.map((field, index) => (
      '<div class="sc-builder-row">' +
        '<span class="sc-grab"><i class="ti ti-grip-vertical"></i></span>' +
        '<strong>' + esc(field.label) + (field.required ? ' · Required' : '') + '</strong>' +
        '<span class="sc-builder-map">' + esc(field.map_to === "submission" ? "Submission only" : field.map_to) + '</span>' +
        '<span class="sc-builder-type">' + esc(field.field_type) + '</span>' +
        '<button class="sc-icon-button sc-danger" type="button" data-remove-field="' + index + '" aria-label="Remove field"><i class="ti ti-trash"></i></button>' +
      '</div>'
    )).join("");
    fieldList.querySelectorAll("[data-remove-field]").forEach((button) => {
      button.addEventListener("click", () => {
        schema.splice(Number(button.dataset.removeField), 1);
        renderFields();
      });
    });
  };
  renderFields();

  const fieldDrawer = document.getElementById("sc-field-drawer");
  const openFieldDrawer = () => {
    fieldDrawer?.classList.add("is-open");
    fieldDrawer?.setAttribute("aria-hidden", "false");
  };
  const closeFieldDrawer = () => {
    fieldDrawer?.classList.remove("is-open");
    fieldDrawer?.setAttribute("aria-hidden", "true");
  };
  document.querySelector("[data-open-field-drawer]")?.addEventListener("click", openFieldDrawer);
  document.querySelectorAll("[data-close-drawer]").forEach((el) => el.addEventListener("click", closeFieldDrawer));

  const mapSelect = document.getElementById("sc-new-field-map");
  const typeSelect = document.getElementById("sc-new-field-type");
  mapSelect?.addEventListener("change", () => {
    const selected = mapSelect.selectedOptions[0];
    const type = selected?.dataset.type;
    const map = mapSelect.value;
    if (map === "email") typeSelect.value = "email";
    else if (map.startsWith("attr:")) {
      const translated = { text: "text", numeric: "numeric", date: "date", datetime: "datetime", option: "option" };
      typeSelect.value = translated[type] || "text";
    }
    typeSelect.disabled = map !== "submission";
  });
  mapSelect?.dispatchEvent(new Event("change"));

  document.getElementById("sc-add-field-confirm")?.addEventListener("click", () => {
    const labelInput = document.getElementById("sc-new-field-label");
    const placeholderInput = document.getElementById("sc-new-field-placeholder");
    const requiredInput = document.getElementById("sc-new-field-required");
    const label = labelInput?.value.trim() || "";
    if (!label) { labelInput?.focus(); return; }
    let key = slugKey(label);
    let suffix = 2;
    while (schema.some((item) => item.key === key)) {
      key = slugKey(label).slice(0, 50) + "_" + suffix++;
    }
    schema.push({
      key,
      label,
      map_to: mapSelect?.value || "submission",
      field_type: typeSelect?.value || "text",
      placeholder: placeholderInput?.value.trim() || "",
      required: Boolean(requiredInput?.checked),
      options: [],
    });
    if (labelInput) labelInput.value = "";
    if (placeholderInput) placeholderInput.value = "";
    if (requiredInput) requiredInput.checked = false;
    renderFields();
    closeFieldDrawer();
  });

  const reminderDrawer = document.getElementById("sc-reminder-drawer");
  const reminderChannel = document.getElementById("sc-reminder-channel");
  const reminderTitle = document.getElementById("sc-reminder-title");
  const emailSubject = document.querySelector(".sc-email-subject");
  const bodyLabel = document.getElementById("sc-reminder-body-label");
  const openReminder = (channel) => {
    if (reminderChannel) reminderChannel.value = channel;
    const labels = { whatsapp: "Add WhatsApp", email: "Add Email", call_reminder: "Add Call Reminder" };
    if (reminderTitle) reminderTitle.textContent = labels[channel] || "Add reminder";
    if (emailSubject) emailSubject.hidden = channel !== "email";
    if (bodyLabel) bodyLabel.textContent = channel === "call_reminder" ? "Call notes" : "Content";
    reminderDrawer?.classList.add("is-open");
    reminderDrawer?.setAttribute("aria-hidden", "false");
  };
  const closeReminder = () => {
    reminderDrawer?.classList.remove("is-open");
    reminderDrawer?.setAttribute("aria-hidden", "true");
  };
  document.querySelectorAll("[data-reminder-channel]").forEach((button) => {
    button.addEventListener("click", () => openReminder(button.dataset.reminderChannel));
  });
  document.querySelectorAll("[data-close-reminder]").forEach((el) => el.addEventListener("click", closeReminder));

  const timing = document.getElementById("sc-reminder-timing");
  const before = document.querySelector(".sc-timing-before");
  const syncTiming = () => { if (before) before.hidden = timing?.value !== "before"; };
  timing?.addEventListener("change", syncTiming);
  syncTiming();

  document.querySelectorAll(".sc-token-row button").forEach((button) => {
    button.addEventListener("click", () => {
      const drawer = button.closest(".sc-drawer");
      const textarea = drawer?.querySelector("textarea[name='body']");
      if (!textarea) return;
      const token = button.textContent.trim();
      const start = textarea.selectionStart ?? textarea.value.length;
      const end = textarea.selectionEnd ?? textarea.value.length;
      textarea.value = textarea.value.slice(0, start) + token + textarea.value.slice(end);
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = start + token.length;
    });
  });
});
